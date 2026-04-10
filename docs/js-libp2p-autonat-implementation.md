# js-libp2p AutoNAT v2 Implementation

Implementation details for the AutoNAT v2 subsystem in
[js-libp2p](https://github.com/libp2p/js-libp2p). For the protocol
specification, see [autonat-v2.md](autonat-v2.md). For the other
implementations, see
[go-libp2p](go-libp2p-autonat-implementation.md) and
[rust-libp2p](rust-libp2p-autonat-implementation.md).

Based on `@libp2p/autonat-v2` **v2.0.13** (2026-03-12).

> **Note:** AutoNAT v2 is in a separate package (`@libp2p/autonat-v2`)
> from v1 (`@libp2p/autonat`). The v1 README explicitly states "AutoNAT v2
> is now available and should be preferred to this module."

---

## Architecture Overview

js-libp2p's AutoNAT v2 implementation is the simplest of the three
implementations. The client and server are combined into a single service
factory (`autoNATv2()`) that registers both roles.

### Key Differences from go-libp2p

| Aspect | go-libp2p | js-libp2p |
|--------|-----------|-----------|
| **Architecture** | Separate orchestrator + client + server + tracker | Single service with client.ts + server.ts |
| **Confidence system** | Sliding window (5 results), targetConfidence=3 | Fixed thresholds: 4 successes or 8 failures |
| **Observed vs. announced** | Uniform treatment | Different thresholds (observed need 4; announced need 1) |
| **Address filtering** | `ObservedAddrManager` with activation threshold | `getUnverifiedMultiaddrs()` with cuckoo filter dedup |
| **Re-probe scheduling** | 5min refresh, 1h/3h high-confidence, exponential backoff | 60s peer discovery interval (hardcoded) |
| **Server selection** | Random shuffle with 2min per-peer throttle | Queue-based with 3 concurrent peers (hardcoded) |
| **Event model** | `EvtHostReachableAddrsChanged` (aggregated) | **None** — no events emitted to consumers |
| **Dial-back host** | Separate `dialerHost` with fresh key+peerstore | Same peer identity (nonce-based verification only) |
| **Black hole detection** | UDP/IPv6 success counters | Not implemented |
| **OTel tracing** | Full spans (probe-lab fork) | None — logging + optional metrics only |
| **Primary/secondary grouping** | Yes (secondary inherits reachability) | No |

---

## Source Layout

```
packages/protocol-autonat-v2/src/
  autonat.ts       Service orchestrator: creates client + server
  client.ts        Client: address verification, candidate management, dial results
  server.ts        Server: incoming dial-request handling, dial-back
  constants.ts     Protocol constants
  utils.ts         Helpers
  index.ts         Public API exports and interfaces
  pb/
    index.proto    Protobuf message definitions
    index.ts       Generated TypeScript code
```

---

## Key Types

### Service Factory

```typescript
export function autoNATv2(init?: AutoNATv2ServiceInit): (components: AutoNATv2Components) => AutoNATv2
```

Returns a service that registers both client and server behaviours.

### Configuration (`AutoNATv2ServiceInit`)

| Option | Default | Description |
|--------|---------|-------------|
| `timeout` | 30,000 ms | Timeout for dial/verify operations |
| `maxInboundStreams` | 2 | Concurrent inbound streams per connection (server) |
| `maxOutboundStreams` | 20 | Concurrent outbound streams per connection |
| `connectionThreshold` | 80% | Revalidate verified addresses when connection usage exceeds this |
| `maxMessageSize` | 8,192 bytes | Maximum incoming message size |
| `maxDialDataBytes` | 200,000 bytes | Maximum amplification data to send |
| `dialDataChunkSize` | 4,096 bytes | Chunk size for amplification data |
| `startupDelay` | — | **Accepted but unused** in implementation |
| `refreshInterval` | — | **Accepted but unused** in implementation |

### Hardcoded Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `REQUIRED_SUCCESSFUL_DIALS` | 4 | Successes to confirm reachable |
| `REQUIRED_FAILED_DIALS` | 8 | Failures to confirm unreachable |
| Peer queue concurrency | 3 | Max concurrent peer verifications |
| Peer queue max size | 50 | Pending operations backlog |
| Peer discovery interval | 60,000 ms | Random peer discovery repeating task |
| Peer discovery timeout | 10,000 ms | Timeout per discovery walk |

---

## Address Candidate Selection

js-libp2p determines probe candidates via `getUnverifiedMultiaddrs()`:

### Address Sources

Addresses come from `addressManager.getAddressesWithMetadata()`, which
includes both **observed** addresses (from identify) and **announced**
addresses (explicitly configured or discovered via UPnP/NAT-PMP).

### Filtering Pipeline

1. **Expiration check** — only addresses past their TTL are candidates
2. **Priority sorting** — announced addresses prioritized over observed
3. **IPv6 compatibility** — peer must support IPv6; address must be globally routable
4. **Private address exclusion** — filters out private IP ranges
5. **Network segment dedup** — one verification per network segment per address
6. **Cuckoo filter** — scalable bloom filter (capacity: 1024) prevents redundant probes

### Comparison with Other Implementations

| Aspect | go-libp2p | rust-libp2p | js-libp2p |
|--------|-----------|-------------|-----------|
| Address source | ObservedAddrManager (filtered) | `NewExternalAddrCandidate` (raw) | `addressManager` (metadata-enriched) |
| Deduplication | Activation threshold (N=4) | None | Cuckoo filter + network segment dedup |
| Ephemeral port problem | No (threshold filters them) | **Yes** (all candidates probed) | No (uses address manager, not raw observations) |
| Batch vs. individual | Individual per probe | Individual per probe | Batch request, sequential processing |

js-libp2p's address manager provides richer metadata than rust-libp2p's
raw candidate events, which helps filter out ephemeral addresses.

---

## Confidence System

js-libp2p uses **monotonic counters with fixed thresholds** instead of
go-libp2p's sliding window. This design is significantly more resistant
to oscillation.

### Thresholds

- **Reachable**: 4 successful dials (`REQUIRED_SUCCESSFUL_DIALS`)
- **Unreachable**: 8 failed dials (`REQUIRED_FAILED_DIALS`)

### Counter Behavior

Counters are **monotonic** — they only increment, never decrement.
Success and failure counters are independent. The first threshold reached
wins:

```
success++ on each successful dial
failure++ on each failed dial

if success >= 4 → confirmAddress() → delete DialResults, set TTL
if failure >= 8 → unconfirmAddress() → delete DialResults, remove addr
```

Once either threshold is reached, the entire `DialResults` entry is
**deleted**. The address is either confirmed with a TTL in the address
manager, or removed entirely. There is no further counting until the
TTL expires and re-verification starts from scratch.

### Observed vs. Announced Addresses

Announced (explicitly configured) addresses are confirmed on first
success. Observed addresses require the full 4-success threshold. This
reflects lower trust in addresses learned through identify observations.

### Re-verification Cycle

After an address is confirmed, it stays confirmed for its TTL duration.
When the TTL expires:

1. `getFirstUnverifiedMultiaddr()` detects `addr.expires < Date.now()`
2. The cuckoo filter entry is cleared to allow retesting
3. A fresh `DialResults` object is created (counters start at 0)
4. Re-verification begins — needs 4 new successes or 8 new failures

The 60-second `findRandomPeers` repeating task provides the periodic
trigger for discovering peers and initiating verification.

### Oscillation Resistance

This design is **much more resistant to oscillation** than go-libp2p's
v1 sliding window:

| Factor | go-libp2p v1 | js-libp2p v1 |
|--------|-------------|-------------|
| Counter type | Net confidence (success - failure) | Independent monotonic counters |
| A single failure after confirmation | Decrements confidence → may flip | No effect (entry deleted, TTL protects) |
| Mixed reliable/unreliable servers | Confidence bounces → oscillation | Successes accumulate independently of failures |
| Threshold to flip from public to private | Confidence drops to 0 (can be 1 failure) | 8 failures from fresh start (after TTL expiry) |
| Protection period after confirmation | None (continuous re-evaluation) | Full TTL duration (no new counting) |

**Example with 2 reliable + 5 unreliable servers:**

go-libp2p v1:
```
success → confidence=1 → failure → confidence=0 → failure → PRIVATE
success → confidence=1 → PUBLIC → failure → confidence=0 → failure → PRIVATE
(oscillates)
```

js-libp2p v1:
```
success=1,fail=0 → success=2,fail=1 → success=3,fail=2 → success=4 → CONFIRMED (TTL set)
... TTL period: stays confirmed regardless of failures ...
TTL expires → fresh counters → success=1,fail=0 → ... → likely confirms again
```

The key insight: in js-libp2p, failures during the counting phase don't
undo successes. With 2 reliable out of 7 servers, the success counter
reaches 4 (after ~7 probes on average) well before the failure counter
reaches 8. And once confirmed, the TTL prevents any re-evaluation.

### Comparison

| Aspect | go-libp2p v1 | rust-libp2p v2 | js-libp2p v1 |
|--------|-------------|----------------|-------------|
| Model | Sliding window (last 5) | Single probe | Monotonic counters + TTL |
| Success criterion | net confidence ≥ 2 | 1 success | 4 successes (observed) or 1 (announced) |
| Failure criterion | net confidence ≤ -2 | 1 failure | 8 failures |
| Oscillation risk | **High** (failures undo successes) | **High** (single probe flips) | **Low** (counters are independent, TTL protects) |
| Re-evaluation | Continuous | Continuous | Only after TTL expiry |
| Backoff | Exponential (5s → 5min) | None | None (TTL acts as implicit backoff) |

---

## Server-Side Dial-Back

### Dial-Back Identity

js-libp2p does **not** use a separate peer ID for dial-back. The server
dials back from the same identity using `connectionManager.openConnection()`.
Verification relies solely on nonce matching.

| Aspect | go-libp2p | rust-libp2p | js-libp2p |
|--------|-----------|-------------|-----------|
| Separate dial-back identity | Yes (fresh key) | No | No |
| Nonce verification | Yes | Yes | Yes |
| Fresh peerstore | Yes | No | No |
| Force direct dial | Yes | No | No |

### Amplification Prevention

Same as go-libp2p: when the client's observed IP differs from the
dial-back target IP, the server requires 30,000–100,000 bytes of data
before proceeding. Data is sent in 4,096-byte chunks.

### Rate Limiting

Limited compared to go-libp2p:

| Mechanism | go-libp2p | js-libp2p |
|-----------|-----------|-----------|
| Global RPM | 60 | None |
| Per-peer RPM | 12 | None |
| Dial-data RPM | 12 | None |
| Max concurrent streams | 2 per connection | 2 per connection |
| Timeout | 15s stream | 30s operation |

---

## Event Model

### Critical Limitation: No Events Emitted

js-libp2p's AutoNAT v2 **does not emit any events** to external consumers.
There is no equivalent of go-libp2p's `EvtHostReachableAddrsChanged`.

Reachability state is tracked internally in a `dialResults` Map but is
not exposed via EventEmitter, callback, or any observable mechanism.

### Implications for the Testbed

Our JS testbed client uses `self:peer:update` events (from the address
manager) as a proxy for reachability changes. This event fires when the
node's advertised addresses change, which correlates with but is not
identical to AutoNAT v2 reachability decisions.

| Implementation | Reachability event | Testbed span source |
|----------------|-------------------|---------------------|
| go-libp2p | `EvtHostReachableAddrsChanged` | Direct event subscription |
| rust-libp2p | `autonat::v2::client::Event` | Direct event handling |
| js-libp2p | **None** | `self:peer:update` proxy |

This means the `reachable_addrs_changed` spans from the JS client may
not perfectly match the internal AutoNAT v2 state — they reflect address
manager changes which lag behind or differ from actual probe results.

---

## OTel / Tracing

The `@libp2p/autonat-v2` package has **no OpenTelemetry instrumentation**.

**Available observability:**
- Structured logging via `@libp2p/logger` (trace, info, error levels)
- Optional metrics: `libp2p_autonat_v2_dial_results` (if metrics component is configured)

For the testbed, we emit spans at the application layer:

| Span name | Source event | Attributes |
|-----------|-------------|------------|
| `started` | Node startup | `peer_id`, `message` |
| `connected` | Successful `node.dial()` | `peer_id` |
| `reachable_addrs_changed` | `self:peer:update` event | `reachable[]`, `unreachable[]`, `unknown[]` |
| `shutdown` | SIGTERM/SIGINT | `message` |
| `peer_discovery_start` | Before connecting to servers | `message` |
| `peer_discovery_done` | After connecting to servers | `message` |

---

## Spec Compliance

| Spec requirement | js-libp2p status |
|---|---|
| Client sends `DialRequest{addrs, nonce}` | Matches |
| Server picks first dialable address in order | Matches |
| Server SHOULD NOT dial private addresses | Matches (implicit via address filtering) |
| Client SHOULD NOT send private addresses | Matches (address manager filters) |
| Amplification prevention when IP differs | Matches |
| Dial data: 30k-100k bytes | Matches |
| Dial data chunks max 4096 bytes | Matches |
| Server uses separate peer ID for dial-back | **No** — same identity, nonce-only verification |
| Nonce verification by client | Matches |
| Client SHOULD NOT verify peer ID on dial-back | Matches |

---

## Known Issues and Limitations

### 1. No Reachability Events

Consumers cannot subscribe to reachability changes. The internal
`dialResults` map is not exposed. This makes it difficult to build
reactive systems that respond to NAT status changes.

### 2. Unused Configuration Options

`startupDelay` and `refreshInterval` are accepted in the config
interface but not used in the implementation. This may be dead code
from an earlier design.

### 3. Hardcoded Intervals

The peer discovery interval (60s) and queue concurrency (3) are
hardcoded constants, unlike go-libp2p where these are configurable.

### 4. No Separate Dial-Back Identity

Using the same peer identity for dial-back means the client may already
have a connection to the server's peer ID when the dial-back arrives.
The spec recommends a separate identity to ensure the dial-back truly
tests network reachability rather than reusing an existing connection.

### 5. No OTel Instrumentation

Only structured logging; no distributed tracing support.

### 6. Testbed Event Proxy

Since no AutoNAT events are emitted, the testbed uses `self:peer:update`
as a proxy. This may produce `reachable_addrs_changed` spans that don't
precisely reflect the AutoNAT v2 probe results.

### 7. TCP Observed Address Exclusion (Node.js Platform Limitation)

js-libp2p's Identify protocol unconditionally drops all TCP observed
addresses. This is not a bug — it's a deliberate workaround for a
Node.js platform limitation.

**Root cause:** Node.js TCP sockets (`net.createConnection()`) do not
support `SO_REUSEPORT`. When a libp2p node dials out over TCP, the OS
assigns a random ephemeral source port instead of reusing the listener
port (4001). The remote peer then sees a different port for every
connection:

```
Go/Rust (port reuse works):
  Dial to peer A → source port 4001 → peer A sees 79.x.x.x:4001
  Dial to peer B → source port 4001 → peer B sees 79.x.x.x:4001
  → consistent observed address → feeds into AutoNAT v2

Node.js (no port reuse):
  Dial to peer A → source port 52341 → peer A sees 79.x.x.x:52341
  Dial to peer B → source port 38712 → peer B sees 79.x.x.x:38712
  → flood of unique addresses → looks like symmetric NAT
```

**Workaround in js-libp2p:** The Identify protocol drops all TCP
observed addresses to prevent this noise from polluting the address
manager:

```typescript
// packages/protocol-identify/src/identify.ts
if (TCP.exactMatch(cleanObservedAddr)) {
  // TODO: because socket dials can't use the same local port as the TCP
  // listener, many unique observed addresses are reported so ignore all
  // TCP addresses until https://github.com/libp2p/js-libp2p/issues/2620
  // is resolved
  return
}
```

**Consequence:** TCP reachability can never be discovered through the
Identify → AutoNAT v2 pipeline. The only way for a js-libp2p node to
learn its TCP external address is through UPnP (`@libp2p/upnp-nat`) or
explicit configuration. This affects all NAT types, not just symmetric
NAT.

QUIC is not affected — `@chainsafe/libp2p-quic` uses UDP sockets which
support port reuse in Node.js. QUIC observed addresses pass through
Identify normally.

| Transport | Port reuse | Identify passes observed addr? | AutoNAT v2 can verify? |
|-----------|-----------|-------------------------------|----------------------|
| TCP | No (Node.js limitation) | No (dropped) | Only via UPnP or manual config |
| QUIC | Yes (UDP supports it) | Yes | Yes |

**Upstream tracking:**
[js-libp2p#2620](https://github.com/libp2p/js-libp2p/issues/2620)

### 8. QUIC Silent Failure Under Symmetric NAT (F4)

While TCP observed addresses are dropped entirely (§7 above), QUIC
observed addresses do enter the pipeline and reach AutoNAT v2. Under
symmetric NAT, each outbound QUIC connection uses a different external
port, so every dial-back fails — the server dials back to the port it
saw, but the NAT only accepts traffic from the original destination on
that port.

The failure path:
1. QUIC observed address enters the address manager (not dropped)
2. AutoNAT v2 probes the address
3. Server dial-back fails (symmetric NAT blocks it)
4. After 8 failures (`REQUIRED_FAILED_DIALS`), the address is removed
5. Since js-libp2p emits **no reachability events** (§1 above), the
   failure is silent from the application's perspective

The node remains in Unknown state indefinitely — no explicit
UNREACHABLE signal is ever produced. This is the js-libp2p side of
Finding 4 (symmetric NAT missing signal). Combined with the TCP
exclusion (§7), js-libp2p nodes behind symmetric NAT have no path to
a reachability determination on any transport.

---

## Testbed Integration

### Docker image

- **Base**: `node:22-alpine`
- **Runtime**: Node.js 22 with ES modules
- **Entrypoint**: Reuses the Go node's `entrypoint.sh` (gateway/routing setup)
- **Build time**: ~18s (npm install + tsc)

### CLI interface

Same flags as the Go and Rust clients:

```
--role=client          Node role
--transport=both       Transport: tcp, quic, both
--port=4001            Listen port
--peer-dir=/peer-addrs Directory with server multiaddr files
--otlp-endpoint=URL    OTLP HTTP endpoint for Jaeger
--trace-file=PATH      Write JSONL spans to file
```

### Compose services

| Service | Profile | Network | IP |
|---------|---------|---------|-----|
| `client-js` | `js` | private-net | 10.0.1.10 |
| `client-js-nonat` | `js-nonat` | public-net | 73.0.0.102 |
| `client-js-mock` | `js-mock` | public-net | 73.0.0.102 |

### QUIC Transport

QUIC is provided by `@chainsafe/libp2p-quic` v2.0.0 (requires Node.js ≥ 22).
It is loaded dynamically and falls back to TCP-only if unavailable.

---

## DHT and AutoNAT Interaction

### DHT Mode Switching Mechanism

js-libp2p's Kademlia DHT (`@libp2p/kad-dht`) has automatic mode
switching via `self:peer:update` events. From `kad-dht.ts`:

```typescript
// When clientMode is not explicitly set, auto-switch based on addresses
if (init.clientMode == null) {
  components.events.addEventListener('self:peer:update', (evt) => {
    const hasPublicAddress = evt.detail.peer.addresses
      .some(({ multiaddr }) => {
        return !isPrivate(multiaddr) && !Circuit.exactMatch(multiaddr)
      })
    if (hasPublicAddress && mode === 'client') {
      await this.setMode('server')
    } else if (mode === 'server' && !hasPublicAddress) {
      await this.setMode('client')
    }
  })
}
```

The DHT listens for **address changes** (not autonat events directly)
and checks whether the node has any non-private, non-relay addresses.
If public addresses appear → switch to server mode. If they disappear →
switch to client mode.

### How AutoNAT v1 Feeds Into DHT

The connection between autonat v1 and the DHT is **indirect**, flowing
through the address manager and peer store. The full chain:

```
AutoNAT v1 probe succeeds (4 successful dials)
  → autonat calls addressManager.confirmObservedAddr(multiaddr)
    → confirmObservedAddr() updates address confidence (verified: true, TTL set)
      → calls _updatePeerStoreAddresses()
        → calls peerStore.patch(selfPeerId, { multiaddrs: [...] })
          → peerStore emits 'self:peer:update' event (because patched ID == self)
            → DHT's event listener fires
              → checks if any address is !isPrivate && !Circuit
                → if yes → setMode('server')
                → if no  → setMode('client')
```

**AutoNAT v1's `confirmAddress()` method:**
```typescript
confirmAddress(results: DialResults): void {
  this.components.addressManager.confirmObservedAddr(results.multiaddr)
  this.dialResults.delete(results.multiaddr.toString())
  results.result = true
}
```

**AddressManager's `confirmObservedAddr()`** updates the address's
verified status and TTL, then calls `_updatePeerStoreAddresses()` which
patches the peer store with the full current address list.

**PeerStore's `patch()`** emits `self:peer:update` when the patched peer
ID matches the node's own ID:
```typescript
#emitIfUpdated(id, result) {
  if (this.peerId.equals(id)) {
    this.events.safeDispatchEvent('self:peer:update', { detail: result })
  }
}
```

The same chain works for `unconfirmAddress()` (after 8 failures), which
calls `addressManager.removeObservedAddr()` → updates peer store →
triggers `self:peer:update` → DHT re-evaluates and may switch to client.

### AutoNAT v2 Would Use the Same Path

AutoNAT v2 (`@libp2p/autonat-v2`) also calls
`addressManager.confirmObservedAddr()` on success, so the same
address manager → peer store → DHT chain would work. The key difference
is that v2 emits no application-level events of its own — but the DHT
doesn't need them since it listens to `self:peer:update` from the peer
store layer.

### Helia: v1 Only, No v2

**Helia uses `@libp2p/autonat` (v1, `^3.0.5`) — not v2.** The default
libp2p configuration includes:

```typescript
autoNAT: autoNAT()  // v1
dht: kadDHT({ validators: { ipns: ... }, selectors: { ipns: ... } })
```

There is no explicit wiring between autonat and DHT in Helia's config.
The connection happens implicitly through the `self:peer:update` event
mechanism described above. AutoNAT v1 updates the address manager →
triggers peer update → DHT evaluates mode.

AutoNAT v2 (`@libp2p/autonat-v2`) is published on npm but is **not used
by Helia or any other known major JS project**.

### Comparison Across Implementations

| Aspect | go-libp2p | rust-libp2p | js-libp2p |
|--------|-----------|-------------|-----------|
| DHT mode trigger | `EvtLocalReachabilityChanged` (v1) | `ExternalAddrConfirmed` (swarm event) | `self:peer:update` (address change) |
| AutoNAT version used by DHT | v1 (majority vote) | v2 (if enabled) | v1 (via address manager) |
| Coupling | Direct event subscription | Swarm-level event | Indirect via address manager |
| Oscillation risk | v1 flips → DHT flips | None (never reaches server) | Depends on address manager stability |
| Production consumer | Kubo (tens of thousands) | None (Substrate skips autonat) | Helia (v1 only) |
| AutoNAT v2 in production | Yes (Kubo) | No | No |

### DHT Impact Summary

| Implementation | AutoNAT issue | DHT consequence |
|---|---|---|
| **go-libp2p** | v1 oscillation with unreliable servers | DHT server↔client oscillation, routing table churn |
| **rust-libp2p** | v2 probes ephemeral ports, nothing confirmed | DHT stuck in client mode permanently |
| **js-libp2p** | v2 not deployed (Helia uses v1) | v1 oscillation possible (same as go-libp2p) |

### Implementation Maturity

js-libp2p's autonat v2 was introduced in June 2025, over a year after
go-libp2p's initial implementation. It is the newest of the three and
has **no known production deployment**. Helia (the primary JS consumer)
still uses v1. The absence of reachability events, configurable
intervals, and production deployment may reflect that v2 is available as
a library but not yet integrated into any shipping product.

---

## Cross-Implementation Summary

| Feature | go-libp2p | rust-libp2p | js-libp2p |
|---------|-----------|-------------|-----------|
| **Maturity** | Primary (since v0.34.0, May 2024) | Second (since v0.54.1, Aug 2024) | Third (since June 2025) |
| **Confidence** | Sliding window, configurable | None (single probe) | Fixed thresholds (4/8) |
| **Address filtering** | ObservedAddrManager (threshold) | None (ephemeral port issue) | Address manager + cuckoo filter |
| **Reachability events** | `EvtHostReachableAddrsChanged` | Per-probe `Event` struct | **None** |
| **Dial-back identity** | Separate host | Same swarm | Same identity |
| **Rate limiting** | 60 RPM global, 12 per-peer | Basic concurrent limit | Stream limits only |
| **OTel** | Full spans (fork) | None | None |
| **Black hole detection** | Yes (UDP/IPv6) | No | No |
| **Testbed status** | Primary client | Builds, connects, probes ephemeral ports | Builds, connects, emits spans |

---

## References

- [AutoNAT v2 Protocol Walkthrough](autonat-v2.md)
- [AutoNAT v2 Specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
- [js-libp2p autonat-v2 source](https://github.com/libp2p/js-libp2p/tree/main/packages/protocol-autonat-v2)
- [js-libp2p autonat-v2 on npm](https://www.npmjs.com/package/@libp2p/autonat-v2)
- [go-libp2p AutoNAT v2 Implementation](go-libp2p-autonat-implementation.md)
- [rust-libp2p AutoNAT v2 Implementation](rust-libp2p-autonat-implementation.md)
