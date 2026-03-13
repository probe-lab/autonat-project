package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	dht "github.com/libp2p/go-libp2p-kad-dht"
	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/event"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/libp2p/go-libp2p/core/protocol"
	"github.com/libp2p/go-libp2p/p2p/host/observedaddrs"
	ma "github.com/multiformats/go-multiaddr"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

var startTime time.Time

// peerStats tracks counts for high-frequency peer events (printed as periodic summary).
var peerStats struct {
	identified     atomic.Int64
	identifyFailed atomic.Int64
	connected      atomic.Int64
	disconnected   atomic.Int64
	protoUpdated   atomic.Int64
}

// emitSpan creates a short-lived span that exports immediately via the batcher.
// Each call produces a separate span in Jaeger, avoiding the 128-event-per-span limit.
func emitSpan(name string, attrs ...attribute.KeyValue) {
	tracer := otel.Tracer("autonat-testbed")
	attrs = append(attrs, attribute.Int64("elapsed_ms", time.Since(startTime).Milliseconds()))
	_, span := tracer.Start(context.Background(), name, trace.WithAttributes(attrs...))
	span.End()
}

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
	jitterFlag := flag.Int("jitter", 0, "Random jitter added to delay in milliseconds; actual delay = delay + rand(0, jitter) (mock-server role only)")
	probabilityFlag := flag.Float64("probability", 0.5, "P(reachable) for probabilistic behavior, 0.0–1.0 (mock-server role only)")
	tcpBehaviorFlag := flag.String("tcp-behavior", "", "Behavior override for TCP addresses; overrides --behavior for TCP probes (mock-server role only)")
	quicBehaviorFlag := flag.String("quic-behavior", "", "Behavior override for QUIC addresses; overrides --behavior for QUIC probes (mock-server role only)")
	dhtMode := flag.String("dht-mode", "auto", "DHT mode: auto, client, or server")
	otlpEndpoint := flag.String("otlp-endpoint", "", "OTLP HTTP endpoint for trace export (e.g. http://jaeger:4318)")
	autonatRefresh := flag.Int("autonat-refresh", 0, "AutoNAT v1 refresh interval in seconds (0 = default 15min)")
	flag.Parse()

	// Initialize OTEL tracing if requested
	if *otlpEndpoint != "" {
		shutdown, err := initTracer(*otlpEndpoint)
		if err != nil {
			log.Fatalf("Failed to initialize tracer: %v", err)
		}
		defer shutdown()
		log.Printf("OTEL tracing enabled, exporting to %s", *otlpEndpoint)
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
			log.Fatalf("Invalid --behavior: %v", err)
		}

		cfg := MockServerConfig{
			Behavior:    behavior,
			Delay:       time.Duration(*delayFlag) * time.Millisecond,
			Jitter:      time.Duration(*jitterFlag) * time.Millisecond,
			Probability: *probabilityFlag,
		}

		if *tcpBehaviorFlag != "" {
			b, err := parseBehavior(*tcpBehaviorFlag)
			if err != nil {
				log.Fatalf("Invalid --tcp-behavior: %v", err)
			}
			cfg.TCPBehavior = &b
		}
		if *quicBehaviorFlag != "" {
			b, err := parseBehavior(*quicBehaviorFlag)
			if err != nil {
				log.Fatalf("Invalid --quic-behavior: %v", err)
			}
			cfg.QUICBehavior = &b
		}

		ms, err := startMockServer(listenAddrs, cfg)
		if err != nil {
			log.Fatalf("Failed to start mock server: %v", err)
		}
		defer ms.Close()

		mockHost := ms.Host()
		log.Printf("Mock server started: %s (behavior=%s, delay=%s, jitter=%s, probability=%.2f)",
			mockHost.ID(), behavior, cfg.Delay, cfg.Jitter, cfg.Probability)
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

	// Override v1 AutoNAT probe schedule if requested.
	// This forces more frequent re-probing so v1 oscillation is visible in short runs.
	if *autonatRefresh > 0 {
		refresh := time.Duration(*autonatRefresh) * time.Second
		log.Printf("AutoNAT v1 schedule override: refreshInterval=%s", refresh)
		opts = append(opts, libp2p.AutoNATSchedule(90*time.Second, refresh))
	}

	// Create the host
	h, err := libp2p.New(opts...)
	if err != nil {
		log.Fatalf("Failed to create libp2p host: %v", err)
	}
	defer h.Close()

	// Create the session span — covers the entire node lifetime.
	// Only carries session-level attributes; individual events are separate spans.
	tracer := otel.Tracer("autonat-testbed")
	_, sessionSpan := tracer.Start(context.Background(), "autonat.session",
		trace.WithAttributes(
			attribute.String("role", *role),
			attribute.String("transport", *transport),
			attribute.String("peer_id", h.ID().String()),
			attribute.StringSlice("listen_addrs", multiaddrsToStrings(h.Addrs())),
		))
	defer sessionSpan.End()

	emitSpan("started",
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

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Subscribe to all event bus events
	go subscribeAllEvents(ctx, h)

	// Print periodic summary of high-frequency peer events
	go func() {
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				log.Printf("[peer summary] identified=%d identify_failed=%d connected=%d disconnected=%d proto_updated=%d",
					peerStats.identified.Load(), peerStats.identifyFailed.Load(),
					peerStats.connected.Load(), peerStats.disconnected.Load(),
					peerStats.protoUpdated.Load())
			}
		}
	}()

	if *role == "client" {
		if *bootstrap {
			go bootstrapDHT(ctx, h, *dhtMode)
		} else if *peerDir != "" {
			go discoverFromDir(ctx, h, *peerDir)
		} else if *peers != "" {
			go connectToPeers(ctx, h, *peers)
		} else {
			log.Println("Warning: client mode with no --peers, no --peer-dir, and no --bootstrap; waiting for inbound connections")
		}
	}

	// Wait for shutdown signal
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh

	emitSpan("shutdown",
		attribute.String("message", "received signal, shutting down"),
	)
	log.Println("Shutting down...")
}

