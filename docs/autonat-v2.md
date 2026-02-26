# AutoNAT v2 Protocol Reference

This document describes the AutoNAT v2 protocol in detail, covering the protocol
flow, message format, implementation behavior in go-libp2p, and all relevant
constants and thresholds.

## Purpose

AutoNAT v2 enables a libp2p node to determine whether its **individual
addresses** are publicly reachable. A node cannot know a priori whether it sits
behind a NAT or firewall. Knowing per-address reachability allows the node to:

- Avoid advertising unreachable addresses to the network
- Proactively connect to relay servers when no public address is reachable
- Test addresses from multiple sources (listen addrs, UPnP, observed addrs)

## Key Differences from AutoNAT v1

| Aspect | v1 | v2 |
|--------|----|----|
| Granularity | Tests the node as a whole | Tests individual addresses |
| Verification | None — trusts server's report | Nonce-based dial-back verification |
| Cross-IP testing | Forbidden (must match observed IP) | Allowed with amplification cost |
| Protocol ID | `/libp2p/autonat/1.0.0` | `/libp2p/autonat/2/dial-request` + `/libp2p/autonat/2/dial-back` |

## Protocol Flow

### Happy Path (same IP — no amplification cost)

```
Client                                          Server
  |                                                |
  |  [1] Open stream: /libp2p/autonat/2/dial-request
  |------ DialRequest{nonce, addrs[]} ------------>|
  |                                                |
  |        Server selects first dialable addr (i)  |
  |        addr[i] IP == client's observed IP      |
  |        → skip amplification prevention         |
  |                                                |
  |  [2] Server opens NEW connection to addr[i]    |
  |      using a separate dialerHost (different     |
  |      peer ID, different port)                   |
  |                                                |
  |<----- [new conn] /libp2p/autonat/2/dial-back --|
  |       DialBack{nonce} -------------------------|
  |                                                |
  |  [3] Client verifies nonce + address consistency
  |       DialBackResponse{OK} ------------------->|
  |       (client closes dial-back stream)          |
  |                                                |
  |  [4] Server reports result on original stream   |
  |<----- DialResponse{OK, addrIdx=i, dialStatus=OK}
  |       (server closes dial-request stream)       |
```

### Happy Path (different IP — amplification prevention)

When the address to dial has a different IP than the client's observed
connection IP, the server imposes a data cost before dialing:

```
Client                                          Server
  |                                                |
  |------ DialRequest{nonce, addrs[]} ------------>|
  |                                                |
  |        addr[i] IP != client's observed IP      |
  |        → require dial data                     |
  |                                                |
  |<----- DialDataRequest{addrIdx=i, numBytes=N} --|
  |                                                |
  |------ DialDataResponse{data: 4096B} ---------->|
  |------ DialDataResponse{data: 4096B} ---------->|
  |------ ... (until >= N bytes sent) ------------>|
  |                                                |
  |        Server waits random [0, 3s]             |
  |        Server dials addr[i]                    |
  |                                                |
  |<----- [new conn] DialBack{nonce} --------------|
  |------ DialBackResponse{OK} ------------------->|
  |                                                |
  |<----- DialResponse{OK, addrIdx=i, dialStatus=OK}
```

### Failure Cases

| Scenario | Server Response |
|----------|----------------|
| Rate limited / resource exhaustion | `DialResponse{status: E_REQUEST_REJECTED}` |
| Server can't dial any provided address | `DialResponse{status: E_DIAL_REFUSED}` |
| Dial attempt fails (unreachable) | `DialResponse{status: OK, dialStatus: E_DIAL_ERROR}` |
| Dial succeeds but stream fails | `DialResponse{status: OK, dialStatus: E_DIAL_BACK_ERROR}` |
| Internal server error | `DialResponse{status: E_INTERNAL_ERROR}` |

## Message Format

All messages on the dial-request stream are wrapped in a `Message` envelope:

```protobuf
message Message {
    oneof msg {
        DialRequest      dialRequest      = 1;
        DialResponse     dialResponse     = 2;
        DialDataRequest  dialDataRequest  = 3;
        DialDataResponse dialDataResponse = 4;
    }
}
```

