package main

import (
	"context"
	"fmt"
	"log"
	"math/rand"
	"time"

	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/libp2p/go-libp2p/core/peerstore"
	"github.com/libp2p/go-libp2p/p2p/protocol/autonatv2/pb"
	ma "github.com/multiformats/go-multiaddr"

	"github.com/libp2p/go-msgio/pbio"
)

const (
	// Protocol IDs (same as go-libp2p's autonatv2 package).
	mockDialProtocol     = "/libp2p/autonat/2/dial-request"
	mockDialBackProtocol = "/libp2p/autonat/2/dial-back"
	mockMaxMsgSize       = 8192
)

// MockBehavior controls how the mock server responds to AutoNAT v2 dial requests.
type MockBehavior int

const (
	// Category A — response only, no dial-back needed.
	BehaviorReject          MockBehavior = iota // E_REQUEST_REJECTED
	BehaviorRefuse                              // E_DIAL_REFUSED
	BehaviorForceUnreachable                    // OK + E_DIAL_ERROR
	BehaviorInternalError                       // E_INTERNAL_ERROR
	BehaviorTimeout                             // read request, never respond

	// Category B — requires actual dial-back.
	BehaviorForceReachable // dial back with correct nonce, respond OK + OK
	BehaviorWrongNonce     // dial back with nonce-1
	BehaviorNoDialbackMsg  // connect but don't send DialBack message

	// Category C — probabilistic.
	BehaviorProbabilistic // randomly force-reachable or force-unreachable per request
)

func parseBehavior(s string) (MockBehavior, error) {
	switch s {
	case "reject":
		return BehaviorReject, nil
	case "refuse":
		return BehaviorRefuse, nil
	case "force-unreachable":
		return BehaviorForceUnreachable, nil
	case "internal-error":
		return BehaviorInternalError, nil
	case "timeout":
		return BehaviorTimeout, nil
	case "force-reachable":
		return BehaviorForceReachable, nil
	case "wrong-nonce":
		return BehaviorWrongNonce, nil
	case "no-dialback-msg":
		return BehaviorNoDialbackMsg, nil
	case "probabilistic":
		return BehaviorProbabilistic, nil
	case "actual":
		return BehaviorForceReachable, nil // alias: real dial-back, reports actual result
	default:
		return 0, fmt.Errorf("unknown mock behavior: %q (valid: reject, refuse, force-unreachable, internal-error, timeout, force-reachable, wrong-nonce, no-dialback-msg, probabilistic, actual)", s)
	}
}

func (b MockBehavior) String() string {
	switch b {
	case BehaviorReject:
		return "reject"
	case BehaviorRefuse:
		return "refuse"
	case BehaviorForceUnreachable:
		return "force-unreachable"
	case BehaviorInternalError:
		return "internal-error"
	case BehaviorTimeout:
		return "timeout"
	case BehaviorForceReachable:
		return "force-reachable"
	case BehaviorWrongNonce:
		return "wrong-nonce"
	case BehaviorNoDialbackMsg:
		return "no-dialback-msg"
	case BehaviorProbabilistic:
		return "probabilistic"
	default:
		return fmt.Sprintf("unknown(%d)", int(b))
	}
}

// needsDialBack returns true for behaviors that require a dialerHost.
func (b MockBehavior) needsDialBack() bool {
	return b == BehaviorForceReachable || b == BehaviorWrongNonce || b == BehaviorNoDialbackMsg || b == BehaviorProbabilistic
}

// MockServerConfig holds all configuration for a MockServer.
type MockServerConfig struct {
	Behavior     MockBehavior
	Delay        time.Duration
	Jitter       time.Duration // random [0, Jitter) added to Delay per request
	Probability  float64       // for BehaviorProbabilistic: P(force-reachable); default 0.5
	TCPBehavior  *MockBehavior // behavior override for TCP addresses; nil = use Behavior
	QUICBehavior *MockBehavior // behavior override for QUIC addresses; nil = use Behavior
}

