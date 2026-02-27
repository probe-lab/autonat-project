package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	dht "github.com/libp2p/go-libp2p-kad-dht"
	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/event"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/libp2p/go-libp2p/p2p/host/observedaddrs"
	ma "github.com/multiformats/go-multiaddr"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/stdout/stdouttrace"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

var startTime time.Time

func main() {
	role := flag.String("role", "client", "Node role: server, client, or mock-server")
	transport := flag.String("transport", "both", "Transport: tcp, quic, or both")
	listenAddr := flag.String("listen", "0.0.0.0", "Listen IP address")
	listenPort := flag.Int("port", 4001, "Listen port")
	peers := flag.String("peers", "", "Comma-separated multiaddrs of AutoNAT servers (local mode)")
	bootstrap := flag.Bool("bootstrap", false, "Bootstrap to IPFS DHT to discover public AutoNAT servers")
	addrFile := flag.String("addr-file", "", "Write this node's multiaddr to file (for server discovery)")
	peerDir := flag.String("peer-dir", "", "Directory to read peer multiaddrs from (client reads server addr files)")
	obsAddrThresh := flag.Int("obs-addr-thresh", 0, "Override observed address activation threshold (default: 0 = use go-libp2p default of 4)")
	behaviorFlag := flag.String("behavior", "force-unreachable", "Mock server behavior (mock-server role only)")
	delayFlag := flag.Int("delay", 0, "Mock server response delay in milliseconds (mock-server role only)")
	traceFile := flag.String("trace-file", "", "Output file for OTEL traces (JSON); empty = no tracing")
	flag.Parse()

	// Initialize OTEL tracing if requested
	if *traceFile != "" {
		shutdown, err := initTracer(*traceFile)
		if err != nil {
			log.Fatalf("Failed to initialize tracer: %v", err)
		}
		defer shutdown()
		log.Printf("OTEL tracing enabled, writing to %s", *traceFile)
	}

	if *obsAddrThresh > 0 {
		observedaddrs.ActivationThresh = *obsAddrThresh
		log.Printf("Observed address activation threshold set to %d", *obsAddrThresh)
	}

	startTime = time.Now()

	// Build listen addresses based on transport flag
	listenAddrs := buildListenAddrs(*listenAddr, *listenPort, *transport)

	// Mock server mode: skip the standard host entirely and use a custom one.
	if *role == "mock-server" {
		behavior, err := parseBehavior(*behaviorFlag)
		if err != nil {
			log.Fatalf("Invalid behavior: %v", err)
		}
		delay := time.Duration(*delayFlag) * time.Millisecond

		ms, err := startMockServer(listenAddrs, behavior, delay)
		if err != nil {
			log.Fatalf("Failed to start mock server: %v", err)
		}
		defer ms.Close()

		mockHost := ms.Host()
		log.Printf("Mock server started: %s (behavior=%s, delay=%s)", mockHost.ID(), behavior, delay)
		for _, addr := range mockHost.Addrs() {
			log.Printf("  Listening on: %s/p2p/%s", addr, mockHost.ID())
		}

		if *addrFile != "" {
			writeAddrFile(mockHost, *addrFile)
		}

		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh

		log.Println("Shutting down...")
		return
	}

	// Build libp2p options
	opts := []libp2p.Option{
		libp2p.ListenAddrStrings(listenAddrs...),
		libp2p.EnableAutoNATv2(),
		libp2p.EnableNATService(),
		libp2p.NATPortMap(),
		// Disable the UDP black hole detector. Without this, the AutoNAT v2
		// dialerHost inherits the main host's counter which starts in Probing
		// state and blocks QUIC dial-backs on fresh nodes with no prior UDP
		// traffic. Setting nil here makes the dialerHost create a fresh counter
		// in Probing+ReadOnly state, which allows all QUIC dials.
		// See: https://github.com/libp2p/go-libp2p/issues/3467
		libp2p.UDPBlackHoleSuccessCounter(nil),
		libp2p.IPv6BlackHoleSuccessCounter(nil),
	}

	// Create the host
	h, err := libp2p.New(opts...)
	if err != nil {
		log.Fatalf("Failed to create libp2p host: %v", err)
	}
	defer h.Close()

	// Create the session span — covers the entire node lifetime.
	// All testbed lifecycle events are recorded as events on this span.
	tracer := otel.Tracer("autonat-testbed")
	ctx, sessionSpan := tracer.Start(context.Background(), "autonat.session",
		trace.WithAttributes(
			attribute.String("role", *role),
			attribute.String("transport", *transport),
			attribute.String("peer_id", h.ID().String()),
			attribute.StringSlice("listen_addrs", multiaddrsToStrings(h.Addrs())),
		))
	defer sessionSpan.End()

	addEvent(sessionSpan, "started",
		attribute.String("peer_id", h.ID().String()),
		attribute.StringSlice("addresses", multiaddrsToStrings(h.Addrs())),
		attribute.String("message", fmt.Sprintf("role=%s transport=%s", *role, *transport)),
	)

	log.Printf("Node started: %s (role=%s)", h.ID(), *role)
	for _, addr := range h.Addrs() {
		log.Printf("  Listening on: %s/p2p/%s", addr, h.ID())
	}

	// Write multiaddr to file if requested (used by servers for discovery)
	if *addrFile != "" {
		writeAddrFile(h, *addrFile)
	}

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	// Subscribe to reachability events (v1: EvtLocalReachabilityChanged, v2: EvtHostReachableAddrsChanged)
	go subscribeReachability(ctx, h, sessionSpan)
	go subscribeReachableAddrs(ctx, h, sessionSpan)

	// Subscribe to address update events
	go subscribeAddressUpdates(ctx, h, sessionSpan)

	if *role == "client" {
		if *bootstrap {
			go bootstrapDHT(ctx, h, sessionSpan)
		} else if *peerDir != "" {
			go discoverFromDir(ctx, h, *peerDir, sessionSpan)
		} else if *peers != "" {
			go connectToPeers(ctx, h, *peers, sessionSpan)
		} else {
			log.Println("Warning: client mode with no --peers, no --peer-dir, and no --bootstrap; waiting for inbound connections")
		}
	}

	// Wait for shutdown signal
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	addEvent(sessionSpan, "shutdown",
		attribute.String("message", "received signal, shutting down"),
	)
	log.Println("Shutting down...")
}

