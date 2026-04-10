# AutoNAT v1 vs v2: State Transitions, Wiring Gap, and Fix Options

How each AutoNAT implementation decides a node is reachable or
unreachable, how it changes that decision, how the decision propagates
to other protocols, and where the wiring breaks in go-libp2p. This
document is the deep-dive companion to
[Finding 1](final-report.md#finding-1-inconsistent-global-vs-per-address-reachability-v1-vs-v2)
in the final report.

**Implementations compared:**
- go-libp2p v0.47.0 (v1 + v2)
- rust-libp2p v0.54 / libp2p-autonat v0.15.0 (v1 + v2)
- js-libp2p / @libp2p/autonat v3, @libp2p/autonat-v2 v2.0.13 (v1 + v2)

For protocol-level details see [autonat-v2.md](autonat-v2.md).
For per-implementation internals see
[go-libp2p](go-libp2p-autonat-implementation.md),
[rust-libp2p](rust-libp2p-autonat-implementation.md),
[js-libp2p](js-libp2p-autonat-implementation.md).

---

## 1. State Models

### v1: Global Reachability

All three implementations model v1 reachability as a single global
state with three values:

| State | go-libp2p | rust-libp2p | js-libp2p |
|-------|-----------|-------------|-----------|
| Unknown | `network.ReachabilityUnknown` | `NatStatus::Unknown` | No explicit state (no addresses confirmed) |
| Public / Reachable | `network.ReachabilityPublic` | `NatStatus::Public` | Address confirmed in address manager (TTL set) |
| Private / Unreachable | `network.ReachabilityPrivate` | `NatStatus::Private` | Address removed from address manager |

**js-libp2p is the outlier**: it has no global reachability enum. v1
results flow through the address manager on a per-address basis. The
"global" state is derived implicitly by consumers checking whether any
public address exists.

### v2: Per-Address Reachability

| State | go-libp2p | rust-libp2p | js-libp2p |
|-------|-----------|-------------|-----------|
| Unknown | Confidence between -2 and +2 | Not yet probed | Not yet reached 4 successes or 8 failures |
| Reachable | Confidence ≥ +2 (min) or +3 (target) | Single successful probe → `ExternalAddrConfirmed` | 4 successful dials → `confirmObservedAddr()` |
| Unreachable | Confidence ≤ -2 (min) or -3 (target) | Single failed probe (remains candidate) | 8 failed dials → `removeObservedAddr()` |

---

## 2. Server Selection

How each implementation picks which peer to ask for a dial-back probe.

### go-libp2p v1

**Algorithm:** Random shuffle of all connected peers, pick first
eligible.

```go
// autonat.go — getPeerToProbe()
peers := as.host.Network().Peers()
// Fisher-Yates shuffle
for n := len(peers); n > 0; n-- {
    randIndex := rand.Intn(n)
    peers[n-1], peers[randIndex] = peers[randIndex], peers[n-1]
}
for _, p := range peers {
    if proto, _ := as.host.Peerstore().SupportsProtocols(p, AutoNATProto); len(proto) == 0 {
        continue  // must support /libp2p/autonat/1.0.0
    }
    if as.config.dialPolicy.skipPeer(info.Addrs) {
        continue  // address-based filter
    }
    return p
}
```

**Filters:** Protocol support (`/libp2p/autonat/1.0.0`), address-based
dial policy (`skipPeer`), per-peer throttle (90s via `recentProbes`
map), max 5 pending probes.

**Trigger:** Timer-based polling. `retryInterval=90s` between probes,
`refreshInterval=15min` for periodic refresh.

**Problem:** Any connected peer supporting the protocol is eligible —
including peers behind their own NAT that can't dial back. A single
failed dial-back from such a peer counts as evidence of Private
reachability, directly causing oscillation.

### go-libp2p v2

**Algorithm:** Random rotation through `peersMap` (connected peers
that support the dial-request protocol), pick first non-throttled.

```go
// autonat.go — GetReachability(), called by addrsReachabilityTracker
for pr := range an.peers.Shuffled() {
    if t := an.throttlePeer[pr]; t.After(now) {
        continue
    }
    p = pr
    an.throttlePeer[p] = time.Now().Add(an.throttlePeerDuration)
    break
}
```

`Shuffled()` picks a random start index and iterates the array
wrapping around — a random rotation, not a full Fisher-Yates shuffle.

**Filters:** Protocol support (`/libp2p/autonat/2/dial-request`),
connectedness confirmed, per-peer throttle (**2 minutes** — longer
than v1's 90s).

**Trigger:** Caller-initiated. `addrsReachabilityTracker` calls
`GetReachability()` during refresh cycles (5 min) or on new address
detection (1s delay).

**Key difference from v1:** Only peers that explicitly support the v2
protocol are eligible. v1 servers are purpose-built (they register the
protocol handler), so they are expected to be able to dial back. This
eliminates the "random DHT peer behind its own NAT" problem.

### rust-libp2p v1

**Algorithm:** Random selection (`Vec::choose`) from combined explicit
server list + connected peers.

```rust
// v1/behaviour/as_client.rs — random_server()
let mut servers: Vec<&PeerId> = self.servers.iter().collect();

if self.config.use_connected {
    servers.extend(self.connected.iter().filter_map(|(id, addrs)| {
        addrs.values().any(|a| a.is_some()).then_some(id)
    }));
}

// Remove throttled servers
servers.retain(|s| !self.throttled_servers.iter().any(|(id, _)| s == &id));

servers.choose(&mut thread_rng()).map(|&&p| p)
```

**Filters:** Explicit servers always included (no protocol check
needed), connected peers optionally included if they have non-relayed
global-IP connections, per-peer throttle (90s via `throttled_servers`).

**Notable:** rust-libp2p v1 has an **explicit server list** via
`add_server()` that applications can populate (e.g., Avail adds
bootstrap nodes). Connected peers are additionally included when
`use_connected=true` (default). No protocol support check — relies on
request-response protocol negotiation to handle unsupported peers.

**Trigger:** Timer-based. `retry_interval=90s`, `refresh_interval=15min`.

### rust-libp2p v2

**Algorithm:** Random selection (`Iterator::choose`) from `peer_info`
map filtered to peers with `supports_autonat == true`.

```rust
// v2/client/behaviour.rs — random_autonat_server()
let (conn_id, info) = self
    .peer_info
    .iter()
    .filter(|(_, info)| info.supports_autonat)
    .choose(&mut self.rng)?;
```

**Filters:** Protocol support (handler reports
`PeerHasServerSupport`). **No per-peer throttle** — the same server
can be picked multiple times in the same tick for different addresses.

**Address dispatch:** Each untested candidate address is sent to a
**different** randomly selected server (one address per server per
tick). This spreads the load but means a single server can still be
selected for multiple addresses.

**Trigger:** Timer-based, `probe_interval=5s`.

### js-libp2p v1

**Algorithm:** Topology-driven, not polling. Peers are probed as they
connect, triggered by the protocol topology `onConnect` callback.

```typescript
// autonat.ts — registrar.register(this.protocol, {
//   onConnect: (peerId, connection) => this.verifyExternalAddresses(connection)
// })
```

When a peer supporting `/libp2p/autonat/1.0.0` connects, the `onConnect`
callback fires and initiates verification for the first unverified
address eligible for that peer.

**Filters:**
- Protocol support: topology registration ensures only supporting peers
  trigger `onConnect`
- **Network segment diversity**: tracks which `/8` (IPv4) or first hex
  group (IPv6) segments have already verified each address — skips
  peers from the same segment. **Unique to js-libp2p.**
- IPv6 capability: only sends IPv6 addresses to peers with IPv6
  addresses
- Per-address cuckoo filter (capacity 1024): prevents redundant probes
- Per-peer dedup: `verifyingPeers` PeerSet
- Connection capacity: skips if connections exceed `connectionThreshold`
  (default 90%)

**Background discovery:** `findRandomPeers` repeating task (60s) does
random DHT walks to discover new peers when addresses remain unverified.

**Notable:** This is the only implementation with IP diversity
enforcement. Go and Rust will probe the same address from multiple
peers in the same `/8` subnet, which provides less independent
confirmation.

### js-libp2p v2

**Algorithm:** Same topology-driven approach as v1.

**Differences from v1:**
- Uses a **single global `PeerQueue`** (concurrency 3, max 50) instead
  of per-address queues
- Sends **all** unverified addresses in a single dial request (v2
  protocol supports multi-address requests)
- Same network segment diversity and IPv6 filters as v1

### Server Selection Summary

| Aspect | Go v1 | Go v2 | Rust v1 | Rust v2 | JS v1 | JS v2 |
|--------|-------|-------|---------|---------|-------|-------|
| **Algorithm** | Random shuffle | Random rotation | `Vec::choose` | `Iterator::choose` | Topology-driven | Topology-driven |
| **Trigger** | Timer (90s/15min) | Caller (5min refresh) | Timer (90s/15min) | Timer (5s) | On peer connect + 60s walk | On peer connect + 60s walk |
| **Protocol check** | Peerstore lookup | Peerstore + connected | No (req-resp handles) | Handler flag | Topology registration | Topology registration |
| **Per-peer throttle** | 90s | **2 min** | 90s | **None** | None (segment dedup) | None (segment dedup) |
| **IP diversity** | No | No | `only_global_ips` filter | No | **Yes** (/8 segment) | **Yes** (/8 segment) |
| **Explicit servers** | No | No | **Yes** (`add_server`) | No | No | No |
| **Addrs per request** | All listen addrs | Caller-provided batch | All listen addrs | 1 per server | 1 per peer | All unverified |

---

## 3. Confidence Systems Compared

### go-libp2p v1: Sliding Window of 3

**Data structure:** Circular buffer of the last `maxConfidence=3`
results (success/failure booleans).

**Determination:**
```
count successes and failures in the window

if successes > failures → Public
if failures > successes → Private
if equal                → retain previous state (or Unknown if first)
```

**Critical property:** A single new failure replaces the oldest result
in the window. With window size 3, the state can flip from Public to
Private with just **2 consecutive failures** — window goes from
[S,S,S] → [S,S,F] (still Public) → [S,F,F] (Private).

### go-libp2p v2: Sliding Window of 5 with Confidence Thresholds

**Data structure:** Circular buffer of the last
`maxRecentDialsWindow=5` results.

**Determination:**
```
confidence = successes - failures  (within window)

confidence ≥ +2 (minConfidence)  → Public
confidence ≤ -2 (minConfidence)  → Private
otherwise                        → Unknown (keep probing)
```

**High confidence** at `targetConfidence=3` triggers longer re-probe
intervals (1h primary, 3h secondary addresses).

**Transition from Reachable (+3) to Unreachable (-2):** Starting from
all-success [S,S,S,S,S] (conf=+5), needs enough failures pushed into
the window to reach -2. Requires **at least 4 consecutive failures**:
[S,F,F,F,F] → conf=-2.

**Transition from Unreachable (-3) to Reachable (+2):** Symmetric —
at least 4 consecutive successes needed.

### rust-libp2p v1: Clamped Counter

**Data structure:** Single integer counter, clamped to
`[-confidence_max, +confidence_max]` where `confidence_max=3`.

**Determination:**
```
on success: confidence = min(confidence + 1, +3)
on failure: confidence = max(confidence - 1, -3)

confidence > 0  → Public
confidence < 0  → Private
confidence == 0 → Unknown
```

**Transition from Public (+3) to Private (-1):** Requires **4
consecutive failures** (3→2→1→0→-1). More stable than go v1's window
of 3 but still vulnerable to a sustained run of failures.

### rust-libp2p v2: No Confidence System

Single probe determines the result. Success emits
`ExternalAddrConfirmed`. Failure leaves address as a candidate.

No sliding window, no accumulation, no hysteresis. The swarm's address
TTL is the only mechanism preventing rapid state changes.

### js-libp2p v1 and v2: Monotonic Counters with TTL

**Data structure:** Two independent counters per address — `successes`
and `failures` — that only increment.

```
REQUIRED_SUCCESSFUL_DIALS = 4
REQUIRED_FAILED_DIALS     = 8

on success: successes++
on failure: failures++

if successes >= 4 → confirmAddress() (set TTL, delete counters)
if failures  >= 8 → unconfirmAddress() (remove address, delete counters)
```

**Transition from Reachable to Unreachable:** After confirmation, the
address is protected by its TTL. No counting during the TTL period.
After expiry, fresh counters start from zero — needs 8 new failures
(without 4 successes first).

**Key property:** Failures do not undo successes. The counters are
independent and monotonic. With 2/7 reliable servers, 4 successes are
reached (~7 probes) well before 8 failures accumulate.

### Confidence Comparison

| | go v1 | go v2 | rust v1 | rust v2 | js v1/v2 |
|--|-------|-------|---------|---------|----------|
| **Model** | Sliding window (3) | Sliding window (5) | Clamped counter (±3) | Single probe | Monotonic counters + TTL |
| **Confirm** | Majority (2/3) | Net +2 | Counter > 0 | 1 success | 4 successes |
| **Unconfirm** | Majority failures | Net -2 | Counter < 0 | TTL expiry | 8 failures after TTL |
| **Failures undo successes?** | **Yes** | **Yes** (but bigger window) | **Yes** | N/A | **No** |
| **Protection after confirm** | None | Long re-probe interval | None | Address TTL | **Full TTL** |
| **Oscillation risk** | **High** | Low | Moderate | Low (no tracking) | **Very low** |

---

## 4. What Counts as Success vs. Failure

### v1

| Outcome | go v1 | rust v1 | js v1 |
|---------|-------|---------|-------|
| Dial-back succeeds | Success | Success | Success |
| Dial-back fails (NAT blocks) | Failure | Failure | Failure |
| Server timeout / no response | **Failure** | Not counted | Not counted |
| Server error (stream reset) | **Failure** | Not counted | Not counted |
| Server refuses | **Failure** | Not counted | Not counted |

**go-libp2p v1 is the most aggressive:** dial timeouts and server
errors count as Private evidence. This amplifies oscillation — a
server that is merely overloaded or restarting is interpreted as "I am
behind NAT."

**Code evidence (go-libp2p v1):**

The v1 probe sends the request and returns any error through the
`dialResponses` channel
([`autonat.go:385-398`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/host/autonat/autonat.go#L385-L398)):

```go
func (as *AmbientAutoNAT) probe(pi *peer.AddrInfo) {
    cli := NewAutoNATClient(as.host, as.config.addressFunc, as.metricsTracer)
    ctx, cancel := context.WithTimeout(as.ctx, as.config.requestTimeout)
    defer cancel()
    err := cli.DialBack(ctx, pi.ID)
    select {
    case as.dialResponses <- err:
    ...
```

`handleDialResponse` classifies the error. Only `E_DIAL_ERROR` is
recognized as a dial error; **all other errors (timeouts, stream
resets, refusals) fall through to `ReachabilityUnknown`**
([`autonat.go:299-310`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/host/autonat/autonat.go#L299-L310)):

```go
func (as *AmbientAutoNAT) handleDialResponse(dialErr error) {
    var observation network.Reachability
    switch {
    case dialErr == nil:
        observation = network.ReachabilityPublic
    case IsDialError(dialErr):
        observation = network.ReachabilityPrivate
    default:
        observation = network.ReachabilityUnknown
    }
    as.recordObservation(observation)
}
```

`IsDialError` only returns true for `Message_E_DIAL_ERROR`
([`client.go:110-112`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/host/autonat/client.go#L110-L112)):

```go
func (e Error) IsDialError() bool {
    return e.Status == pb.Message_E_DIAL_ERROR
}
```

`recordObservation` then decrements confidence on `ReachabilityUnknown`
if current status is non-Unknown. This means **server failures erode
confidence even though they don't directly flip to Private**
([`autonat.go:357-359`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/host/autonat/autonat.go#L357-L359)):

```go
} else if as.confidence > 0 {
    // don't just flip to unknown, reduce confidence first
    as.confidence--
}
```

Once confidence hits 0, the next Unknown observation flips the status.
With `maxConfidence=3`, it takes 4 consecutive Unknown (server failure)
observations to flip from Public to Unknown — which can happen quickly
with unreliable servers.

### v2

| Outcome | go v2 | rust v2 | js v2 |
|---------|-------|---------|-------|
| Dial-back succeeds (nonce verified) | Success → confidence++ | Success → `ExternalAddrConfirmed` | Success → counter++ |
| Dial-back fails (NAT blocks) | Failure → confidence-- | Failure → remains candidate | Failure → counter++ |
| `E_DIAL_REFUSED` (rate limited) | **Not recorded** | Not counted | Not counted |
| `E_DIAL_BACK_ERROR` (stream error after connection) | **Success** (connection proves reachability) | Error → not counted | Error → not counted |
| `E_INTERNAL_ERROR` | Not recorded | Not counted | Not counted |
| Server timeout | Triggers backoff (5s→5min) | Not counted | Not counted |

**Code evidence (go-libp2p v2):**

The v2 client maps server responses to reachability in
[`client.go:191-209`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/protocol/autonatv2/client.go#L191-L209):

```go
switch resp.DialStatus {
case pb.DialStatus_OK:
    // ... nonce verified
    rch = network.ReachabilityPublic
case pb.DialStatus_E_DIAL_BACK_ERROR:
    // connection established → address is reachable
    rch = network.ReachabilityPublic
case pb.DialStatus_E_DIAL_ERROR:
    rch = network.ReachabilityPrivate
default:
    return Result{}, fmt.Errorf("invalid response: ...")
}
```

When the server returns `E_DIAL_REFUSED`, the client returns a
`Result{AllAddrsRefused: true}` with no error — this is handled
specially as a non-result
([`client.go:141-143`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/protocol/autonatv2/client.go#L141-L143)):

```go
if resp.GetStatus() == pb.DialResponse_E_DIAL_REFUSED {
    return Result{AllAddrsRefused: true}, nil
}
```

When the client fails to even reach the server (timeout, stream reset,
connection error), `GetReachability` returns an error. The reachability
tracker's `CompleteProbe` **discards the result entirely**
([`addrs_reachability_tracker.go:576`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/host/basic/addrs_reachability_tracker.go#L576)):

```go
func (m *probeManager) CompleteProbe(reqs probe, res autonatv2.Result, err error) {
    ...
    // nothing to do if the request errored.
    if err != nil {
        return
    }
    ...
```

This is the fundamental difference: **v1 treats server failures as NAT
evidence (eroding confidence); v2 ignores them entirely.** Only actual
`E_DIAL_ERROR` (server tried and failed to dial back) counts as Private
in v2.

**go-libp2p v2's `E_DIAL_BACK_ERROR` → Success:** If the server
reaches the client (TCP/QUIC connection established) but the dial-back
stream fails (wrong nonce, stream reset), go-libp2p counts this as
success. The network-level connection proves the address is reachable;
the stream-level failure is treated as a server bug. This is a
deliberate design choice not in the spec.

**Refusals are not recorded** in go-libp2p v2. After 5 consecutive
refusals (`maxConsecutiveRefusals`), probing pauses for 10 minutes.
This prevents rate-limited servers from contaminating the confidence
window.

---

## 5. State Transition Timing

### Time to First Determination

| Implementation | Fastest | Typical | Limiting factor |
|----------------|---------|---------|-----------------|
| go v1 | ~3s | ~15min | `refreshInterval` (15min) |
| go v2 | ~6s | ~6s | 1s new-addr delay + 3 probes × ~2s |
| rust v1 | ~10s | ~10s | `retry_interval` (10s) |
| rust v2 | ~5s | ~5s | `probe_interval` (5s) |
| js v1 | ~60s | ~240s | 60s peer discovery × 4 successes needed |
| js v2 | ~60s | ~240s | Same scheduling as v1 |

### Time to Detect Loss of Reachability

| Implementation | Minimum | Typical | Limiting factor |
|----------------|---------|---------|-----------------|
| go v1 | ~30s (2 failures) | ~15min | Refresh interval |
| go v2 | ~20min (4 failures × 5min) | ~1h | `highConfidenceAddrProbeInterval` (1h) |
| rust v1 | ~40s (4 failures × 10s) | ~15min | Refresh interval |
| rust v2 | Address TTL | Address TTL | No active re-probe for confirmed addrs |
| js v1/v2 | TTL + ~480s | TTL + ~480s | TTL expiry + 8 failures |

**go-libp2p v2 is slowest to detect loss of reachability** by design.
The 1-hour re-probe for high-confidence addresses means a node that
becomes unreachable may take up to 1 hour to notice. This is the
stability/responsiveness tradeoff: the same mechanism that prevents
oscillation also delays detection of genuine changes.

### Re-probe Intervals (go-libp2p v2)

| State | Confidence | Interval |
|-------|------------|----------|
| Unknown | < target | 5 min (refresh cycle) |
| Reachable (high confidence) | ≥ 3 | 1 hour (primary), 3 hours (secondary) |
| Unreachable (high confidence) | ≤ -3 | 1 hour (primary), 3 hours (secondary) |
| Stale | > 5h since last probe | Immediate on next refresh |
| New address | N/A | 1 second |
| Error/backoff | N/A | Exponential: 5s → 5min |

---

## 6. Protocol Interactions

### How AutoNAT Results Propagate

```
                    go-libp2p                rust-libp2p              js-libp2p
                    ─────────                ───────────              ─────────
AutoNAT v1  ──►  EvtLocalReachability     NatStatus event          addressManager.
                  Changed                  (user handles)           confirmObservedAddr()
                     │                                                    │
                     ▼                                                    ▼
                  DHT (server/client)                               peerStore.patch()
                  AutoRelay (start/stop)                                  │
                  Address Manager                                         ▼
                  NAT Service                                      'self:peer:update'
                                                                         │
                                                                         ▼
                                                                    DHT (server/client)

AutoNAT v2  ──►  EvtHostReachableAddrs    ExternalAddr             addressManager.
                  Changed                  Confirmed/Expired        confirmObservedAddr()
                     │                          │                        │
                     ▼                          ▼                        ▼
                  Address Manager          Kademlia                 (same path as v1)
                  (ONLY — DHT, Relay       Identify
                   do NOT consume v2)      Rendezvous
```

### DHT Mode Switching

| Implementation | Trigger | v1 path | v2 path |
|----------------|---------|---------|---------|
| go-libp2p | `EvtLocalReachabilityChanged` | Direct subscription | **Not wired** — DHT ignores v2 |
| rust-libp2p | `FromSwarm::ExternalAddrConfirmed` | v1 doesn't emit this | Direct — v2 confirms → DHT switches |
| js-libp2p | `self:peer:update` | Indirect via address manager | Same path (both call `confirmObservedAddr`) |

**go-libp2p is the only implementation where v1 and v2 have completely
separate signal paths.** In rust and js, both versions feed through the
same mechanism (external address list / peer store), so there is no
v1/v2 gap.

### AutoRelay Activation

| Implementation | Trigger | Affected by v1/v2 gap? |
|----------------|---------|------------------------|
| go-libp2p | `EvtLocalReachabilityChanged` → Private | **Yes** — only v1 triggers relay |
| rust-libp2p | N/A (no automatic relay client) | No |
| js-libp2p | Independent of autonat | No |

### Address Advertisement (Identify)

| Implementation | Depends on v1? | Depends on v2? |
|----------------|----------------|----------------|
| go-libp2p | **Yes** — Private → replace public addrs with relay addrs | No |
| rust-libp2p | No | **Yes** — confirmed addrs in swarm external list |
| js-libp2p | Indirectly (v1 confirms → addr manager) | Same path |

---

## 7. Oscillation Analysis

**Scenario:** 2 reliable + 5 unreliable servers. Node is genuinely
reachable but unreliable servers fail dial-backs.

| Implementation | Oscillation risk | Why |
|----------------|-----------------|-----|
| go v1 | **High** (60% of testbed runs) | Window of 3, random peer selection. P(2 consecutive unreliable) = (5/7)² = 51% |
| go v2 | **None** (0% in testbed) | Window of 5, needs 4 consecutive failures to flip from +3 to -2. P(4 consecutive) = (5/7)⁴ = 33% but intervening successes reset |
| rust v1 | **Moderate** | Counter needs 4 failures (+3→-1). Same probability but no sliding window eviction |
| rust v2 | **None** | Single probe, no undo mechanism. TTL protects confirmed addresses |
| js v1/v2 | **Very low** | Monotonic counters: 4 successes reached (~7 probes avg) before 8 failures. TTL protects after confirmation |

---

## 8. Design Philosophy Summary

| Dimension | go-libp2p | rust-libp2p | js-libp2p |
|-----------|-----------|-------------|-----------|
| **v1 stability** | Low (sliding window, errors = evidence) | Moderate (clamped counter, errors ignored) | High (monotonic counters + TTL) |
| **v2 confidence** | Rich (sliding window, primary/secondary, backoff) | Minimal (single probe, no accumulation) | Moderate (same v1 thresholds applied to v2) |
| **Server selection** | Random from all supporting peers | Random from explicit + connected | Topology-driven with segment diversity |
| **Failure philosophy** | Aggressive — errors = Private evidence | Conservative — errors ignored | Conservative — errors ignored |
| **Signal architecture** | Dual path (v1 global + v2 per-addr, incompatible) | Unified (v2 feeds swarm external addrs) | Unified (both feed address manager) |
| **Change detection** | v1: fast but oscillates. v2: slow (1h re-probe) | v2: fast (5s) but no re-verification | Slow (TTL + 60s probe interval) |

---

---

## 9. The Wiring Gap: v1 Controls Everything in go-libp2p

*(Merged from v1-v2-reachability-gap.md)*

AutoNAT v1 and v2 coexist in go-libp2p but produce **independent,
incompatible reachability signals**. v1 emits a global
Public/Private/Unknown flag. v2 emits per-address reachability. There
is no bridge — v2 results do not feed into the v1 global flag.

This matters because every subsystem that reacts to reachability
consumes the **v1 global flag only**:

| Consumer | Event consumed | v2 aware? |
|----------|---------------|-----------|
| AutoRelay | `EvtLocalReachabilityChanged` (v1) | No |
| Kademlia DHT | `EvtLocalReachabilityChanged` (v1) | No |
| Address Manager | `EvtLocalReachabilityChanged` (v1) | No |
| NAT Service | `EvtLocalReachabilityChanged` (v1) | No |

**rust-libp2p** is partially mitigated: its Kademlia reacts to the
external address list (`ExternalAddrConfirmed`), not a global flag.
v2 results DO flow into Kademlia. No built-in AutoRelay exists.

**js-libp2p** never had a global flag. Both v1 and v2 feed into the
same `confirmObservedAddr()` → `self:peer:update` → DHT pipeline.
This is the architecture go-libp2p should converge toward.

| Feature | go-libp2p | rust-libp2p | js-libp2p |
|---------|-----------|-------------|-----------|
| v2 → DHT mode | **No** (reads v1 only) | **Yes** (reads address list) | **Yes** (reads address list) |
| v2 → Relay | **No** (reads v1 only) | N/A (manual relay) | N/A (relay independent) |
| v2 → Address advertisement | **No** (reads v1 only) | **Yes** (Identify reads external addrs) | **Yes** (returns verified addrs) |
| v1/v2 can diverge? | **Yes** | Partially | **No** — single pipeline |

### Impact on External Projects

- **Obol/Charon** (go-libp2p v0.47.0): Exports `p2p_reachability_status`
  from v1. Reports NAT-related connectivity issues (low hole-punching
  success, relay churn) — consistent with v1 oscillation but not
  directly attributed to AutoNAT. See [obol.md](obol.md).
- **Avail** (rust-libp2p v0.55.0): Disabled AutoNAT entirely in v1.13.2
  after "autonat-over-quic" errors (rust-libp2p#3900, since fixed by
  PR #4568). See [avail.md](avail.md).

### Recommended Fix Options

**Option A: Reduction Function (minimal change)**

Add logic to emit `EvtLocalReachabilityChanged` based on v2 results:
```
if any v2-confirmed address is reachable → emit ReachabilityPublic
if all probed addresses are unreachable  → emit ReachabilityPrivate
if still probing                         → emit ReachabilityUnknown
```

All v1 consumers benefit without changing their code.

**Option B: Per-Address Consumers (js-libp2p model)**

Refactor consumers to check the confirmed address list:
- AutoRelay: "start relaying if no address is confirmed reachable"
- Kademlia: "server mode if any address is confirmed reachable"
- Address Manager: "replace only unreachable addresses with relay equivalents"

Most correct. Larger refactor.

**Option C: Hybrid (pragmatic)**

Implement Option A as an immediate fix, then migrate consumers to
Option B over time.

---

## References

- [go-libp2p implementation](go-libp2p-autonat-implementation.md)
- [rust-libp2p implementation](rust-libp2p-autonat-implementation.md)
- [js-libp2p implementation](js-libp2p-autonat-implementation.md)
- [Measurement results](measurement-results.md) — includes v1/v2 performance analysis
- [AutoNAT v2 protocol walkthrough](autonat-v2.md)
- [AutoNAT v2 spec](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