// needsDialBack returns true if any configured behavior requires a dialerHost.
func (cfg MockServerConfig) needsDialBack() bool {
	if cfg.Behavior.needsDialBack() {
		return true
	}
	if cfg.TCPBehavior != nil && cfg.TCPBehavior.needsDialBack() {
		return true
	}
	if cfg.QUICBehavior != nil && cfg.QUICBehavior.needsDialBack() {
		return true
	}
	return false
}

// MockServer is a controllable AutoNAT v2 server that generates specific
// protobuf responses without using go-libp2p's built-in AutoNAT implementation.
type MockServer struct {
	host       host.Host
	dialerHost host.Host // nil when no behavior requires dial-back
	cfg        MockServerConfig
}

// startMockServer creates and starts a mock AutoNAT v2 server.
// It registers a stream handler for the dial-request protocol, making the
// server visible to clients via Identify as a valid AutoNAT v2 server.
func startMockServer(listenAddrs []string, cfg MockServerConfig) (*MockServer, error) {
	// Create host WITHOUT EnableAutoNATv2() to avoid the built-in handler.
	h, err := libp2p.New(
		libp2p.ListenAddrStrings(listenAddrs...),
		libp2p.EnableNATService(),
		libp2p.NATPortMap(),
	)
	if err != nil {
		return nil, fmt.Errorf("creating mock server host: %w", err)
	}

	ms := &MockServer{host: h, cfg: cfg}

	// For behaviors that require dial-back, create a separate dialerHost with
	// a different peer ID (same pattern as go-libp2p's server).
	if cfg.needsDialBack() {
		dialerHost, err := libp2p.New(
			libp2p.NoListenAddrs,
			libp2p.UDPBlackHoleSuccessCounter(nil),
			libp2p.IPv6BlackHoleSuccessCounter(nil),
		)
		if err != nil {
			h.Close()
			return nil, fmt.Errorf("creating dialer host: %w", err)
		}
		ms.dialerHost = dialerHost
	}

	h.SetStreamHandler(mockDialProtocol, ms.handleDialRequest)
	log.Printf("Mock server registered stream handler for %s", mockDialProtocol)

	return ms, nil
}

// isTCPAddr reports whether addr uses TCP as the transport.
func isTCPAddr(addr ma.Multiaddr) bool {
	_, err := addr.ValueForProtocol(ma.P_TCP)
	return err == nil
}

// isQUICAddr reports whether addr uses QUIC or QUICv1 as the transport.
func isQUICAddr(addr ma.Multiaddr) bool {
	_, err1 := addr.ValueForProtocol(ma.P_QUIC_V1)
	_, err2 := addr.ValueForProtocol(ma.P_QUIC)
	return err1 == nil || err2 == nil
}

// determineBehavior resolves the effective behavior for a given probed address,
// applying per-transport overrides and probabilistic selection.
func (ms *MockServer) determineBehavior(addr ma.Multiaddr) MockBehavior {
	if addr != nil {
		if ms.cfg.TCPBehavior != nil && isTCPAddr(addr) {
			return *ms.cfg.TCPBehavior
		}
		if ms.cfg.QUICBehavior != nil && isQUICAddr(addr) {
			return *ms.cfg.QUICBehavior
		}
	}
	if ms.cfg.Behavior == BehaviorProbabilistic {
		p := ms.cfg.Probability
		if p <= 0 {
			p = 0.5
		}
		if rand.Float64() < p {
			return BehaviorForceReachable
		}
		return BehaviorForceUnreachable
	}
	return ms.cfg.Behavior
}

// Close shuts down the mock server and its dialer host.
func (ms *MockServer) Close() error {
	if ms.dialerHost != nil {
		ms.dialerHost.Close()
	}
	return ms.host.Close()
}

// Host returns the main host (for writing addr files, etc.).
func (ms *MockServer) Host() host.Host {
	return ms.host
}

