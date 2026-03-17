# rust-libp2p AutoNAT v2 Implementation

Implementation details for the AutoNAT v2 subsystem in
[rust-libp2p](https://github.com/libp2p/rust-libp2p). For the protocol
specification, see [autonat-v2.md](autonat-v2.md). For go-libp2p comparison,
see [go-libp2p-autonat-implementation.md](go-libp2p-autonat-implementation.md).

Based on rust-libp2p **v0.54** / libp2p-autonat **v0.15.0**.

---

## Architecture Overview

rust-libp2p's AutoNAT v2 implementation is structurally simpler than
go-libp2p's. The client and server are separate `NetworkBehaviour`s
that can be composed independently into a swarm.

### Key Differences from go-libp2p

| Aspect | go-libp2p | rust-libp2p |
|--------|-----------|-------------|
| **Architecture** | Monolithic `AutoNAT` struct with client+server | Separate `client::Behaviour` and `server::Behaviour` |
| **Orchestration** | `addrsReachabilityTracker` with refresh cycles | Simple periodic probing with score-based candidates |
| **Confidence system** | Sliding window (5 results), min/target confidence | None — single probe per candidate |
| **Address grouping** | Primary/secondary by thin-waist, secondary inherits | None — each address independent |
| **Re-probe scheduling** | 5min refresh, 1h/3h high-confidence, exponential backoff | Fixed probe interval (5s) |
| **Server selection** | Random shuffle with 2min per-peer throttle | Random from connected AutoNAT peers |
| **Observed addr filtering** | `ObservedAddrManager` with activation threshold (N=4) | None — raw `NewExternalAddrCandidate` events |
| **Black hole detection** | UDP/IPv6 success counters, blocks QUIC when failing | Not implemented |
| **Event model** | `EvtHostReachableAddrsChanged` (aggregated) | Per-probe `Event` struct |
| **Dial-back host** | Separate `dialerHost` with fresh key+peerstore | Shared swarm (server behaviour uses normal dial) |

### Implications for Cross-Implementation Testing

The lack of observed address consolidation in rust-libp2p means:

1. **No-NAT scenario**: go-libp2p correctly identifies listen port → REACHABLE.
   rust-libp2p probes ephemeral ports → UNREACHABLE (false negative).

2. **NAT scenarios**: Behavior depends on whether the NAT preserves ports.
   For EIM NATs, all servers see the same mapped port, so the candidate
   list may converge. For symmetric NAT, every connection uses a different
   port (same as no-NAT behavior).

3. **Confidence**: go-libp2p requires 3 net successes for high confidence.
   rust-libp2p makes a single determination per probe. A single success
   or failure is final.

4. **Aggregation**: go-libp2p emits one `EvtHostReachableAddrsChanged` event
   with all reachable/unreachable/unknown addresses aggregated. rust-libp2p
   emits individual `Event` per probe. Our testbed Rust client aggregates
   these into the same `reachable_addrs_changed` span format.

---

## Source Layout

```
protocols/autonat/src/
  v2/
    client/
      behaviour.rs     Client behaviour: candidate management, probe scheduling
      handler/
        dial_back.rs   Client-side dial-back stream handler
        dial_request.rs  Client-side dial-request stream handler
    server/
      behaviour.rs     Server behaviour: incoming dial-request handling
      handler/
        dial_back.rs   Server-side dial-back execution
        dial_request.rs  Server-side dial-request stream handler
    protocol.rs        Protocol IDs and wire format
  lib.rs               Module root, re-exports
```

---

## Key Types

### `autonat::v2::client::Behaviour`

The client behaviour manages address candidates and schedules probes.

```rust
pub struct Behaviour {
    // Address candidates scored by frequency of observation
    candidates: HashMap<Multiaddr, CandidateState>,
    // Connected peers that support AutoNAT v2
    servers: Vec<PeerId>,
    // Configuration
    config: Config,
}
```

### `autonat::v2::client::Event`

Emitted per-address after each probe completes.

```rust
pub struct Event {
    pub tested_addr: Multiaddr,      // the address that was probed
    pub bytes_sent: usize,           // amplification data sent (0 or 30K–100K)
    pub server: PeerId,              // server that performed the dial-back
    pub result: Result<(), Error>,   // success or failure reason
}
```

### `autonat::v2::client::Config`

```rust
pub struct Config {
    pub max_candidates: usize,       // max addresses to track (default: 10)
    pub probe_interval: Duration,    // time between probe rounds (default: 5s)
}
```

### `autonat::v2::server::Behaviour`

The server behaviour handles incoming dial-request streams and performs
dial-backs. In our testbed, the Go servers handle the server role, but the
Rust client also includes the server behaviour for protocol completeness.

---

## Address Candidate Selection

This is the most significant architectural difference from go-libp2p.

### How candidates are discovered

The autonat v2 client receives address candidates from
`SwarmEvent::NewExternalAddrCandidate` events, which are primarily
emitted by the **identify protocol**. When a remote peer identifies our
node, it reports our `ObservedAddr` — the address+port it sees us
connecting from. The identify behaviour then emits this as a
`NewExternalAddrCandidate`.

### The ephemeral port problem

Each outbound TCP connection uses a unique ephemeral source port assigned
by the kernel (e.g., `/ip4/73.0.0.101/tcp/43992`). When we connect to 7
servers, each server sees a different source port and reports a different
`ObservedAddr`. The autonat v2 client receives 7+ unique candidates, all
with ephemeral ports that nothing is listening on.

When the autonat v2 client probes these addresses, the Go server's
dial-back naturally fails — there's no listener on port 43992. The result
is that every probe returns `UNREACHABLE` with `NoConnection`, even for a
node that is genuinely reachable on its listen port (4001).

### Comparison with go-libp2p

go-libp2p solves this with the **ObservedAddrManager** (in
`p2p/host/observedaddrs/manager.go`), which has an **activation threshold**:

| Aspect | go-libp2p | rust-libp2p |
|--------|-----------|-------------|
| Address source | ObservedAddrManager (filtered) | `NewExternalAddrCandidate` (raw) |
| Deduplication | Groups by listen port, counts observations | None — each observed addr is a unique candidate |
| Activation threshold | Requires N observations of same addr (default: 4) | No threshold — every candidate is immediately eligible |
| Ephemeral port handling | Filtered out (never reach activation threshold) | Probed and fail with `NoConnection` |
| Listen port promotion | Promoted after threshold met | Never promoted (rely on identify observations) |

In go-libp2p, when 7 servers all observe our `/tcp/4001` listen port
(since NAT is not remapping), the ObservedAddrManager sees 7 observations
of the same address, crosses the activation threshold, and promotes
`/ip4/73.0.0.101/tcp/4001` as an external address. AutoNAT v2 then probes
this correct address and confirms reachability.

In rust-libp2p, each server observes a different ephemeral source port
(because these are outbound connections, not inbound). The identify
protocol reports these ephemeral ports as `ObservedAddr`. Without
consolidation, the autonat v2 client probes all of them and all fail.

### Why `add_external_address()` doesn't help

We attempted to manually register listen addresses via
`swarm.add_external_address(addr)` when `NewListenAddr` events fire.
This emits `ExternalAddrConfirmed`, but the autonat v2 client builds
its candidate list from `NewExternalAddrCandidate` events (a different
event type). Confirmed external addresses are not re-tested.

### Impact on testbed results

In the no-NAT scenario:
- **go-libp2p client**: `/tcp/4001` promoted → probed → `REACHABLE` (~6s)
- **rust-libp2p client**: ephemeral ports probed → all `UNREACHABLE` (false negative)

This is a **false negative caused by address candidate selection**, not
by the AutoNAT v2 protocol itself. The protocol works correctly — the
server dials back the requested address, and it correctly reports that
nothing is listening on the ephemeral port.

### Potential fixes (upstream)

1. **Add observed address consolidation** to rust-libp2p's identify or
   swarm layer, similar to go-libp2p's `ObservedAddrManager`. Group
   observations by listen port, require N observations before promotion.

2. **Use `ExternalAddrConfirmed` in autonat v2 client** as an additional
   source of candidates, not just `NewExternalAddrCandidate`.

3. **Filter candidates by listen port** — if a candidate shares the same
   IP as a listen address but has a different port, and the candidate
   port is not a known listen port, deprioritize or discard it.

---

## OTel / Tracing

The rust-libp2p autonat v2 implementation has **no OpenTelemetry
instrumentation**. The `client/behaviour.rs` uses only `tracing::debug!()`
and `tracing::warn!()` for logging — no `#[instrument]` annotations, no
`tracing::span!` macros, no OpenTelemetry code.

For the testbed, we emit spans at the application layer from swarm events:

| Span name | Source event | Attributes |
|-----------|-------------|------------|
| `started` | Node startup | `peer_id`, `message` |
| `connected` | `SwarmEvent::ConnectionEstablished` | `peer_id` |
| `reachable_addrs_changed` | `autonat::v2::client::Event` | `reachable[]`, `unreachable[]`, `unknown[]` |
| `shutdown` | SIGTERM/SIGINT | `message` |
| `peer_discovery_start` | Before connecting to servers | `message` |
| `peer_discovery_done` | After connecting to servers | `message` |

These match the span names and attribute format used by the Go client,
so `analyze.py` can process traces from either implementation unchanged.

Unlike go-libp2p (with our probe-lab fork), there are no per-probe spans
(`autonatv2.probe`) or refresh cycle spans (`autonatv2.refresh_cycle`).

---

## Constants

### Client

| Constant | Value | Description |
|----------|-------|-------------|
| `max_candidates` | 10 | Maximum address candidates to track |
| `probe_interval` | 5s | Interval between probe rounds |

### Protocol (shared with go-libp2p)

| Constant | Value | Description |
|----------|-------|-------------|
| Dial-request protocol | `/libp2p/autonat/2/dial-request` | Stream protocol for requesting probes |
| Dial-back protocol | `/libp2p/autonat/2/dial-back` | Stream protocol for nonce verification |
| Min handshake bytes | 30,000 | Minimum amplification data |
| Max handshake bytes | 100,000 | Maximum amplification data |

---

## Testbed Integration

### Docker image

- **Base**: `rust:alpine` (latest stable)
- **Binary**: `autonat-node-rust` — single static binary (~20MB stripped)
- **Entrypoint**: Reuses the Go node's `entrypoint.sh` (gateway/routing setup)
- **Build time**: ~2m16s (first build with dependency caching)

### CLI interface

Same flags as the Go client:

```
--role=client          Node role (only "client" supported)
--transport=both       Transport: tcp, quic, both
--port=4001            Listen port
--peer-dir=/peer-addrs Directory with server multiaddr files
--otlp-endpoint=URL    OTLP HTTP endpoint for Jaeger
--trace-file=PATH      Write JSONL spans to file
```

### Compose services

| Service | Profile | Network | IP |
|---------|---------|---------|-----|
| `client-rust` | `rust` | private-net | 10.0.1.10 |
| `client-rust-nonat` | `rust-nonat` | public-net | 73.0.0.101 |
| `client-rust-mock` | `rust-mock` | public-net | 73.0.0.101 |

---

## Spec Compliance

| Spec requirement | rust-libp2p status |
|---|---|
| Client sends `DialRequest{addrs, nonce}` | Matches |
| Server picks first dialable address in order | Matches |
| Server SHOULD NOT dial private addresses | Matches |
| Client SHOULD NOT send private addresses | **Partial** — no `IsPublicAddr` filter; relies on identify-reported addresses which may include private ranges |
| Amplification prevention when IP differs | Matches |
| Dial data: 30k-100k bytes | Matches |
| Server uses separate peer ID for dial-back | **No** — server behaviour dials from the same swarm (same peer ID) |
| Nonce verification by client | Matches |
| Client SHOULD NOT verify peer ID on dial-back | Matches |

### Implementation-Specific Behavior

| Feature | go-libp2p | rust-libp2p |
|---------|-----------|-------------|
| Separate dialer host for dial-back | Yes (fresh key, peerstore) | No (shared swarm) |
| `E_DIAL_BACK_ERROR` → Public | Yes (network connection succeeded) | No (reports as error) |
| Address consistency check (DNS normalization) | Yes | No |
| Rate limiting (server) | Yes (60 RPM global, 12 per-peer) | Basic (concurrent request limit) |
| Anti-thundering-herd delay | Yes (random 0-3s) | No |
| Primary/secondary address grouping | Yes (inherit reachability) | No |
| Confidence sliding window | Yes (5 results, min/target confidence) | No (single probe) |
| Exponential backoff on errors | Yes (5s → 5min) | No |

---

## Server-Side Differences

The rust-libp2p `autonat::v2::server::Behaviour` differs from go-libp2p's
server in several ways that affect testbed results:

### Dial-Back Identity

go-libp2p creates a separate `dialerHost` with a fresh Ed25519 key and
isolated peerstore for dial-backs. This ensures the client can distinguish
the dial-back from a normal connection (different peer ID) and prevents
connection reuse.

rust-libp2p's server behaviour uses the same swarm for dial-backs. This
means the dial-back comes from the same peer ID that the client is already
connected to, which could confuse address verification (the spec says
the client "SHOULD NOT" verify the peer ID, but implementations may
behave differently).

### Rate Limiting

go-libp2p has sophisticated rate limiting: 60 requests/minute global,
12/minute per-peer, 12/minute for dial-data requests. rust-libp2p has
simpler limits (max concurrent requests) without time-windowed rate
limiting.

### Address Selection

Both implementations pick the first dialable address from the client's
request. go-libp2p applies `manet.IsPublicAddr()` to filter private
addresses; rust-libp2p applies similar but not identical filtering.

---

## Known Issues and Limitations

### 1. Ephemeral Port Probing (Critical)

**Status**: Confirmed in testbed. Tracked at [rust-libp2p #4873](https://github.com/libp2p/rust-libp2p/issues/4873).

The autonat v2 client probes observed connection addresses (ephemeral ports)
instead of listen addresses. Without the address consolidation that
go-libp2p's `ObservedAddrManager` provides, nodes behind no NAT or
port-preserving NAT still fail to confirm reachability.

See [Address Candidate Selection](#address-candidate-selection) for full
analysis.

### 2. No Confidence Tracking

A single probe determines the result. In networks with intermittent
connectivity or partial reachability (some servers can reach, others
can't), this leads to unstable results. go-libp2p's `targetConfidence=3`
requires 3 net successes before declaring public, which is more resilient.

### 3. No OTel Instrumentation

The crate has no OpenTelemetry spans or metrics. For production
observability, instrumentation would need to be added at the
application layer (as we do in the testbed) or upstreamed.

### 4. No Black Hole Detection

rust-libp2p does not implement a UDP black hole detector. On networks
that silently drop UDP traffic, the client will continue probing QUIC
addresses indefinitely instead of falling back to TCP.

---

## References

- [AutoNAT v2 Protocol Walkthrough](autonat-v2.md)
- [AutoNAT v2 Specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
- [rust-libp2p autonatv2 source](https://github.com/libp2p/rust-libp2p/tree/master/protocols/autonat/src/v2)
- [rust-libp2p autonat v2 PR #5526](https://github.com/libp2p/rust-libp2p/pull/5526)
- [Address candidate issue #4873](https://github.com/libp2p/rust-libp2p/issues/4873)
- [go-libp2p AutoNAT v2 Implementation](go-libp2p-autonat-implementation.md)
- [go-libp2p observed address manager](https://github.com/libp2p/go-libp2p/blob/master/p2p/host/observedaddrs/manager.go)