// addEvent adds a named event to the session span with elapsed_ms computed from startTime.
func addEvent(span trace.Span, name string, attrs ...attribute.KeyValue) {
	attrs = append(attrs, attribute.Int64("elapsed_ms", time.Since(startTime).Milliseconds()))
	span.AddEvent(name, trace.WithAttributes(attrs...))
}

func buildListenAddrs(ip string, port int, transport string) []string {
	var addrs []string
	switch transport {
	case "tcp":
		addrs = append(addrs, fmt.Sprintf("/ip4/%s/tcp/%d", ip, port))
	case "quic":
		addrs = append(addrs, fmt.Sprintf("/ip4/%s/udp/%d/quic-v1", ip, port))
	case "both":
		addrs = append(addrs,
			fmt.Sprintf("/ip4/%s/tcp/%d", ip, port),
			fmt.Sprintf("/ip4/%s/udp/%d/quic-v1", ip, port),
		)
	default:
		log.Fatalf("Unknown transport: %s (use tcp, quic, or both)", transport)
	}
	return addrs
}

func subscribeReachability(ctx context.Context, h host.Host, sessionSpan trace.Span) {
	sub, err := h.EventBus().Subscribe(new(event.EvtLocalReachabilityChanged))
	if err != nil {
		log.Printf("Failed to subscribe to reachability events: %v", err)
		return
	}
	defer sub.Close()

	for {
		select {
		case <-ctx.Done():
			return
		case evt, ok := <-sub.Out():
			if !ok {
				return
			}
			reachEvt := evt.(event.EvtLocalReachabilityChanged)
			reachStr := reachabilityString(reachEvt.Reachability)
			log.Printf("REACHABILITY CHANGED: %s", reachStr)
			addEvent(sessionSpan, "reachability_changed",
				attribute.String("reachability", reachStr),
				attribute.StringSlice("addresses", multiaddrsToStrings(h.Addrs())),
			)
		}
	}
}