func buildListenAddrs(ip string, port int, transport string) []string {
	var addrs []string
	switch transport {
	case "tcp":
		addrs = append(addrs,
			fmt.Sprintf("/ip4/%s/tcp/%d", ip, port),
			fmt.Sprintf("/ip6/::/tcp/%d", port),
		)
	case "quic":
		addrs = append(addrs,
			fmt.Sprintf("/ip4/%s/udp/%d/quic-v1", ip, port),
			fmt.Sprintf("/ip6/::/udp/%d/quic-v1", port),
		)
	case "both":
		addrs = append(addrs,
			fmt.Sprintf("/ip4/%s/tcp/%d", ip, port),
			fmt.Sprintf("/ip4/%s/udp/%d/quic-v1", ip, port),
			fmt.Sprintf("/ip6/::/tcp/%d", port),
			fmt.Sprintf("/ip6/::/udp/%d/quic-v1", port),
		)
	case "all":
		addrs = append(addrs,
			fmt.Sprintf("/ip4/%s/tcp/%d", ip, port),
			fmt.Sprintf("/ip4/%s/udp/%d/quic-v1", ip, port),
			fmt.Sprintf("/ip4/%s/udp/%d/quic-v1/webtransport", ip, port),
			fmt.Sprintf("/ip4/%s/udp/%d/webrtc-direct", ip, port),
			fmt.Sprintf("/ip6/::/tcp/%d", port),
			fmt.Sprintf("/ip6/::/udp/%d/quic-v1", port),
			fmt.Sprintf("/ip6/::/udp/%d/quic-v1/webtransport", port),
			fmt.Sprintf("/ip6/::/udp/%d/webrtc-direct", port),
		)
	default:
		log.Fatalf("Unknown transport: %s (use tcp, quic, both, or all)", transport)
	}
	return addrs
}

