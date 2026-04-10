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

This is the most significant behavioral difference from go-libp2p.
Our investigation revealed a chain of interacting issues involving
TCP port reuse, identify address translation, and connection timing.

### Background: TCP Port Reuse

When a libp2p node makes an outbound TCP connection, the OS normally
assigns a random **ephemeral port** (e.g., 55638) as the local source
port. The remote peer sees this ephemeral port, not the node's listen
port (4001). This is a problem because:

- The remote peer reports `ObservedAddr=/ip4/X.X.X.X/tcp/55638` via identify
- AutoNAT v2 probes this address — nothing listens on port 55638
- Result: UNREACHABLE (false negative)

**TCP port reuse** (`SO_REUSEPORT`) solves this by binding outbound
connections to the same port as the listener. The remote peer then sees
port 4001 — the correct listen port. Both go-libp2p and rust-libp2p
support TCP port reuse.

**QUIC does not have this problem** because all QUIC connections share a
single UDP socket bound to the listen port. Every peer naturally sees
the correct port.

### How go-libp2p Handles It

go-libp2p uses [`go-reuseport`](https://github.com/libp2p/go-reuseport)
which sets `SO_REUSEPORT` + `SO_REUSEADDR` on TCP sockets:

1. Listener binds to `0.0.0.0:4001` with `SO_REUSEPORT`
2. Outbound `net.Dialer` sets `LocalAddr` to the listen address (port 4001)
   and applies `SO_REUSEPORT` via the `Control` function
3. Kernel binds outbound socket to port 4001
4. Remote peer sees port 4001 → identify reports correct `ObservedAddr`
5. `ObservedAddrManager` sees consistent observations → promotes address
6. AutoNAT v2 probes `/tcp/4001` → REACHABLE

Port reuse is enabled by default in go-libp2p and generally works on
Linux and macOS. The `ObservedAddrManager` provides an additional safety
net: it groups observations by "thin waist" (IP + transport, port-independent)
and replaces the observed port with the listen port when reconstructing
the external address.

### How rust-libp2p Handles It

rust-libp2p has TCP port reuse support with two layers of defense:

**Layer 1: TCP transport port reuse.**
The TCP transport sets `SO_REUSEPORT` and attempts to `bind()` outbound
sockets to the listen address via `local_dial_addr()`. The default
`DialOpts` uses `PortUse::Reuse`. If binding fails (e.g., listen address
not yet registered), it silently falls back to an ephemeral port.

**Layer 2: Identify address translation.**
When the identify behaviour receives an `ObservedAddr` from a peer, it
checks whether the connection used an ephemeral port (tracked in
`outbound_connections_with_ephemeral_port`). If so, it applies
`_address_translation()` — replacing the observed IP into the listen
address template, preserving the listen port. This correctly produces
`/ip4/X.X.X.X/tcp/4001` from an ephemeral observation.

### The Bug: Race Condition Between Listener and Dialer

Our testbed revealed that **both layers fail** due to a timing issue:

**Testbed observation** (rust client, no-NAT, 3 servers):
```
Connected to ... endpoint=Dialer { ..., port_use: Reuse }   ← QUIC connections
Connected to ... endpoint=Dialer { ..., port_use: Reuse }
Listening on /ip4/73.0.0.101/tcp/4001  ← TCP listener ready AFTER some dials
Connected to ... endpoint=Dialer { ..., port_use: Reuse }   ← TCP connections
NewExternalAddrCandidate: /ip4/73.0.0.101/udp/4001/quic-v1  ← QUIC correct
NewExternalAddrCandidate: /ip4/73.0.0.101/tcp/48168          ← TCP WRONG
NewExternalAddrCandidate: /ip4/73.0.0.101/tcp/58804          ← TCP WRONG
AutoNAT v2: /ip4/73.0.0.101/tcp/48168 UNREACHABLE           ← false negative
AutoNAT v2: /ip4/73.0.0.101/udp/4001/quic-v1 REACHABLE      ← correct!
```

**What happens:**

1. `swarm.listen_on()` is called for both TCP and QUIC
2. QUIC listener binds immediately (single UDP socket)
3. TCP listener setup is asynchronous — not registered yet
4. `connect_from_dir()` starts dialing peers with `PortUse::Reuse`
5. **Layer 1 fails**: `local_dial_addr()` checks `listen_addrs` — TCP
   listener not registered yet → returns `None` → ephemeral port used
6. TCP connection established with ephemeral local port (e.g., 48168)
7. Connection metadata still says `PortUse::Reuse` (the *requested*
   mode, not the actual outcome)
8. **Layer 2 fails**: Identify checks `outbound_connections_with_ephemeral_port`
   — this connection is NOT in the set (it was marked `Reuse`) →
   translation skipped
9. `NewExternalAddrCandidate` emitted with raw ephemeral port
10. AutoNAT v2 probes `/tcp/48168` → nothing listens → UNREACHABLE

QUIC works because step 2 completes synchronously — the UDP socket is
ready before any dials, so the observed address is already correct
(port 4001).

### Root Causes

Three issues contribute to the testbed failure:

1. **TCP listener registration is asynchronous** — the listen address
   isn't available to `local_dial_addr()` until the TCP listener is
   fully bound, which may happen after the first outbound connections.

2. **Port reuse failure doesn't update `PortUse`** — when `bind()` fails
   and the transport falls back to an ephemeral port, the connection is
   still marked as `PortUse::Reuse`. This is tracked in rust-libp2p's
   TCP transport (`lib.rs`): the fallback creates a new socket with
   `PortUse::New` internally, but the metadata isn't propagated to the
   connection.

3. **Identify trusts `PortUse` metadata** — the translation logic in
   `emit_new_external_addr_candidate_event()` checks
   `outbound_connections_with_ephemeral_port` (populated only for
   `PortUse::New`). Since the connection says `Reuse`, translation is
   skipped even though the actual port is ephemeral.

### Testbed Fix and Verification

Fixing the timing (waiting for `NewListenAddr` before dialing)
resolves the issue completely — both TCP and QUIC report REACHABLE:

```
Waiting for 2 listeners to be ready...
Listening on /ip4/73.0.0.101/tcp/4001
Listening on /ip4/73.0.0.101/udp/4001/quic-v1
All 2 listeners ready, connecting to peers...
NewExternalAddrCandidate: /ip4/73.0.0.101/tcp/4001          ← correct!
NewExternalAddrCandidate: /ip4/73.0.0.101/udp/4001/quic-v1  ← correct!
AutoNAT v2: /ip4/73.0.0.101/tcp/4001 REACHABLE
AutoNAT v2: /ip4/73.0.0.101/udp/4001/quic-v1 REACHABLE
```

This confirms that **TCP port reuse works correctly** in rust-libp2p
when the listener is ready. The issue is purely a timing/startup
ordering problem, not a kernel or socket-level failure.

### Broader Issue: No Safety Net Without Port Reuse

While the testbed timing issue is fixable, a deeper architectural
difference remains. If port reuse is not available or fails for any
reason, the three implementations behave differently:

| Scenario | go-libp2p | rust-libp2p | js-libp2p |
|----------|-----------|-------------|-----------|
| Port reuse works | ✅ Correct port | ✅ Correct port | N/A (no port reuse in Node.js) |
| Port reuse not used / fails | ✅ `ObservedAddrManager` corrects port via thin-waist grouping | ⚠️ Identify translation works ONLY if `PortUse::New` is explicit | ❌ No translation mechanism |
| Port reuse requested, fails silently | N/A (generally works) | ❌ Identify skips translation (trusts `PortUse` metadata) | N/A |

**go-libp2p** is the most robust: the `ObservedAddrManager` provides a
safety net independent of port reuse. It groups observations by thin
waist (IP + transport, port-independent) and replaces the observed port
with the listen port. Even with all ephemeral ports, the correct
address is promoted after `ActivationThresh=4` observations.

**rust-libp2p** has identify address translation that works when
`PortUse::New` is explicit — but fails when reuse is requested and
silently falls back. There is no `ObservedAddrManager` equivalent as a
safety net.

**js-libp2p** has no port reuse (Node.js TCP doesn't support
`SO_REUSEPORT`) and no address translation for ephemeral ports. It
relies on the address manager's `confirmObservedAddr()` path, which
stores observed addresses without port correction.

### Full Testbed Validation

After fixing the listener timing, the Rust client was tested across
all NAT types with port reuse enabled (default) and disabled
(`--no-port-reuse`):

**Port reuse enabled (default):**

| NAT Type | Transport | Candidate Port | Result | Correct? |
|----------|-----------|----------------|--------|----------|
| no-NAT | both | tcp/4001, udp/4001 | TCP+QUIC REACHABLE | Yes |
| full-cone | tcp | tcp/4001 | REACHABLE | Yes |
| full-cone | both | udp/4001 | QUIC REACHABLE | Yes |
| addr-restricted | tcp | tcp/4001 | REACHABLE (FP) | Same as go (ADF) |
| port-restricted | tcp | tcp/4001 | UNREACHABLE | Yes |
| port-restricted | both | udp/4001 | UNREACHABLE | Yes |
| symmetric | both | udp/random | UNREACHABLE | Yes |

**Port reuse disabled (`--no-port-reuse` / `PortUse::New`):**

| NAT Type | Transport | Candidate Port | Result | Correct? |
|----------|-----------|----------------|--------|----------|
| no-NAT | both | tcp/4001, udp/4001 | TCP+QUIC REACHABLE | Yes |

When port reuse is explicitly disabled, the identify `_address_translation`
correctly replaces the ephemeral port with the listen port. Both TCP and
QUIC produce correct candidates and REACHABLE results.

**Conclusion:** rust-libp2p AutoNAT v2 produces correct results in all
tested scenarios when either: (a) listeners are ready before dialing
(port reuse works), or (b) `PortUse::New` is explicit (identify
translation works). The only failure mode is when `PortUse::Reuse` is
requested but fails silently — identify then skips translation.

### Comparison: Why go-libp2p Doesn't Have This Problem

| Aspect | go-libp2p | rust-libp2p |
|--------|-----------|-------------|
| Port reuse mechanism | `go-reuseport` with `LocalAddr` on dialer | `SO_REUSEPORT` + `bind()` |
| Listener timing | Synchronous (listener ready before dials) | Asynchronous (race condition) |
| Fallback on failure | Not observed (reuse generally works) | Silent fallback to ephemeral |
| Port metadata accuracy | N/A (port is always correct) | `PortUse::Reuse` even when reuse failed |
| Address translation | `ObservedAddrManager` replaces port via thin-waist grouping | Identify translation skipped (trusts `PortUse`) |
| Safety net | `ObservedAddrManager` activation threshold | None — raw candidates go to autonat |

go-libp2p has two independent mechanisms that prevent this: (1) port
reuse generally works because the listener is ready before dials, and
(2) the `ObservedAddrManager` replaces the port even if it were wrong.
rust-libp2p has neither working for TCP due to the timing issue.

### Testbed Verification

**QUIC (working):**
```
NewExternalAddrCandidate: /ip4/73.0.0.101/udp/4001/quic-v1   ← correct port
ExternalAddrConfirmed: /ip4/73.0.0.101/udp/4001/quic-v1
AutoNAT v2: /ip4/73.0.0.101/udp/4001/quic-v1 REACHABLE
```

**TCP (broken):**
```
NewExternalAddrCandidate: /ip4/73.0.0.101/tcp/48168           ← ephemeral port
NewExternalAddrCandidate: /ip4/73.0.0.101/tcp/58804           ← ephemeral port
AutoNAT v2: /ip4/73.0.0.101/tcp/48168 UNREACHABLE (NoConnection)
AutoNAT v2: /ip4/73.0.0.101/tcp/58804 UNREACHABLE (NoConnection)
```

### Potential Fixes

**In our testbed (immediate):**
Wait for `NewListenAddr` events before dialing peers. This ensures the
TCP listener is registered and port reuse can find it.

**Upstream fix option 1 — Identify should check actual port:**
Instead of trusting `PortUse` metadata, identify should compare
`conn.local_addr().port()` against known listen ports. If they don't
match, apply translation regardless of `PortUse`.

**Upstream fix option 2 — TCP transport should report actual outcome:**
When port reuse fails and falls back to ephemeral, update the
connection's `PortUse` to `New` so identify handles it correctly.

**Upstream fix option 3 — Add `ObservedAddrManager` equivalent:**
Add a consolidation layer (like go-libp2p's thin-waist grouping) that
replaces observed ports with listen ports, providing a safety net
independent of port reuse success.

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

### 1. TCP Address Candidate Failure

**Status**: Confirmed and root-caused in testbed. Fixed in testbed
client. Upstream issue remains (no safety net without port reuse).

**Summary**: TCP AutoNAT v2 probes fail when port reuse is unavailable
(e.g., listener not yet registered, `SO_REUSEPORT` not supported).
Identify's address translation is bypassed when `PortUse::Reuse` was
requested but silently failed. Unlike go-libp2p, there is no
`ObservedAddrManager` to correct the port as a fallback.

**Testbed root cause**: A timing issue where outbound connections
started before the TCP listener was registered, preventing port reuse.
Fixed by waiting for `NewListenAddr` before dialing — TCP then works
correctly (REACHABLE confirmed).

**Remaining upstream issue**: rust-libp2p has no safety net when port
reuse genuinely fails (kernel limitation, socket error, etc.). go-libp2p
handles this via `ObservedAddrManager`; rust-libp2p does not.

**QUIC is not affected**: UDP socket binds synchronously, all
connections share it.

See [Address Candidate Selection](#address-candidate-selection) for
full analysis.

Related: [rust-libp2p #4873](https://github.com/libp2p/rust-libp2p/issues/4873)
(address ordering issue in v1 — different but related).

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

## DHT and AutoNAT Interaction

### DHT Mode Switching Mechanism

Unlike go-libp2p, rust-libp2p's DHT does **not** consume v1's global
reachability flag. It consumes `ExternalAddrConfirmed` events from the
swarm, which v2 emits when an address is confirmed reachable. This
means the F1 wiring gap (v1 controlling DHT mode) does not affect
rust-libp2p — v2 results feed into the DHT directly.

The Kademlia DHT (`libp2p-kad`) in rust-libp2p uses automatic mode
switching based on **confirmed external addresses**:

```rust
// From libp2p-kad behaviour.rs — determine_mode_from_external_addresses()
self.mode = match (self.external_addresses.as_slice(), self.mode) {
    ([], Mode::Server) => Mode::Client,   // lost all external addrs → client
    ([], Mode::Client) => Mode::Client,   // no addrs, stay client
    (_, Mode::Client) => Mode::Server,    // got external addrs → server
    (_, Mode::Server) => Mode::Server,    // have addrs, stay server
};
```

The DHT starts in `Mode::Client` and switches to `Mode::Server` when a
`FromSwarm::ExternalAddrConfirmed` event arrives (indicating at least one
confirmed external address). It switches back to client when all external
addresses expire.

### How AutoNAT v2 Feeds Into DHT

When AutoNAT v2 successfully probes an address, the swarm should emit
`ExternalAddrConfirmed`, which the DHT picks up. The intended flow is:

```
identify → NewExternalAddrCandidate → autonat v2 probes it →
  success → ExternalAddrConfirmed → DHT switches to server mode
```

However, because of the ephemeral port probing issue (see #1 above),
autonat v2 never confirms any address. The DHT therefore **stays in
client mode permanently**, even for genuinely reachable nodes.

### No Production Consumer

**Substrate/Polkadot does not enable autonat.** The `sc-network` crate
configures libp2p with `identify`, `kad`, `ping`, `mdns`, `noise`,
`tcp`, `websocket`, `yamux` — but not `autonat`. Substrate nodes
typically run on servers with public IPs and don't need NAT detection.

This means rust-libp2p's autonat v2 has **no known production
deployment** in a major project. The autonat↔DHT interaction described
above has likely never been exercised at scale.

### Comparison with go-libp2p

| Aspect | go-libp2p | rust-libp2p |
|--------|-----------|-------------|
| DHT mode trigger | `EvtLocalReachabilityChanged` (v1 event) | `FromSwarm::ExternalAddrConfirmed` |
| AutoNAT version used | v1 majority vote | v2 per-address (if autonat enabled) |
| Oscillation risk | v1 flips → DHT flips | No oscillation (but may never reach server) |
| Production deployment | Kubo (tens of thousands of nodes) | None (Substrate doesn't use autonat) |

The key difference: go-libp2p's DHT oscillation problem comes from v1's
flaky majority vote. rust-libp2p doesn't have this problem but has the
opposite one — the DHT may never enter server mode because autonat v2's
address selection is broken.

### Implementation Maturity

rust-libp2p's autonat v2 was introduced in v0.54.1 (August 2024),
roughly 3 months after go-libp2p's initial implementation. However,
go-libp2p has had significantly more iteration — including the
`addrsReachabilityTracker` (v0.42.0), confidence system, and address
grouping. The absence of observed address consolidation, confidence
tracking, and production autonat deployment may reflect the fact that
rust-libp2p's primary consumer (Substrate) doesn't use autonat at all.

---

## References

- [AutoNAT v2 Protocol Walkthrough](autonat-v2.md)
- [AutoNAT v2 Specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
- [rust-libp2p autonatv2 source](https://github.com/libp2p/rust-libp2p/tree/master/protocols/autonat/src/v2)
- [rust-libp2p autonat v2 PR #5526](https://github.com/libp2p/rust-libp2p/pull/5526)
- [Address candidate issue #4873](https://github.com/libp2p/rust-libp2p/issues/4873)
- [go-libp2p AutoNAT v2 Implementation](go-libp2p-autonat-implementation.md)
- [go-libp2p observed address manager](https://github.com/libp2p/go-libp2p/blob/master/p2p/host/observedaddrs/manager.go)