func subscribeReachableAddrs(ctx context.Context, h host.Host, sessionSpan trace.Span) {
	sub, err := h.EventBus().Subscribe(new(event.EvtHostReachableAddrsChanged))
	if err != nil {
		log.Printf("Failed to subscribe to reachable addrs events: %v", err)
		return
	}
	defer sub.Close()

	for {
		select {
		case <-ctx.Done():
			return
		case evt, ok := <-sub.Out():
			if !ok {
				return
			}
			reachEvt := evt.(event.EvtHostReachableAddrsChanged)
			log.Printf("REACHABLE ADDRS CHANGED: reachable=%d unreachable=%d unknown=%d",
				len(reachEvt.Reachable), len(reachEvt.Unreachable), len(reachEvt.Unknown))
			for _, a := range reachEvt.Reachable {
				log.Printf("  REACHABLE:   %s", a)
			}
			for _, a := range reachEvt.Unreachable {
				log.Printf("  UNREACHABLE: %s", a)
			}
			for _, a := range reachEvt.Unknown {
				log.Printf("  UNKNOWN:     %s", a)
			}
			addEvent(sessionSpan, "reachable_addrs_changed",
				attribute.StringSlice("addresses", multiaddrsToStrings(reachEvt.Reachable)),
				attribute.StringSlice("unreachable", multiaddrsToStrings(reachEvt.Unreachable)),
				attribute.StringSlice("unknown", multiaddrsToStrings(reachEvt.Unknown)),
			)
		}
	}
}

func subscribeAddressUpdates(ctx context.Context, h host.Host, sessionSpan trace.Span) {
	sub, err := h.EventBus().Subscribe(new(event.EvtLocalAddressesUpdated))
	if err != nil {
		log.Printf("Failed to subscribe to address events: %v", err)
		return
	}
	defer sub.Close()

	for {
		select {
		case <-ctx.Done():
			return
		case evt, ok := <-sub.Out():
			if !ok {
				return
			}
			addrEvt := evt.(event.EvtLocalAddressesUpdated)
			var current []string
			for _, a := range addrEvt.Current {
				current = append(current, a.Address.String())
			}
			var removed []string
			for _, a := range addrEvt.Removed {
				removed = append(removed, a.Address.String())
			}
			addEvent(sessionSpan, "addresses_updated",
				attribute.StringSlice("addresses", current),
				attribute.StringSlice("removed", removed),
			)
		}
	}
}

func connectToPeers(ctx context.Context, h host.Host, peersStr string, sessionSpan trace.Span) {
	for _, addrStr := range strings.Split(peersStr, ",") {
		addrStr = strings.TrimSpace(addrStr)
		if addrStr == "" {
			continue
		}
		maddr, err := ma.NewMultiaddr(addrStr)
		if err != nil {
			log.Printf("Invalid peer multiaddr %q: %v", addrStr, err)
			continue
		}
		pi, err := peer.AddrInfoFromP2pAddr(maddr)
		if err != nil {
			log.Printf("Failed to parse peer info from %q: %v", addrStr, err)
			continue
		}
		if err := h.Connect(ctx, *pi); err != nil {
			log.Printf("Failed to connect to %s: %v", pi.ID, err)
			addEvent(sessionSpan, "connect_failed",
				attribute.String("peer_id", pi.ID.String()),
				attribute.String("message", err.Error()),
			)
		} else {
			log.Printf("Connected to %s", pi.ID)
			addEvent(sessionSpan, "connected",
				attribute.String("peer_id", pi.ID.String()),
			)
		}
	}
}

func bootstrapDHT(ctx context.Context, h host.Host, sessionSpan trace.Span) {
	addEvent(sessionSpan, "bootstrap_start",
		attribute.String("message", "connecting to IPFS DHT bootstrap peers"),
	)

	d, err := dht.New(ctx, h, dht.Mode(dht.ModeClient))
	if err != nil {
		log.Printf("Failed to create DHT: %v", err)
		addEvent(sessionSpan, "bootstrap_error",
			attribute.String("message", err.Error()),
		)
		return
	}
	defer d.Close()

	if err := d.Bootstrap(ctx); err != nil {
		log.Printf("DHT bootstrap failed: %v", err)
		addEvent(sessionSpan, "bootstrap_error",
			attribute.String("message", err.Error()),
		)
		return
	}

	// Connect to default bootstrap peers
	for _, peerAddr := range dht.DefaultBootstrapPeers {
		pi, err := peer.AddrInfoFromP2pAddr(peerAddr)
		if err != nil {
			continue
		}
		if err := h.Connect(ctx, *pi); err != nil {
			log.Printf("Bootstrap peer %s: failed (%v)", pi.ID.ShortString(), err)
		} else {
			log.Printf("Bootstrap peer %s: connected", pi.ID.ShortString())
			addEvent(sessionSpan, "bootstrap_connected",
				attribute.String("peer_id", pi.ID.String()),
			)
		}
	}

	addEvent(sessionSpan, "bootstrap_done",
		attribute.String("message", "DHT bootstrap complete, discovering peers..."),
	)

	// Keep the DHT running so it discovers peers (and AutoNAT servers)
	<-ctx.Done()
}

