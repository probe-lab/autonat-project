# Upstream Issues to File

Drafts for issues to open on upstream repositories based on confirmed
findings. See [#88](https://github.com/probe-lab/autonat-project/issues/88).

---

## 1. go-libp2p: v2 results not consumed by DHT/AutoRelay

**Repository:** https://github.com/libp2p/go-libp2p
**Finding:** #1 (v1/v2 reachability gap) + #2 (v1 oscillation → DHT)

### Title

AutoNAT v2 results not consumed by DHT, AutoRelay, or Address Manager — v1 oscillation controls subsystems

### Body

**Problem:**
AutoNAT v2 (`EvtHostReachableAddrsChanged`) provides stable, per-address
reachability with 0% FNR/FPR in our testbed. However, the Kademlia DHT
(`ModeAuto`), AutoRelay, Address Manager, and NAT Service all subscribe
to `EvtLocalReachabilityChanged` (v1 only). v2 results are ignored.

Meanwhile, v1 oscillates in 60% of runs when the connected peer pool
includes unreliable servers (peers behind their own restrictive NAT whose
dial-back fails). A reachable node behind full-cone NAT can be reported
as Private by v1 while v2 correctly confirms it as reachable — causing
unnecessary relay activation and DHT client mode.

**Evidence:**
- Testbed: 5 runs with 2 reliable + 5 unreliable servers. v1 oscillates
  public↔private in 3/5 runs. v2 stays stable (0% oscillation).
- Trace: v2 reaches REACHABLE at 6s and never changes. v1 flips to
  PRIVATE at 108s, back to PUBLIC at 183s.
- DHT subscribes to v1 at [subscriber_notifee.go#L30](https://github.com/libp2p/go-libp2p-kad-dht/blob/master/subscriber_notifee.go#L30)
- `EvtHostReachableAddrsChanged` (v2) does NOT appear anywhere in
  go-libp2p-kad-dht

**Impact:**
- Obol Network (Charon): reports oscillating `p2p_reachability_status`
  Prometheus metric, triggering unnecessary relay paths
- Any Kubo node with mixed-quality connected peers

**Proposed fix:**
Add a reduction function in `addrsReachabilityTracker` that emits
`EvtLocalReachabilityChanged` based on v2 results: "PUBLIC if any
v2-confirmed address is reachable, PRIVATE if all unreachable." This
would make all v1 consumers benefit from v2's stability without
changing their subscription code.

**References:**
- [Full analysis](https://github.com/probe-lab/autonat-project/blob/main/docs/v1-v2-reachability-gap.md)
- [v1 vs v2 performance comparison](https://github.com/probe-lab/autonat-project/blob/main/docs/v1-vs-v2-performance.md)
- [Obol impact](https://github.com/probe-lab/autonat-project/blob/main/docs/obol.md)

---

## 2. go-libp2p: UDP black hole detector blocks QUIC dial-back on v2 dialerHost

**Repository:** https://github.com/libp2p/go-libp2p
**Finding:** #5 (UDP black hole blocks QUIC dial-back)

### Title

AutoNAT v2 dialerHost should disable UDP black hole detector (matching v1 fix)

### Body

**Problem:**
The AutoNAT v2 `dialerHost` (created in `makeAutoNATV2Host()`) shares
the main host's `UDPBlackHoleSuccessCounter` in read-only mode. On fresh
servers with zero UDP history, the counter enters Blocked state →
`CanDial()` returns false for QUIC → server refuses QUIC dial-back with
`E_DIAL_REFUSED` → **false negative** for QUIC addresses.

The v1 dialer solved this in [PR #2529](https://github.com/libp2p/go-libp2p/pull/2529)
by setting the counter to nil. The v2 dialerHost should adopt the same
approach.

**Root cause:**
`config.go:makeAutoNATV2Host()` at [line 240](https://github.com/libp2p/go-libp2p/blob/master/config/config.go#L240):
```go
UDPBlackHoleSuccessCounter: cfg.UDPBlackHoleSuccessCounter,  // shared from main host
```

v1 fix at [line 712](https://github.com/libp2p/go-libp2p/blob/master/config/config.go#L712):
```go
swarm.WithUDPBlackHoleSuccessCounter(nil),  // disabled for v1 dialer
```

**Why the detector doesn't belong on the dialerHost:**
AutoNAT dial-backs are one-shot requested operations where the failure
itself is useful information. The detector's purpose (protect against
wasted speculative dials) doesn't apply — the client explicitly asked
to test this address.

**Proposed fix:**
```go
// In makeAutoNATV2Host():
UDPBlackHoleSuccessCounter:        nil,
CustomUDPBlackHoleSuccessCounter:  true,
IPv6BlackHoleSuccessCounter:       nil,
CustomIPv6BlackHoleSuccessCounter: true,
```

**References:**
- [Full analysis with 5 fix options](https://github.com/probe-lab/autonat-project/blob/main/docs/udp-black-hole-detector.md)
- [PR #2529 (v1 fix)](https://github.com/libp2p/go-libp2p/pull/2529)

---

## 3. rust-libp2p: silent PortUse::Reuse fallback causes identify to skip address translation

**Repository:** https://github.com/libp2p/rust-libp2p
**Finding:** #6 (TCP port reuse safety net)

### Title

Identify skips address translation when TCP port reuse silently falls back to ephemeral port

### Body

**Problem:**
When a TCP connection is dialed with `PortUse::Reuse` (the default) but
port reuse fails (e.g., TCP listener not yet registered), the transport
silently falls back to an ephemeral port. The connection metadata still
says `PortUse::Reuse`, so identify's `emit_new_external_addr_candidate_event()`
doesn't apply `_address_translation()` — it only translates for
connections in `outbound_connections_with_ephemeral_port` (populated only
for `PortUse::New`).

Result: `NewExternalAddrCandidate` is emitted with the ephemeral port
instead of the listen port. AutoNAT v2 probes the ephemeral port →
UNREACHABLE (false negative).

**When port reuse works** (listener ready before dialing) or when
**`PortUse::New` is explicit** (identify translates correctly), AutoNAT
v2 produces correct results across all NAT types.

**Testbed evidence:**
- Without timing fix: TCP candidates show ephemeral ports (`/tcp/48168`) → UNREACHABLE
- With timing fix: TCP candidates show listen port (`/tcp/4001`) → REACHABLE
- Port reuse disabled (`allocate_new_port()`): identify translates → `/tcp/4001` → REACHABLE
- QUIC unaffected in all cases (shared UDP socket)

**Proposed fix (option A):**
Identify should check the actual local port rather than trusting
`PortUse` metadata:
```rust
if conn.local_addr().port() != any_listen_port {
    // Apply _address_translation regardless of PortUse
}
```

**Proposed fix (option B):**
TCP transport should update the connection's `PortUse` when reuse fails:
```rust
// In fallback path when bind() fails:
// Update metadata to PortUse::New so identify handles it correctly
```

**References:**
- [Full analysis](https://github.com/probe-lab/autonat-project/blob/main/docs/rust-libp2p-autonat-implementation.md#address-candidate-selection)
- Related: [#4873](https://github.com/libp2p/rust-libp2p/issues/4873) (v1 address ordering)

---

## 4. libp2p/specs: ADF false positive — protocol cannot distinguish address-restricted from full-cone NAT

**Repository:** https://github.com/libp2p/specs
**Finding:** #3 (ADF false positive)

### Title

AutoNAT v2: 100% false positive rate for address-restricted (ADF) NAT — dial-back always from trusted IP

### Body

**Problem:**
AutoNAT v2's dial-back always originates from the server's IP — the same
IP the client previously connected to. For address-restricted NAT (ADF),
the NAT allows inbound from any port of a trusted IP. The dial-back
succeeds, reporting REACHABLE, even though arbitrary peers the client has
never contacted cannot reach it.

The protocol cannot distinguish full-cone (EIF) from address-restricted
(ADF) because both allow the dial-back through.

**Testbed evidence:**
120 runs (60 ADF, 60 APDF control) across TCP, QUIC, and both
transports: ADF produces **100% FPR** deterministically. APDF produces
0% FPR.

**Real-world impact:**
Likely limited — ADF is rare in modern consumer routers (most default to
APDF). RFC 7857 moved recommendations away from ADF. However, no
measurement data exists to quantify ADF prevalence in the IPFS network.

**Note:**
A fix would require dial-back from a different IP than the one the client
contacted (multi-server verification), which is a significant protocol
change. This is documented for awareness — a fix may not be warranted
given ADF's rarity.

**References:**
- [Full analysis](https://github.com/probe-lab/autonat-project/blob/main/docs/adf-false-positive.md)
- [RFC 4787](https://www.rfc-editor.org/rfc/rfc4787) — NAT filtering taxonomy