Messages on the dial-back stream are sent directly (no wrapper).

### DialRequest
```protobuf
message DialRequest {
    repeated bytes addrs = 1;  // multiaddrs, priority descending
    fixed64 nonce = 2;         // random 64-bit verification token
}
```
- `addrs`: up to 50 addresses inspected (go-libp2p). Ordered by descending
  priority — the server picks the first one it can dial.
- `nonce`: random value the server must echo back on the dial-back connection.

### DialDataRequest
```protobuf
message DialDataRequest {
    uint32 addrIdx  = 1;  // zero-based index of selected address
    uint64 numBytes = 2;  // bytes the client must send [30,000–100,000)
}
```

### DialDataResponse
```protobuf
message DialDataResponse {
    bytes data = 1;  // max 4096 bytes per message, min 100 bytes
}
```
Only the `data` field byte count counts toward the `numBytes` total — protobuf
framing overhead is excluded.

### DialBack (on dial-back stream)
```protobuf
message DialBack {
    fixed64 nonce = 1;
}
```

### DialBackResponse (on dial-back stream)
```protobuf
message DialBackResponse {
    enum DialBackStatus { OK = 0; }
    DialBackStatus status = 1;
}
```

### DialResponse
```protobuf
message DialResponse {
    ResponseStatus status = 1;
    uint32 addrIdx        = 2;
    DialStatus dialStatus = 3;
}

enum ResponseStatus {
    E_INTERNAL_ERROR   = 0;
    E_REQUEST_REJECTED = 100;
    E_DIAL_REFUSED     = 101;
    OK                 = 200;
}

enum DialStatus {
    UNUSED            = 0;
    E_DIAL_ERROR      = 100;
    E_DIAL_BACK_ERROR = 101;
    OK                = 200;
}
```

`addrIdx` and `dialStatus` are only meaningful when `status == OK`.

## Server Behavior (go-libp2p)

### Address Selection

The server iterates the client's address list in order and picks the **first
address it can dial**. An address is dialable if:
- The server's `dialerHost` transport supports it
- It's not a private/loopback address (unless `allowPrivateAddrs` is set)
- The server has the necessary connectivity (e.g., IPv4 for an IPv4 address)

If no address is dialable, the server returns `E_DIAL_REFUSED`.

### Dial-Back Mechanism

The server uses a **separate `dialerHost`** — a dedicated libp2p host with:
- Its own private key (different peer ID from the server)
- No reuse of existing connections
- `network.WithForceDirectDial` to prevent relay usage
- `swarm.WithReadOnlyBlackHoleDetector` to avoid corrupting black hole detection

After the dial-back completes, the server immediately closes the connection
(`ClosePeer` + `ClearAddrs` + `RemovePeer`). To ensure the nonce is delivered
before disconnecting, the server does:
1. Write `DialBack{nonce}` to the stream
2. `CloseWrite()` — signal end of writing
3. `Read(1 byte)` with 5-second deadline — wait for client confirmation

### Rate Limiting

| Parameter | Default | Description |
|-----------|---------|-------------|
| `serverRPM` | 60 | Global requests per minute (1/sec) |
| `serverPerPeerRPM` | 12 | Per-peer requests per minute (1/5sec) |
| `serverDialDataRPM` | 12 | Dial-data requests per minute (1/5sec) |
| `maxConcurrentRequestsPerPeer` | 2 | Max simultaneous requests from one peer |

Uses a 1-minute sliding window. Rate limit is checked BEFORE reading the request
message. The dial-data rate limit is checked separately AFTER determining dial
data is needed.

### Amplification Prevention

**Triggers when**: selected address IP != client's observed connection IP.

**Cost**: client must send `rand(30000, 100000)` bytes of data in 4096-byte
chunks (minimum 100 bytes per chunk to prevent compute exhaustion).

**Additional mitigation**: after receiving dial data, the server waits a random
`[0, 3s]` delay before dialing (anti-thundering-herd).