func parseDHTMode(mode string) dht.ModeOpt {
	switch mode {
	case "auto":
		return dht.ModeAutoServer
	case "client":
		return dht.ModeClient
	case "server":
		return dht.ModeServer
	default:
		log.Fatalf("Unknown DHT mode: %s (use auto, client, or server)", mode)
		return dht.ModeAutoServer
	}
}

// subscribeAllEvents subscribes to all relevant event bus events and dispatches them.
func subscribeAllEvents(ctx context.Context, h host.Host) {
	sub, err := h.EventBus().Subscribe([]any{
		new(event.EvtLocalReachabilityChanged),
		new(event.EvtHostReachableAddrsChanged),
		new(event.EvtNATDeviceTypeChanged),
		new(event.EvtLocalAddressesUpdated),
		new(event.EvtPeerIdentificationCompleted),
		new(event.EvtPeerIdentificationFailed),
		new(event.EvtPeerConnectednessChanged),
		new(event.EvtPeerProtocolsUpdated),
		new(event.EvtLocalProtocolsUpdated),
		new(event.EvtAutoRelayAddrsUpdated),
	})
	if err != nil {
		log.Printf("Failed to subscribe to events: %v", err)
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
			handleEvent(evt, h)
		}
	}
}

func handleEvent(evt interface{}, h host.Host) {
	switch tevt := evt.(type) {
	// --- Important events: emit individual short-lived spans ---

	case event.EvtLocalReachabilityChanged:
		reachStr := reachabilityString(tevt.Reachability)
		log.Printf("REACHABILITY CHANGED: %s", reachStr)
		emitSpan("reachability_changed",
			attribute.String("reachability", reachStr),
			attribute.StringSlice("addresses", multiaddrsToStrings(h.Addrs())),
		)

	case event.EvtHostReachableAddrsChanged:
		log.Printf("REACHABLE ADDRS CHANGED: reachable=%d unreachable=%d unknown=%d",
			len(tevt.Reachable), len(tevt.Unreachable), len(tevt.Unknown))
		for _, a := range tevt.Reachable {
			log.Printf("  REACHABLE:   %s", a)
		}
		for _, a := range tevt.Unreachable {
			log.Printf("  UNREACHABLE: %s", a)
		}
		for _, a := range tevt.Unknown {
			log.Printf("  UNKNOWN:     %s", a)
		}
		emitSpan("reachable_addrs_changed",
			attribute.StringSlice("reachable", multiaddrsToStrings(tevt.Reachable)),
			attribute.StringSlice("unreachable", multiaddrsToStrings(tevt.Unreachable)),
			attribute.StringSlice("unknown", multiaddrsToStrings(tevt.Unknown)),
		)

	case event.EvtNATDeviceTypeChanged:
		log.Printf("NAT DEVICE TYPE (%s): %s", tevt.TransportProtocol, tevt.NatDeviceType)
		emitSpan("nat_device_type_changed",
			attribute.String("transport_protocol", string(tevt.TransportProtocol)),
			attribute.String("nat_device_type", tevt.NatDeviceType.String()),
		)

	case event.EvtLocalAddressesUpdated:
		var current []string
		for _, a := range tevt.Current {
			current = append(current, a.Address.String())
		}
		var removed []string
		for _, a := range tevt.Removed {
			removed = append(removed, a.Address.String())
		}
		emitSpan("addresses_updated",
			attribute.StringSlice("current", current),
			attribute.StringSlice("removed", removed),
		)

	case event.EvtLocalProtocolsUpdated:
		log.Printf("LOCAL PROTOCOLS UPDATED: added=%d removed=%d", len(tevt.Added), len(tevt.Removed))
		emitSpan("local_protocols_updated",
			attribute.StringSlice("added", protocolIDsToStrings(tevt.Added)),
			attribute.StringSlice("removed", protocolIDsToStrings(tevt.Removed)),
		)

	case event.EvtAutoRelayAddrsUpdated:
		log.Printf("AUTO-RELAY ADDRS UPDATED: %d relay addresses", len(tevt.RelayAddrs))
		for _, addr := range tevt.RelayAddrs {
			log.Printf("  RELAY: %s", addr)
		}
		emitSpan("auto_relay_addrs_updated",
			attribute.StringSlice("relay_addrs", multiaddrsToStrings(tevt.RelayAddrs)),
		)

	// --- High-frequency peer events: counter-only, no spans ---

	case event.EvtPeerIdentificationCompleted:
		peerStats.identified.Add(1)

	case event.EvtPeerIdentificationFailed:
		peerStats.identifyFailed.Add(1)

	case event.EvtPeerConnectednessChanged:
		if tevt.Connectedness == network.Connected {
			peerStats.connected.Add(1)
		} else {
			peerStats.disconnected.Add(1)
		}

	case event.EvtPeerProtocolsUpdated:
		peerStats.protoUpdated.Add(1)

	default:
		log.Printf("Unknown event: %T", evt)
	}
}