func (ms *MockServer) handleDialRequest(s network.Stream) {
	defer s.Close()

	remotePeer := s.Conn().RemotePeer()
	log.Printf("Mock server: received dial request from %s (behavior=%s)", remotePeer.ShortString(), ms.cfg.Behavior)

	r := pbio.NewDelimitedReader(s, mockMaxMsgSize)
	w := pbio.NewDelimitedWriter(s)

	// Read the DialRequest message.
	var msg pb.Message
	if err := r.ReadMsg(&msg); err != nil {
		log.Printf("Mock server: failed to read dial request: %v", err)
		return
	}

	req := msg.GetDialRequest()
	if req == nil {
		log.Printf("Mock server: message has no dial request")
		return
	}

	nonce := req.GetNonce()
	log.Printf("Mock server: nonce=%d, %d addresses", nonce, len(req.GetAddrs()))

	// Parse the first address for dial-back.
	var firstAddr ma.Multiaddr
	if len(req.GetAddrs()) > 0 {
		var err error
		firstAddr, err = ma.NewMultiaddrBytes(req.GetAddrs()[0])
		if err != nil {
			log.Printf("Mock server: failed to parse first address: %v", err)
		} else {
			log.Printf("Mock server: first address: %s", firstAddr)
		}
	}

	// Apply delay + jitter if configured.
	actualDelay := ms.cfg.Delay
	if ms.cfg.Jitter > 0 {
		actualDelay += time.Duration(rand.Int63n(int64(ms.cfg.Jitter)))
	}
	if actualDelay > 0 {
		log.Printf("Mock server: delaying %s before responding", actualDelay)
		time.Sleep(actualDelay)
	}

	behavior := ms.determineBehavior(firstAddr)
	log.Printf("Mock server: effective behavior=%s", behavior)
	switch behavior {
	case BehaviorReject:
		ms.sendResponse(w, pb.DialResponse_E_REQUEST_REJECTED, 0, pb.DialStatus_UNUSED)

	case BehaviorRefuse:
		ms.sendResponse(w, pb.DialResponse_E_DIAL_REFUSED, 0, pb.DialStatus_UNUSED)

	case BehaviorForceUnreachable:
		ms.sendResponse(w, pb.DialResponse_OK, 0, pb.DialStatus_E_DIAL_ERROR)

	case BehaviorInternalError:
		ms.sendResponse(w, pb.DialResponse_E_INTERNAL_ERROR, 0, pb.DialStatus_UNUSED)

	case BehaviorTimeout:
		log.Printf("Mock server: timeout behavior — hanging indefinitely")
		// Block until the stream is reset/closed by the client.
		buf := make([]byte, 1)
		s.Read(buf)

	case BehaviorForceReachable:
		if firstAddr == nil {
			log.Printf("Mock server: no valid address for dial-back, sending E_DIAL_ERROR")
			ms.sendResponse(w, pb.DialResponse_OK, 0, pb.DialStatus_E_DIAL_ERROR)
			return
		}
		dialStatus := ms.performDialBack(remotePeer, firstAddr, nonce)
		ms.sendResponse(w, pb.DialResponse_OK, 0, dialStatus)

	case BehaviorWrongNonce:
		if firstAddr == nil {
			log.Printf("Mock server: no valid address for dial-back, sending E_DIAL_ERROR")
			ms.sendResponse(w, pb.DialResponse_OK, 0, pb.DialStatus_E_DIAL_ERROR)
			return
		}
		// Dial back with nonce-1 so the client rejects it.
		dialStatus := ms.performDialBack(remotePeer, firstAddr, nonce-1)
		ms.sendResponse(w, pb.DialResponse_OK, 0, dialStatus)

	case BehaviorNoDialbackMsg:
		if firstAddr == nil {
			log.Printf("Mock server: no valid address for dial-back, sending E_DIAL_ERROR")
			ms.sendResponse(w, pb.DialResponse_OK, 0, pb.DialStatus_E_DIAL_ERROR)
			return
		}
		// Connect but don't send the DialBack message — client will timeout.
		ms.performConnectOnly(remotePeer, firstAddr)
		ms.sendResponse(w, pb.DialResponse_OK, 0, pb.DialStatus_OK)
	}
}