## Client Behavior (go-libp2p)

### Server Discovery

AutoNAT servers are discovered passively via the event bus:
- `EvtPeerIdentificationCompleted` — when identify reveals a peer supports
  `/libp2p/autonat/2/dial-request`
- `EvtPeerProtocolsUpdated` — when a peer's protocol list changes
- `EvtPeerConnectednessChanged` — when a peer connects/disconnects

A peer is eligible when it is **connected** AND supports the dial-request protocol.

### Server Selection

When a reachability probe is needed:
1. Eligible peers are iterated in **random order** (shuffled)
2. Peers whose throttle timer has not expired are skipped
3. First unthrottled peer is selected and throttled for **2 minutes**
4. If no peers available, `ErrNoPeers` is returned

### Reachability Interpretation

| Server Response | Client Interpretation |
|-----------------|----------------------|
| `dialStatus: OK` | **Public** (reachable) |
| `dialStatus: E_DIAL_BACK_ERROR` | **Public** (connection succeeded, stream failed) |
| `dialStatus: E_DIAL_ERROR` | **Private** (unreachable) |
| `status: E_REQUEST_REJECTED` | No result — try another server |
| `status: E_DIAL_REFUSED` | No result — try another server |

Note: `E_DIAL_BACK_ERROR` is interpreted as **Public** because the server
successfully established a connection to the address. The stream-level failure
doesn't negate the network-level reachability.

### Verification Steps

Before accepting a dial-back result, the client verifies:

1. **Nonce match**: the nonce received on the dial-back stream must match the
   one sent in the DialRequest.
2. **Address consistency**: the local address of the dial-back connection must be
   consistent with the address that was tested. This handles DNS→IP resolution,
   certhash stripping, and transport normalization.

The client does NOT verify the peer ID of the dial-back connection — the server
is explicitly allowed to use a different peer ID for dial-backs.

### Confidence System

The probing scheduler (`addrsReachabilityTracker`) maintains per-address confidence:

| Parameter | Value | Description |
|-----------|-------|-------------|
| `targetConfidence` | 3 | Net successes (or failures) needed for high confidence |
| `minConfidence` | 2 | Minimum net difference to declare reachable/unreachable |
| `maxRecentDialsWindow` | 5 | Sliding window of recent probe outcomes |
| `maxConsecutiveRefusals` | 5 | Refusals before pausing probes for an address |

**Reachability determination**:
- `successes - failures >= minConfidence` → **Public**
- `failures - successes >= minConfidence` → **Private**
- Otherwise → **Unknown** (keep probing)

**Re-probe intervals**:
- High-confidence primary address: every **1 hour**
- High-confidence secondary address: every **3 hours**
- After consecutive refusals: pause for **10 minutes**
- New address detected: probe after **1 second** delay
- Refresh ticker: every **5 minutes**

### Primary vs Secondary Addresses

Addresses are grouped by "thin waist" (IP + transport port). Within each group:
- **Primary**: simplest transport (QUIC-v1 or TCP)
- **Secondary**: higher-level transports (WebTransport, WebRTC, WSS)

A secondary address inherits `ReachabilityPublic` from its primary. Rationale:
if the port is network-reachable, protocol-level failures in secondary
transports typically mean the probing peer doesn't support that transport, not
that the address is unreachable.

## Timeouts

| Timeout | Value | Context |
|---------|-------|---------|
| Stream deadline (dial-request) | 15 seconds | Entire request-response exchange |
| Dial-back stream deadline | 5 seconds | Dial-back nonce exchange |
| Dial-back dial timeout | 10 seconds | Server's connection attempt to client |
| Amplification delay | 0–3 seconds (random) | Wait after dial data before dialing |
| Dial-back confirmation read | 5 seconds | Server waits for client to ACK nonce |

## Integration with Other Protocols

### [Identify](https://github.com/libp2p/specs/blob/master/identify/README.md)
The Identify protocol serves two roles in AutoNAT v2:

1. **Server discovery**: when Identify completes and reveals a peer supports
   `/libp2p/autonat/2/dial-request`, that peer is added to the eligible servers pool.
