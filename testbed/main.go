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
	"github.com/libp2p/go-libp2p/p2p/host/eventbus"
	"github.com/libp2p/go-libp2p/p2p/host/observedaddrs"
	ma "github.com/multiformats/go-multiaddr"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/exporters/stdout/stdouttrace"
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
	announceIP := flag.String("announce-ip", "", "Public IP to announce in multiaddrs (use on AWS/cloud where the public IP is not bound to the interface)")
	traceFile := flag.String("trace-file", "", "Output file for OTEL traces (JSON); empty = no tracing")
	otlpEndpoint := flag.String("otlp-endpoint", "", "Optional OTLP HTTP endpoint for trace export (e.g. http://jaeger:4318); can be used alongside --trace-file")
	flag.Parse()

	// Initialize OTEL tracing if requested
	if *traceFile != "" || *otlpEndpoint != "" {
		shutdown, err := initTracer(*traceFile, *otlpEndpoint)
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

	// On AWS and other cloud providers the public IP is not bound to the
	// network interface — the OS only sees the private IP (e.g. 172.31.x.x).
	// --announce-ip replaces private IPs in announced multiaddrs with the
	// given public IP so that external peers can actually reach the node.
	if *announceIP != "" {
		pubIP := *announceIP
		log.Printf("Announce IP override: %s (replacing private IPs in multiaddrs)", pubIP)
		opts = append(opts, libp2p.AddrsFactory(func(addrs []ma.Multiaddr) []ma.Multiaddr {
			var out []ma.Multiaddr
			for _, addr := range addrs {
				// Replace /ip4/<private> with /ip4/<pubIP>, keep the rest of the multiaddr.
				rewritten, err := rewriteIP4(addr, pubIP)
				if err == nil {
					out = append(out, rewritten)
				}
			}
			return out
		}))
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

	// Subscribe to events in two separate subscriptions to prevent high-frequency
	// peer events (identification, connectedness) from filling the buffer and
	// dropping important low-frequency events (reachability, NAT type).
	go subscribeImportantEvents(ctx, h, sessionSpan)
	go subscribePeerEvents(ctx, h, sessionSpan)

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
			go bootstrapDHT(ctx, h, *dhtMode, sessionSpan)
		} else if *peerDir != "" {
			go discoverFromDir(ctx, h, *peerDir, sessionSpan)
		}
		if *peers != "" {
			go connectToPeers(ctx, h, *peers, sessionSpan)
		}
		if !*bootstrap && *peerDir == "" && *peers == "" {
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

// rewriteIP4 replaces the /ip4/<any> component of addr with /ip4/<newIP>.
// Returns the original addr unchanged if it has no /ip4 component.
func rewriteIP4(addr ma.Multiaddr, newIP string) (ma.Multiaddr, error) {
	newIPComp, err := ma.NewComponent("ip4", newIP)
	if err != nil {
		return nil, err
	}
	var components []*ma.Component
	replaced := false
	ma.ForEach(addr, func(c ma.Component) bool {
		if c.Protocol().Code == ma.P_IP4 {
			components = append(components, newIPComp)
			replaced = true
		} else {
			cv := c
			components = append(components, &cv)
		}
		return true
	})
	if !replaced {
		return addr, nil
	}
	result := components[0].Multiaddr()
	for _, c := range components[1:] {
		result = result.Encapsulate(c)
	}
	return result, nil
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

// subscribeImportantEvents subscribes to low-frequency but critical events
// (reachability, NAT type, local addresses) with a dedicated subscription so
// that high-frequency peer events cannot overflow the buffer and drop them.
func subscribeImportantEvents(ctx context.Context, h host.Host, sessionSpan trace.Span) {
	sub, err := h.EventBus().Subscribe([]any{
		new(event.EvtLocalReachabilityChanged),
		new(event.EvtHostReachableAddrsChanged),
		new(event.EvtNATDeviceTypeChanged),
		new(event.EvtLocalAddressesUpdated),
		new(event.EvtLocalProtocolsUpdated),
		new(event.EvtAutoRelayAddrsUpdated),
	}, eventbus.BufSize(64))
	if err != nil {
		log.Printf("Failed to subscribe to important events: %v", err)
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
			handleEvent(evt, h, sessionSpan)
		}
	}
}

// subscribePeerEvents subscribes to high-frequency peer lifecycle events.
// These are kept in a separate subscription to prevent flooding the important
// events subscription buffer.
func subscribePeerEvents(ctx context.Context, h host.Host, sessionSpan trace.Span) {
	sub, err := h.EventBus().Subscribe([]any{
		new(event.EvtPeerIdentificationCompleted),
		new(event.EvtPeerIdentificationFailed),
		new(event.EvtPeerConnectednessChanged),
		new(event.EvtPeerProtocolsUpdated),
	}, eventbus.BufSize(256))
	if err != nil {
		log.Printf("Failed to subscribe to peer events: %v", err)
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
			handleEvent(evt, h, sessionSpan)
		}
	}
}

func handleEvent(evt interface{}, h host.Host, sessionSpan trace.Span) {
	switch tevt := evt.(type) {
	case event.EvtLocalReachabilityChanged:
		reachStr := reachabilityString(tevt.Reachability)
		log.Printf("REACHABILITY CHANGED: %s", reachStr)
		addEvent(sessionSpan, "reachability_changed",
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
		addEvent(sessionSpan, "reachable_addrs_changed",
			attribute.StringSlice("reachable", multiaddrsToStrings(tevt.Reachable)),
			attribute.StringSlice("unreachable", multiaddrsToStrings(tevt.Unreachable)),
			attribute.StringSlice("unknown", multiaddrsToStrings(tevt.Unknown)),
		)

	case event.EvtNATDeviceTypeChanged:
		log.Printf("NAT DEVICE TYPE (%s): %s", tevt.TransportProtocol, tevt.NatDeviceType)
		addEvent(sessionSpan, "nat_device_type_changed",
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
		addEvent(sessionSpan, "addresses_updated",
			attribute.StringSlice("current", current),
			attribute.StringSlice("removed", removed),
		)

	case event.EvtPeerIdentificationCompleted:
		peerStats.identified.Add(1)
		addEvent(sessionSpan, "peer_identification_completed",
			attribute.String("peer_id", tevt.Peer.String()),
			attribute.String("observed_addr", tevt.ObservedAddr.String()),
			attribute.StringSlice("protocols", protocolIDsToStrings(tevt.Protocols)),
			attribute.String("agent_version", tevt.AgentVersion),
		)

	case event.EvtPeerIdentificationFailed:
		peerStats.identifyFailed.Add(1)
		addEvent(sessionSpan, "peer_identification_failed",
			attribute.String("peer_id", tevt.Peer.String()),
			attribute.String("reason", tevt.Reason.Error()),
		)

	case event.EvtPeerConnectednessChanged:
		connStr := connectednessString(tevt.Connectedness)
		if tevt.Connectedness == network.Connected {
			peerStats.connected.Add(1)
		} else {
			peerStats.disconnected.Add(1)
		}
		addEvent(sessionSpan, "peer_connectedness_changed",
			attribute.String("peer_id", tevt.Peer.String()),
			attribute.String("connectedness", connStr),
		)

	case event.EvtPeerProtocolsUpdated:
		peerStats.protoUpdated.Add(1)
		addEvent(sessionSpan, "peer_protocols_updated",
			attribute.String("peer_id", tevt.Peer.String()),
			attribute.StringSlice("added", protocolIDsToStrings(tevt.Added)),
			attribute.StringSlice("removed", protocolIDsToStrings(tevt.Removed)),
		)

	case event.EvtLocalProtocolsUpdated:
		log.Printf("LOCAL PROTOCOLS UPDATED: added=%d removed=%d", len(tevt.Added), len(tevt.Removed))
		addEvent(sessionSpan, "local_protocols_updated",
			attribute.StringSlice("added", protocolIDsToStrings(tevt.Added)),
			attribute.StringSlice("removed", protocolIDsToStrings(tevt.Removed)),
		)

	case event.EvtAutoRelayAddrsUpdated:
		log.Printf("AUTO-RELAY ADDRS UPDATED: %d relay addresses", len(tevt.RelayAddrs))
		for _, addr := range tevt.RelayAddrs {
			log.Printf("  RELAY: %s", addr)
		}
		addEvent(sessionSpan, "auto_relay_addrs_updated",
			attribute.StringSlice("relay_addrs", multiaddrsToStrings(tevt.RelayAddrs)),
		)

	default:
		log.Printf("Unknown event: %T", evt)
	}
}

func connectToPeers(ctx context.Context, h host.Host, peersStr string, sessionSpan trace.Span) {
	addrs := strings.Split(peersStr, ",")
	log.Printf("Connecting to %d explicit peer(s)...", len(addrs))
	for _, addrStr := range addrs {
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

func bootstrapDHT(ctx context.Context, h host.Host, dhtMode string, sessionSpan trace.Span) {
	mode := parseDHTMode(dhtMode)
	addEvent(sessionSpan, "bootstrap_start",
		attribute.String("message", "connecting to IPFS DHT bootstrap peers"),
		attribute.String("dht_mode", dhtMode),
	)

	d, err := dht.New(ctx, h, dht.Mode(mode))
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

func connectednessString(c network.Connectedness) string {
	switch c {
	case network.Connected:
		return "connected"
	case network.NotConnected:
		return "not_connected"
	default:
		return fmt.Sprintf("connectedness(%d)", c)
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

// initTracer sets up an OTEL TracerProvider with one or both exporters:
//   - path: write spans as JSONL to a file (empty = skip)
//   - otlpEndpoint: export via OTLP HTTP to a collector/Jaeger (empty = skip)
func initTracer(path string, otlpEndpoint string) (func(), error) {
	var opts []sdktrace.TracerProviderOption
	var closers []func()

	if path != "" {
		f, err := os.Create(path)
		if err != nil {
			return nil, fmt.Errorf("create trace file: %w", err)
		}
		exporter, err := stdouttrace.New(stdouttrace.WithWriter(f))
		if err != nil {
			f.Close()
			return nil, fmt.Errorf("create file exporter: %w", err)
		}
		opts = append(opts, sdktrace.WithBatcher(exporter))
		closers = append(closers, func() { f.Close() })
	}

	if otlpEndpoint != "" {
		ctx := context.Background()
		exporter, err := otlptracehttp.New(ctx,
			otlptracehttp.WithEndpointURL(otlpEndpoint+"/v1/traces"),
			otlptracehttp.WithInsecure(),
		)
		if err != nil {
			return nil, fmt.Errorf("create OTLP exporter: %w", err)
		}
		opts = append(opts, sdktrace.WithBatcher(exporter))
	}

	tp := sdktrace.NewTracerProvider(opts...)
	otel.SetTracerProvider(tp)

	return func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		tp.Shutdown(ctx)
		for _, c := range closers {
			c()
		}
	}, nil
}