func (ms *MockServer) sendResponse(w pbio.WriteCloser, status pb.DialResponse_ResponseStatus, addrIdx uint32, dialStatus pb.DialStatus) {
	resp := &pb.Message{
		Msg: &pb.Message_DialResponse{
			DialResponse: &pb.DialResponse{
				Status:     status,
				AddrIdx:    addrIdx,
				DialStatus: dialStatus,
			},
		},
	}
	if err := w.WriteMsg(resp); err != nil {
		log.Printf("Mock server: failed to write response: %v", err)
	} else {
		log.Printf("Mock server: sent response status=%v dialStatus=%v", status, dialStatus)
	}
}

// performDialBack connects to the remote peer on the given address and sends
// a DialBack message with the specified nonce. This follows the same pattern
// as go-libp2p's server.dialBack().
func (ms *MockServer) performDialBack(remotePeer peer.ID, addr ma.Multiaddr, nonce uint64) pb.DialStatus {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	ctx = network.WithForceDirectDial(ctx, "mock-autonat")
	ms.dialerHost.Peerstore().AddAddr(remotePeer, addr, peerstore.TempAddrTTL)
	defer func() {
		ms.dialerHost.Network().ClosePeer(remotePeer)
		ms.dialerHost.Peerstore().ClearAddrs(remotePeer)
		ms.dialerHost.Peerstore().RemovePeer(remotePeer)
	}()

	if err := ms.dialerHost.Connect(ctx, peer.AddrInfo{ID: remotePeer}); err != nil {
		log.Printf("Mock server: dial-back connect failed: %v", err)
		return pb.DialStatus_E_DIAL_ERROR
	}

	s, err := ms.dialerHost.NewStream(ctx, remotePeer, mockDialBackProtocol)
	if err != nil {
		log.Printf("Mock server: dial-back stream failed: %v", err)
		return pb.DialStatus_E_DIAL_BACK_ERROR
	}
	defer s.Close()

	w := pbio.NewDelimitedWriter(s)
	if err := w.WriteMsg(&pb.DialBack{Nonce: nonce}); err != nil {
		log.Printf("Mock server: dial-back write failed: %v", err)
		s.Reset()
		return pb.DialStatus_E_DIAL_BACK_ERROR
	}

	// Ensure message delivery: CloseWrite + read a byte (same pattern as go-libp2p).
	s.CloseWrite()
	buf := make([]byte, 1)
	s.Read(buf)

	log.Printf("Mock server: dial-back completed (nonce=%d)", nonce)
	return pb.DialStatus_OK
}

// performConnectOnly connects to the remote peer but does NOT send the DialBack
// message. The client will timeout waiting for the nonce verification.
func (ms *MockServer) performConnectOnly(remotePeer peer.ID, addr ma.Multiaddr) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	ctx = network.WithForceDirectDial(ctx, "mock-autonat")
	ms.dialerHost.Peerstore().AddAddr(remotePeer, addr, peerstore.TempAddrTTL)
	defer func() {
		ms.dialerHost.Network().ClosePeer(remotePeer)
		ms.dialerHost.Peerstore().ClearAddrs(remotePeer)
		ms.dialerHost.Peerstore().RemovePeer(remotePeer)
	}()

	if err := ms.dialerHost.Connect(ctx, peer.AddrInfo{ID: remotePeer}); err != nil {
		log.Printf("Mock server: connect-only failed: %v", err)
		return
	}

	s, err := ms.dialerHost.NewStream(ctx, remotePeer, mockDialBackProtocol)
	if err != nil {
		log.Printf("Mock server: connect-only stream failed: %v", err)
		return
	}

	// Hold the stream open for a bit so the client can see the connection,
	// but never send a DialBack message. Then close.
	time.Sleep(2 * time.Second)
	s.Close()
	log.Printf("Mock server: connect-only completed (no DialBack sent)")
}