2. **Address discovery**: Identify reports the client's **observed address** —
   the IP:port the remote peer sees. For a node behind NAT, this is the router's
   external address. The client uses these observed addresses as candidates to
   verify via AutoNAT v2.

### Observed Address Activation

go-libp2p does **not** immediately accept an observed address from a single peer.
The `ObservedAddrManager` (`p2p/host/observedaddrs/manager.go`) requires multiple
independent confirmations before an address is "activated" and added to the
host's advertised address list.

**Key parameters:**
- `ActivationThresh = 4` — an observed address must be reported by **at least 4
  distinct observers** before it's activated.
- For **IPv4**, each individual IP counts as a separate observer (no subnet grouping).
- For **IPv6**, all addresses in the same `/56` prefix count as a single observer.
- Maximum 3 external addresses per local (listen) address.

**Testbed implications:** A Docker testbed with servers on the same subnet
(e.g., `73.0.0.0/24`) needs at least 4 servers with distinct IPs for the
client to activate its observed public address. With fewer than 4 servers,
the observed address never meets the threshold and AutoNAT probing never
starts. The Heathrow local experiment worked because IPFS bootstrap peers are
on diverse IPs (5 peers on different /16 networks → 5 distinct observers).

`ActivationThresh` is a package-level `var` (not `const`), so it can be
overridden for testing:
```go
import "github.com/libp2p/go-libp2p/p2p/host/observedaddrs"

func init() {
    observedaddrs.ActivationThresh = 2
}
```

### Transport-Specific Probing Behavior (Docker Testbed Finding)

In the Docker testbed with port-restricted NAT and `OBS_ADDR_THRESH=2`:

- **TCP observed address**: Activated at ~5s, probed by 3 servers in ~1s, correctly
  determined as **unreachable**. The full flow completed in ~6s total.
- **QUIC observed address**: Activated at ~5s, but probing **never completed** within
  120+ seconds. The address stayed as "unknown" indefinitely.

**Root cause**: With port-restricted NAT (iptables `--to-ports` + MASQUERADE), the
AutoNAT v2 dial-back over QUIC/UDP cannot complete because:
1. The server's dial-back comes from a different source port than what the client
   originally connected to
2. Port-restricted NAT drops the incoming UDP packet since it doesn't match any
   existing NAT mapping
3. TCP dial-back also gets blocked, but the timeout is shorter and the probing
   framework handles it correctly, marking the address as unreachable

**Practical implication**: AutoNAT v2 probing results may differ by transport even
for the same NAT type. QUIC addresses behind restrictive NATs may never get a
definitive reachability result, while TCP addresses are correctly classified.

### Non-Port-Preserving NAT Prevents Address Activation (Flight WiFi Finding)

On networks with non-port-preserving NAT (symmetric NAT, CGNAT with port
randomization), the observed address manager **never activates** a public address:

1. Each outbound connection gets a different external port
2. Peer A sees us as `/ip4/X.X.X.X/tcp/54321`, peer B sees `/ip4/X.X.X.X/tcp/54322`
3. These are treated as different multiaddrs — each observed only once
4. No single address reaches `ActivationThresh` (default 4)
5. AutoNAT v2 has no addresses to probe; only v1 provides a result

Observed on satellite in-flight WiFi (216.250.199.18 public IP, 711ms avg RTT):
- 4/5 bootstrap peers connected, but no public address was ever activated
- Only AutoNAT v1 fired (~31s to `private` result)
- 1/3 runs showed oscillation (private → unknown), suggesting v1 instability
  under high-latency conditions

Compare with airport WiFi (port-preserving NAT, 55ms RTT):
- Public address activated at ~5s (external port matched internal port)
- AutoNAT v2 probed and confirmed unreachable
- Stable `private` result at ~17s in 4/4 runs

### Relay (Circuit v2)
When AutoNAT v2 is enabled and no addresses are confirmed reachable, the host
adds relay addresses to its advertised set. When at least one address is
confirmed reachable, relay addresses are excluded.