func writeAddrFile(h host.Host, path string) {
	var addrs []string
	for _, addr := range h.Addrs() {
		addrs = append(addrs, fmt.Sprintf("%s/p2p/%s", addr, h.ID()))
	}
	content := strings.Join(addrs, "\n") + "\n"
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		log.Printf("Failed to write addr file: %v", err)
	} else {
		log.Printf("Wrote multiaddrs to %s", path)
	}
}

func discoverFromDir(ctx context.Context, h host.Host, dir string, sessionSpan trace.Span) {
	addEvent(sessionSpan, "peer_discovery_start",
		attribute.String("message", fmt.Sprintf("reading server addresses from %s", dir)),
	)

	// Wait for server addr files to appear (servers may still be starting)
	maxWait := 30 * time.Second
	start := time.Now()
	for {
		entries, err := os.ReadDir(dir)
		if err == nil && len(entries) > 0 {
			break
		}
		if time.Since(start) > maxWait {
			log.Printf("Timeout waiting for peer addr files in %s", dir)
			addEvent(sessionSpan, "peer_discovery_timeout",
				attribute.String("message", fmt.Sprintf("no addr files found in %s after %s", dir, maxWait)),
			)
			return
		}
		time.Sleep(1 * time.Second)
	}

	// Small extra delay to let all servers write their files
	time.Sleep(3 * time.Second)

	entries, err := os.ReadDir(dir)
	if err != nil {
		log.Printf("Failed to read peer dir: %v", err)
		return
	}

	// Collect all addresses grouped by peer ID
	peerAddrs := make(map[peer.ID][]ma.Multiaddr)
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		data, err := os.ReadFile(fmt.Sprintf("%s/%s", dir, entry.Name()))
		if err != nil {
			log.Printf("Failed to read %s: %v", entry.Name(), err)
			continue
		}
		for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}
			maddr, err := ma.NewMultiaddr(line)
			if err != nil {
				log.Printf("Invalid multiaddr %q: %v", line, err)
				continue
			}
			pi, err := peer.AddrInfoFromP2pAddr(maddr)
			if err != nil {
				log.Printf("Failed to parse peer info from %q: %v", line, err)
				continue
			}
			peerAddrs[pi.ID] = append(peerAddrs[pi.ID], pi.Addrs...)
		}
	}

	// Connect to each peer once with all their addresses
	for id, addrs := range peerAddrs {
		pi := peer.AddrInfo{ID: id, Addrs: addrs}
		if err := h.Connect(ctx, pi); err != nil {
			log.Printf("Failed to connect to %s: %v", id.ShortString(), err)
			addEvent(sessionSpan, "connect_failed",
				attribute.String("peer_id", id.String()),
				attribute.String("message", err.Error()),
			)
		} else {
			log.Printf("Connected to server %s", id.ShortString())
			addEvent(sessionSpan, "connected",
				attribute.String("peer_id", id.String()),
			)
		}
	}

	addEvent(sessionSpan, "peer_discovery_done",
		attribute.String("message", fmt.Sprintf("connected to servers from %s", dir)),
	)
}

func reachabilityString(r network.Reachability) string {
	switch r {
	case network.ReachabilityPublic:
		return "public"
	case network.ReachabilityPrivate:
		return "private"
	default:
		return "unknown"
	}
}

func multiaddrsToStrings(addrs []ma.Multiaddr) []string {
	strs := make([]string, len(addrs))
	for i, a := range addrs {
		strs[i] = a.String()
	}
	return strs
}

// initTracer sets up an OTEL TracerProvider that exports spans as JSON to a file.
// Each span is written as a single-line JSON object (JSONL format).
func initTracer(path string) (func(), error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, fmt.Errorf("create trace file: %w", err)
	}

	exporter, err := stdouttrace.New(
		stdouttrace.WithWriter(f),
	)
	if err != nil {
		f.Close()
		return nil, fmt.Errorf("create stdout exporter: %w", err)
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
	)
	otel.SetTracerProvider(tp)

	return func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		tp.Shutdown(ctx)
		f.Close()
	}, nil
}
