# go-libp2p AutoNAT v2 Implementation

Implementation details for the AutoNAT v2 subsystem in
[go-libp2p](https://github.com/libp2p/go-libp2p). For the protocol
walkthrough, see [autonat-v2.md](autonat-v2.md).

Based on go-libp2p **v0.47.0** (2026-01-25).

---

## Source Layout

```
p2p/protocol/autonatv2/
  autonat.go        AutoNAT orchestrator: server discovery, peer throttling, GetReachability()
  client.go         Client-side probe: DialRequest, dial-back handler, address verification
  server.go         Server-side handler: rate limiting, address selection, dial-back
  pb/               Protobuf definitions and generated code

p2p/host/basic/
  addrs_reachability_tracker.go   Address state machine: confidence, primary/secondary,
                                  re-probe scheduling, refresh cycles

p2p/host/observedaddrs/
  manager.go        ObservedAddrManager: collects observed addresses, activation threshold

config/
  config.go         makeAutoNATV2Host(): creates isolated dialer host for server-side dial-back
```

---

## Key Structs

### `autonatv2.AutoNAT` (autonat.go)

The top-level orchestrator. Maintains the set of eligible servers, handles
peer throttling, and delegates to the client for actual probes.

```go
type AutoNAT struct {
    host                 host.Host
    srv                  *server              // server-side handler (if enabled)
    cli                  *client              // client-side probe logic
    peers                *peersMap            // connected peers supporting dial-request
    throttlePeer         map[peer.ID]time.Time // per-peer cooldown
    throttlePeerDuration time.Duration         // default: 2 minutes
    allowPrivateAddrs    bool
}
```

### `autonatv2.client` (client.go)

Handles the client side of a single probe: opens a stream, sends
DialRequest, waits for response and dial-back, verifies nonce.

```go
type Request struct {
    Addr         ma.Multiaddr  // address to verify
    SendDialData bool          // accept amplification cost for this address
}

type Result struct {
    Addr            ma.Multiaddr
    Idx             int
    Reachability    network.Reachability  // Public, Private, or Unknown
    AllAddrsRefused bool                  // true when server refused all addrs
}
```

### `addrsReachabilityTracker` (addrs_reachability_tracker.go)

Manages per-address probe state, confidence tracking, primary/secondary
grouping, and periodic refresh cycles.

```go
type addrsReachabilityTracker struct {
    client              autonatv2Client       // interface to AutoNAT.GetReachability()
    reachabilityUpdateCh chan struct{}         // notified on state change
    maxConcurrency      int                   // concurrent probe workers (default: 5)
    newAddrsProbeDelay  time.Duration         // delay before first probe (1s)
    probeManager        *probeManager         // per-address state machine
    reachableAddrs      []ma.Multiaddr
    unreachableAddrs    []ma.Multiaddr
    unknownAddrs        []ma.Multiaddr
}
```

### `autonatv2.server` (server.go)

Handles incoming dial-request streams, rate limiting, and dial-back
execution.

```go
type server struct {
    host                   host.Host
    dialerHost             host.Host          // separate host for dial-back (different peer ID)
    limiter                *rateLimiter
    dialDataRequestPolicy  dataRequestPolicyFunc
    amplificatonAttackPreventionDialWait time.Duration  // max random delay before dial-back
}
```

---

## Constants Reference

### Client (client.go)

| Constant | Value | Description |
|----------|-------|-------------|
| `dialBackStreamTimeout` | 5s | Max wait for dial-back stream after DialResponse |
| `dialBackDialTimeout` | 10s | Server's connection timeout when dialing back |
| `minHandshakeSizeBytes` | 30,000 | Minimum amplification data bytes |
| `maxHandshakeSizeBytes` | 100,000 | Maximum amplification data bytes |
| `maxMsgSize` | 8,192 | Max message size on dial-request stream |
| `maxPeerAddresses` | 50 | Max addresses the server will inspect |

### Orchestrator (autonat.go)

| Constant | Value | Description |
|----------|-------|-------------|
| `streamTimeout` | 15s | Deadline for entire dial-request stream |
| `defaultThrottlePeerDuration` | 2 min | Per-peer cooldown between probes |

### Server (server.go)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `serverRPM` | 60 | Global requests per minute |
| `serverPerPeerRPM` | 12 | Per-peer requests per minute |
| `serverDialDataRPM` | 12 | Dial-data requests per minute |
| `maxConcurrentRequestsPerPeer` | 2 | Max simultaneous requests from one peer |

Rate limits use a 1-minute sliding window. The global rate limit is checked
before reading the request; dial-data rate limit is checked separately after
determining data is needed.

### Confidence System (addrs_reachability_tracker.go)

| Constant | Value | Description |
|----------|-------|-------------|
| `targetConfidence` | 3 | Net successes/failures for high confidence |
| `minConfidence` | 2 | Minimum net difference for definitive state |
| `maxRecentDialsWindow` | 5 | Sliding window of recent probe outcomes |
| `maxConsecutiveRefusals` | 5 | Refusals before pausing probes |
| `maxAddrsPerRequest` | 10 | Addresses per single probe request |
| `maxTrackedAddrs` | 50 | Max addresses tracked (10 per transport x 5) |
| `defaultMaxConcurrency` | 5 | Concurrent probe workers |

### Re-probe Intervals

| Interval | Value | Trigger |
|----------|-------|---------|
| `newAddrsProbeDelay` | 1s | New address detected |
| `defaultReachabilityRefreshInterval` | 5 min | Periodic refresh ticker |
| `highConfidenceAddrProbeInterval` | 1 hour | Primary address re-probe |
| `highConfidenceSecondaryAddrProbeInterval` | 3 hours | Secondary address re-probe |
| `maxProbeResultTTL` | 5 hours | Oldest result before forced re-probe |
| `backoffStartInterval` | 5s | Initial backoff on errors |
| `maxBackoffInterval` | 5 min | Maximum backoff interval |
| Consecutive refusal pause | 10 min | After `maxConsecutiveRefusals` |

---

## Confidence System

The `probeManager` maintains a per-address sliding window of the last 5 probe
outcomes (success or failure). Rejected and refused results are not recorded.

**State determination:**

```
confidence = successes - failures   (within the sliding window)

if confidence >= minConfidence (2):  → Public
if confidence <= -minConfidence (-2): → Private
otherwise:                           → Unknown (keep probing)
```

**High confidence** is reached at `targetConfidence = 3` net successes or
failures. This triggers the longer re-probe interval (1h primary, 3h
secondary).

**Probe count calculation:** The `probeManager.RequiredProbeCount()` method
determines how many probes an address needs:

1. If secondary and primary is Public → **0** (inherit)
2. If `maxConsecutiveRefusals` exceeded and timeout not elapsed → **0** (pause)
3. If confidence < targetConfidence → **targetConfidence - |confidence|**
4. If high-confidence result is stale (>1h primary, >3h secondary) → **1**
5. If last probe disagrees with confirmed reachability → **1** (re-verify)
6. Otherwise → **0**

---

## Observed Address Manager

**Location:** `p2p/host/observedaddrs/manager.go`

The `ObservedAddrManager` collects external addresses reported by peers via
Identify and decides when to "activate" them (add to the host's advertised
address list).

### Activation Rules

- `ActivationThresh = 4` — an observed address must be reported by at least
  4 distinct observers before activation
- **IPv4**: each distinct IP counts as a separate observer (no subnet grouping)
- **IPv6**: all IPs in the same `/56` prefix count as one observer
- Maximum 3 external addresses per local (listen) address

`ActivationThresh` is a package-level `var` (not `const`), overridable:

```go
import "github.com/libp2p/go-libp2p/p2p/host/observedaddrs"
observedaddrs.ActivationThresh = 2
```

### Impact on AutoNAT v2

The activation threshold is the gate that determines whether AutoNAT v2
runs at all:

- **EIM NATs** (full cone, address-restricted, port-restricted): all peers
  see the same external IP:port → reaches threshold after 4 connections
- **ADPM NATs** (symmetric): each peer sees a different external port → no
  address ever reaches the threshold → AutoNAT v2 never runs

The testbed overrides this threshold (via `OBS_ADDR_THRESH` env var) to allow
probing with fewer servers. The default of 4 requires at least 4 servers with
distinct IPs.

---

## Server Discovery and Selection

### Discovery (autonat.go `background()`)

The AutoNAT orchestrator subscribes to three events:

1. `EvtPeerIdentificationCompleted` — peer just completed Identify, check if
   it supports `/libp2p/autonat/2/dial-request`
2. `EvtPeerProtocolsUpdated` — peer's protocol list changed
3. `EvtPeerConnectednessChanged` — peer connected or disconnected

A peer is eligible when it is **connected** AND supports the dial-request
protocol. Disconnected peers are immediately removed.

### Selection (autonat.go `GetReachability()`)

1. Filter addresses to only public ones (`manet.IsPublicAddr()`)
2. Shuffle eligible peers using a random-start iterator (`peersMap.Shuffled()`)
3. Skip peers whose throttle timer hasn't expired (2-minute cooldown)
4. Select the first unthrottled peer and mark it as throttled
5. If no peer is available → return `ErrNoPeers`

The `peersMap` uses a slice + index map for O(1) random access, avoiding
Go's map iteration bias.

### Public Address Filtering

`manet.IsPublicAddr()` rejects the following ranges (relevant subset):

- **Private**: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`
- **CGNAT**: `100.64.0.0/10`
- **Link-local**: `169.254.0.0/16`
- **Unroutable**: `192.0.0.0/26`, `192.0.2.0/24`, `198.18.0.0/15`, `203.0.113.0/24`

Docker testbed subnets must use "real" public IP ranges (e.g., `73.0.0.0/24`)
for addresses to pass this filter.

---

## Server-Side Dial-Back

### Dialer Host (config.go `makeAutoNATV2Host()`)

The server uses a separate `dialerHost` for dial-backs:

- **Separate Ed25519 key** → different peer ID from the server
- **Fresh peerstore** → no reuse of existing connections
- **`network.WithForceDirectDial`** → prevents relay usage
- **`swarm.WithReadOnlyBlackHoleDetector`** → failed dials don't corrupt
  the server's black hole detection state
- **`swarm.NoDelayDialRanker`** → faster dials (no ranking delay)

### Amplification Prevention (server.go)

The `amplificationAttackPrevention` policy:

1. Extract IP from the client's observed address (stream connection remote addr)
2. Extract IP from the address to dial
3. If IPs differ → require dial data (30-100KB)
4. If IP extraction fails (DNS addresses) → require dial data

After receiving data, the server waits `rand(0, 3s)` before dialing
(anti-thundering-herd).

### Dial-Back Sequence (server.go `dialBack()`)

1. Set deadline to `dialBackDialTimeout` (10s)
2. Add client address to peerstore with `TempAddrTTL`
3. Connect via `dialerHost.Connect()`
4. Open `/libp2p/autonat/2/dial-back` stream
5. Write `DialBack{nonce}`
6. `CloseWrite()` — signal end of writing
7. `Read(1 byte)` with 5s deadline — wait for client's `DialBackResponse`
8. Close connection: `ClosePeer()` + `ClearAddrs()` + `RemovePeer()`

The `CloseWrite()` then `Read()` pattern ensures the nonce message is
flushed to the client before the connection is torn down.

---

## Primary / Secondary Address Grouping

Addresses are grouped by **thin waist** — the IP + transport port combination.
Within each group, addresses are scored by transport complexity:

| Transport | Score |
|-----------|-------|
| TCP | 1 |
| QUIC-v1 | 1 |
| WebTransport | 2 |
| WebRTC | 4 |
| WS / WSS | 8 |
| Unknown | 2^20 |

The lowest-scoring address in each group is **primary**; all others are
**secondary**.

**Why this matters**: If the primary address is confirmed Public, all
secondary addresses in the same group inherit Public status without being
probed. The rationale: if the port is network-reachable, protocol-level
failures on secondary transports typically mean the probing peer doesn't
support that transport, not that the address is unreachable.

This reduces probing load when a node listens on multiple transports on the
same port (e.g., TCP + WebSocket, or QUIC + WebTransport).

---

## Address Consistency Check

The client verifies dial-back connections via `areAddrsConsistent()` (client.go):

1. Strip trailing `/p2p/...` and `/certhash/...` components
2. Convert `/wss` to `/tls/ws` and strip `/sni/...`
3. Compare protocol sequences component by component:
   - **First component** (IP/DNS): DNS/DNSADDR can match IP4/IP6 (the server
     may have resolved DNS to IP); DNS4 matches IP4; DNS6 matches IP6
   - **Other components**: must match exactly
4. Lengths must match after stripping

This handles real-world scenarios where the tested address uses DNS but the
dial-back connection reports the resolved IP, or where certificate hashes
change between address advertisement and dial-back.

---

## Black Hole Detector

The black hole detector (`p2p/net/swarm/black_hole_detector.go`) tracks
UDP/QUIC and IPv6 connection success rates using a sliding window. When the
success counter enters `Blocked` state (too few successes),
`filterKnownUndialables` returns "dial refused because of black hole" and
the swarm refuses to dial. This protects nodes from wasting resources on
networks that silently drop UDP traffic.

The `dialerHost` shares the main host's counter in read-only mode. On fresh
servers with no UDP history, this causes QUIC dial-backs to be refused. See
[UDP Black Hole Detector and AutoNAT v2](udp-black-hole-detector.md) for the
full analysis, testbed workaround, and upstream fix proposal.

---

## Refresh Cycle

The `addrsReachabilityTracker.background()` goroutine runs the main loop:

1. **Wait** for refresh ticker (5 min) or new address notification
2. **Collect** all addresses needing probes (via `RequiredProbeCount()`)
3. **Dispatch** up to `maxConcurrency` (5) concurrent probe workers
4. Each worker calls `AutoNAT.GetReachability()` for its assigned address
5. **Record** results in the per-address sliding window
6. If any address changed state → notify `reachabilityUpdateCh`
7. **Backoff** on errors: 5s → 10s → 20s → ... → 5min (exponential, capped)

The refresh can be interrupted by new addresses (detected via
`EvtLocalAddressesUpdated`). New addresses trigger a probe after a 1-second
delay.

---

## Spec Compliance

| Spec requirement | go-libp2p status |
|---|---|
| Client sends `DialRequest{addrs, nonce}` | Matches |
| Server picks first dialable address in order | Matches |
| Server SHOULD NOT dial private addresses | Matches (`manet.IsPublicAddr` check) |
| Client SHOULD NOT send private addresses | Matches (filtered in `GetReachability`) |
| Amplification prevention when IP differs | Matches |
| Dial data: 30k-100k bytes | Matches |
| Dial data chunks max 4096 bytes | Matches (client uses 4000-byte buffer) |
| Min 100 bytes per chunk | Matches (`readDialData` rejects < 100) |
| Server uses separate peer ID for dial-back | Matches (fresh Ed25519 key) |
| Nonce verification by client | Matches |
| Client SHOULD NOT verify peer ID on dial-back | Matches |
| Servers SHOULD NOT reuse listening port | Matches (separate dialer host) |

### Implementation-Specific Behavior (Not in Spec)

| Feature | Description |
|---------|-------------|
| Address consistency check | Verifies dial-back local address matches tested address with DNS/transport normalization |
| `E_DIAL_BACK_ERROR` → Public | Treats stream-level failure as Public since network connection succeeded |
| `SendDialData` per-address | Each `Request` has a boolean controlling whether amplification cost is accepted |
| Anti-thundering-herd delay | Server waits random `[0, 3s]` after receiving dial data |
| Max message sizes | `maxMsgSize=8192` (dial-request), `dialBackMaxMsgSize=1024` (dial-back) |
| Confidence system | Sliding window with `minConfidence=2`, `targetConfidence=3`, primary/secondary grouping |

---

## Notable Bug Fixes

| Version | Date | Fix |
|---------|------|-----|
| v0.41.1 | 2025-03-24 | Amplification policy compared wrong addresses (client observed IP vs server's own local IP instead of dial target) |
| v0.41.1 | 2025-03-24 | DNS addresses not handled (`manet.ToIP()` failed silently, always triggering dial data) |
| v0.44.0 | 2025-10-07 | WebSocket normalization (`/wss` not normalized to `/tls/ws` for address consistency) |

---

## Implementation History

### go-libp2p

| Version | Date | Changes |
|---------|------|---------|
| v0.34.0 | 2024-05-20 | Initial autonatv2 implementation |
| v0.37.0 | 2024-10-22 | Panic recovery added |
| v0.40.0 | 2025-02-17 | Multiple concurrent requests per peer (default 2) |
| v0.41.1 | 2025-03-24 | Critical bug fixes: amplification policy + DNS handling |
| v0.42.0 | 2025-06-18 | `addrsReachabilityTracker` — autonatv2 becomes primary reachability mechanism; metrics |
| v0.43.0 | 2025-08-07 | Migrated to log/slog |
| v0.44.0 | 2025-10-07 | WebSocket normalization fix; removed webrtc/webtransport dependency |
| v0.47.0 | 2026-01-25 | Latest stable release |

The protobuf wire format has not changed since the initial implementation.

### rust-libp2p

| Crate version | libp2p version | Date | Changes |
|---------------|---------------|------|---------|
| libp2p-autonat 0.13.0 | v0.54.1 | 2024-08-19 | Initial autonatv2 ([PR #5526](https://github.com/libp2p/rust-libp2p/pull/5526)) |
| libp2p-autonat 0.14.0 | v0.55.0 | 2025-01-15 | Verify dial from connected peer; deprecate `void` crate |
| libp2p-autonat 0.15.0 | v0.56.0 | 2025-06-27 | Fix infinite loop on wrong nonce ([PR #5848](https://github.com/libp2p/rust-libp2p/pull/5848)) |

### js-libp2p

| Package version | Date | Changes |
|-----------------|------|---------|
| @libp2p/autonat-v2 1.0.0 | 2025-06-25 | Initial autonatv2 ([PR #3196](https://github.com/libp2p/js-libp2p/pull/3196)) |
| @libp2p/autonat-v2 2.0.0 | 2025-09-03 | Streams as EventTargets (breaking API change) |
| @libp2p/autonat-v2 2.0.10 | 2026-01-16 | Latest release |

### Timeline

| Date | Milestone |
|------|-----------|
| 2024-05-20 | **go-libp2p** ships autonatv2 (v0.34.0) — first implementation |
| 2024-08-19 | **rust-libp2p** ships autonatv2 (v0.54.1) |
| 2025-03-24 | go-libp2p critical bug fixes (v0.41.1) |
| 2025-06-18 | go-libp2p makes autonatv2 primary reachability mechanism (v0.42.0) |
| 2025-06-25 | **js-libp2p** ships autonatv2 |

---

## References

- [AutoNAT v2 Protocol Walkthrough](autonat-v2.md)
- [AutoNAT v2 Specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
- [go-libp2p autonatv2 source](https://github.com/libp2p/go-libp2p/tree/master/p2p/protocol/autonatv2)
- [go-libp2p address reachability tracker](https://github.com/libp2p/go-libp2p/blob/master/p2p/host/basic/addrs_reachability_tracker.go)
- [go-libp2p observed address manager](https://github.com/libp2p/go-libp2p/blob/master/p2p/host/observedaddrs/manager.go)
- [rust-libp2p autonatv2 source](https://github.com/libp2p/rust-libp2p/tree/master/protocols/autonat/src/v2)
- [js-libp2p autonatv2 source](https://github.com/libp2p/js-libp2p/tree/main/packages/protocol-autonat-v2)
- [Amplification attack analysis (issue #640)](https://github.com/libp2p/specs/issues/640)