### Hole Punching
`HolePunchAddrs()` returns all direct public addresses regardless of AutoNAT v2
reachability status. This is correct because hole punching targets addresses
behind NAT.

### Black Hole Detector
The AutoNAT v2 dialer host uses `ReadOnlyBlackHoleDetector` so failed dial-back
attempts don't corrupt the node's black hole detection state.

**Known bug (patched):** The original code shares `UDPBlackHoleSuccessCounter`
between the main host and the `dialerHost`. On fresh servers with zero UDP
connection history, the counter enters `Blocked` state, causing the
`dialerHost` to refuse all QUIC/UDP dials (`filterKnownUndialables` returns
`"dial refused because of black hole"`). Long-running nodes (Kubo) are
unaffected because their counters accumulate enough successes to reach
`Allowed` state. **Fix:** Set `UDPBlackHoleSuccessCounter: nil` with
`CustomUDPBlackHoleSuccessCounter: true` to disable the detector for the
`dialerHost` entirely. See `go-libp2p-patched/config/config.go`.

## Spec vs Implementation

This section documents where the go-libp2p implementation matches, extends, or
diverges from the [AutoNAT v2 specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md).

### Wire format

The protobuf definition is **identical** to the spec. Protocol IDs match:
`/libp2p/autonat/2/dial-request` and `/libp2p/autonat/2/dial-back`.

### Spec compliance

| Spec requirement | Status |
|---|---|
| Client sends `DialRequest{addrs, nonce}` | Matches |
| Server picks first dialable address in order | Matches |
| Server SHOULD NOT dial private addresses | Matches (`manet.IsPublicAddr` check) |
| Client SHOULD NOT send private addresses | Matches (filtered in `GetReachability`) |
| Amplification prevention when IP differs | Matches |
| Dial data: 30k–100k bytes | Matches (`minHandshakeSizeBytes=30_000`, `maxHandshakeSizeBytes=100_000`) |
| Dial data chunks max 4096 bytes | Matches (client uses 4000-byte buffer) |
| Min 100 bytes per chunk | Matches (`readDialData` rejects < 100) |
| Server uses separate peer ID for dial-back | Matches (fresh Ed25519 key) |
| Nonce verification by client | Matches |
| Client SHOULD NOT verify peer ID on dial-back | Matches |
| Servers SHOULD NOT reuse listening port | Matches (separate dialer host) |

### Implementation-specific behavior (not in spec)

**Address consistency check**: The client verifies the dial-back connection's
local address matches the tested address using `areAddrsConsistent()`. This does
protocol-by-protocol comparison with special handling for DNS→IP resolution,
`/wss`→`/tls/ws` normalization, `/sni` stripping, and certhash/p2p component
removal. The spec only says "examining the local address of the connection."

**`E_DIAL_BACK_ERROR` → Public**: The implementation treats `E_DIAL_BACK_ERROR`
as reachable (Public) because the server successfully connected at the network
level. The spec describes what the status means but does not prescribe how the
client should interpret it.

**`SendDialData` per-address**: Each `Request` has a `SendDialData` boolean.
The client can accept amplification cost only for high-priority addresses and
reject `DialDataRequest` for low-priority ones. Not in the spec.

**Anti-thundering-herd delay**: After receiving dial data, the server waits a
random `[0, 3s]` before dialing. Not in the spec.

**Max message sizes**: `maxMsgSize=8192` (dial-request stream),
`dialBackMaxMsgSize=1024` (dial-back stream). Not specified.