func connectToPeers(ctx context.Context, h host.Host, peersStr string) {
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
			emitSpan("connect_failed",
				attribute.String("peer_id", pi.ID.String()),
				attribute.String("message", err.Error()),
			)
		} else {
			log.Printf("Connected to %s", pi.ID)
			emitSpan("connected",
				attribute.String("peer_id", pi.ID.String()),
			)
		}
	}
}

func bootstrapDHT(ctx context.Context, h host.Host, dhtMode string) {
	mode := parseDHTMode(dhtMode)
	emitSpan("bootstrap_start",
		attribute.String("message", "connecting to IPFS DHT bootstrap peers"),
		attribute.String("dht_mode", dhtMode),
	)

	d, err := dht.New(ctx, h, dht.Mode(mode))
	if err != nil {
		log.Printf("Failed to create DHT: %v", err)
		emitSpan("bootstrap_error",
			attribute.String("message", err.Error()),
		)
		return
	}
	defer d.Close()

	if err := d.Bootstrap(ctx); err != nil {
		log.Printf("DHT bootstrap failed: %v", err)
		emitSpan("bootstrap_error",
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
			emitSpan("bootstrap_connected",
				attribute.String("peer_id", pi.ID.String()),
			)
		}
	}

	emitSpan("bootstrap_done",
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

func discoverFromDir(ctx context.Context, h host.Host, dir string) {
	emitSpan("peer_discovery_start",
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
			emitSpan("peer_discovery_timeout",
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
			emitSpan("connect_failed",
				attribute.String("peer_id", id.String()),
				attribute.String("message", err.Error()),
			)
		} else {
			log.Printf("Connected to server %s", id.ShortString())
			emitSpan("connected",
				attribute.String("peer_id", id.String()),
			)
		}
	}

	emitSpan("peer_discovery_done",
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

func protocolIDsToStrings(ids []protocol.ID) []string {
	strs := make([]string, len(ids))
	for i, id := range ids {
		strs[i] = string(id)
	}
	return strs
}

func multiaddrsToStrings(addrs []ma.Multiaddr) []string {
	strs := make([]string, len(addrs))
	for i, a := range addrs {
		strs[i] = a.String()
	}
	return strs
}

// initTracer sets up an OTEL TracerProvider that exports via OTLP HTTP to a collector/Jaeger.
func initTracer(otlpEndpoint string) (func(), error) {
	ctx := context.Background()
	exporter, err := otlptracehttp.New(ctx,
		otlptracehttp.WithEndpointURL(otlpEndpoint+"/v1/traces"),
		otlptracehttp.WithInsecure(),
	)
	if err != nil {
		return nil, fmt.Errorf("create OTLP exporter: %w", err)
	}

	res := resource.NewWithAttributes("",
		attribute.String("service.name", "autonat-testbed"),
	)

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tp)

	return func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		tp.Shutdown(ctx)
	}, nil
}