**Confidence system**: The spec suggests "more than 3 servers report a successful
dial" as a heuristic. The implementation uses a sliding window with
`minConfidence=2`, `targetConfidence=3`, primary/secondary address grouping,
exponential backoff, and per-address refusal tracking. See
[Confidence System](#confidence-system) above.

### Notable bug fixes

| Version | Date | Fix |
|---------|------|-----|
| v0.41.1 | 2025-03-24 | **Amplification policy was comparing wrong addresses** — was comparing client's observed IP with the server's own local IP instead of the dial target. Dial data was almost always requested unnecessarily. |
| v0.41.1 | 2025-03-24 | **DNS addresses not handled** — `manet.ToIP()` silently failed for DNS multiaddrs, always triggering dial data. |
| v0.44.0 | 2025-10-07 | **WebSocket normalization** — `/wss` addresses weren't normalized to `/tls/ws` for address consistency checks. |

### Implementation history across languages

#### go-libp2p

| Version | Date | AutoNAT v2 changes |
|---------|------|-------------------|
| v0.34.0 | 2024-05-20 | Initial autonatv2 implementation |
| v0.37.0 | 2024-10-22 | Panic recovery added |
| v0.40.0 | 2025-02-17 | Multiple concurrent requests per peer (default 2) |
| v0.41.1 | 2025-03-24 | Critical bug fixes: amplification policy + DNS addr handling |
| v0.42.0 | 2025-06-18 | `addrsReachabilityTracker` — autonatv2 becomes primary reachability mechanism; metrics added |
| v0.43.0 | 2025-08-07 | Migrated to log/slog |
| v0.44.0 | 2025-10-07 | WebSocket normalization fix; removed webrtc/webtransport dependency |
| v0.47.0 | 2026-01-25 | Latest stable release |

The protobuf wire format has not changed since the initial implementation.
The core protocol (message exchange, nonce verification, amplification
prevention) has been stable since v0.41.1 after the bug fixes above.

#### rust-libp2p

| Crate version | libp2p version | Date | AutoNAT v2 changes |
|---------------|---------------|------|-------------------|
| libp2p-autonat 0.13.0 | v0.54.1 | 2024-08-19 | Initial autonatv2 implementation ([PR #5526](https://github.com/libp2p/rust-libp2p/pull/5526)) |
| libp2p-autonat 0.14.0 | v0.55.0 | 2025-01-15 | Verify dial comes from connected peer; deprecate `void` crate |
| libp2p-autonat 0.15.0 | v0.56.0 | 2025-06-27 | Fix infinite loop on wrong nonce during dial-back ([PR #5848](https://github.com/libp2p/rust-libp2p/pull/5848)) |

#### js-libp2p

| Package version | Date | AutoNAT v2 changes |
|-----------------|------|-------------------|
| @libp2p/autonat-v2 1.0.0 | 2025-06-25 | Initial autonatv2 implementation ([PR #3196](https://github.com/libp2p/js-libp2p/pull/3196)) |
| @libp2p/autonat-v2 2.0.0 | 2025-09-03 | Streams as EventTargets (breaking API change) |
| @libp2p/autonat-v2 2.0.10 | 2026-01-16 | Latest release (dependency updates) |

#### Timeline summary

| Date | Milestone |
|------|-----------|
| 2024-05-20 | **go-libp2p** ships autonatv2 (v0.34.0) — first implementation |
| 2024-08-19 | **rust-libp2p** ships autonatv2 (v0.54.1) |
| 2025-03-24 | go-libp2p critical bug fixes (v0.41.1) |
| 2025-06-18 | go-libp2p makes autonatv2 primary reachability mechanism (v0.42.0) |
| 2025-06-25 | **js-libp2p** ships autonatv2 (@libp2p/autonat-v2 1.0.0) |

## References

- [AutoNAT v2 Specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
- [Identify Specification](https://github.com/libp2p/specs/blob/master/identify/README.md)
- [go-libp2p implementation](https://github.com/libp2p/go-libp2p/tree/master/p2p/protocol/autonatv2)
- [go-libp2p address reachability tracker](https://github.com/libp2p/go-libp2p/blob/master/p2p/host/basic/addrs_reachability_tracker.go)
- [rust-libp2p implementation](https://github.com/libp2p/rust-libp2p/tree/master/protocols/autonat/src/v2)
- [js-libp2p implementation](https://github.com/libp2p/js-libp2p/tree/main/packages/protocol-autonat-v2)
- [Amplification attack analysis (issue #640)](https://github.com/libp2p/specs/issues/640)
