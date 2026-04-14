# UDP Black Hole Detector and AutoNAT v2

How go-libp2p's UDP black hole detector interacts with AutoNAT v2 dial-backs,
why it causes QUIC failures on fresh servers, and how the testbed works
around it.

---

## What the Black Hole Detector Does

The black hole detector is a go-libp2p performance optimization (not part of
the libp2p specification). It protects nodes from wasting resources attempting
UDP/QUIC connections on networks that silently drop UDP traffic — corporate
firewalls, restrictive airport WiFi, etc.

The detector lives in the swarm layer
(`p2p/net/swarm/black_hole_detector.go`) and tracks connection success rates
using a `BlackHoleSuccessCounter`. It was introduced in
[go-libp2p PR #2320](https://github.com/libp2p/go-libp2p/pull/2320)
(Jun 2023) and has no corresponding specification in
[libp2p/specs](https://github.com/libp2p/specs).

### Counter States

The counter uses a sliding window of the last N dial attempts (default N=100,
MinSuccesses=5). The initial state is `Probing` (zero value of the state
enum):

| State | Condition | Filter behavior (non-readOnly) |
|-------|-----------|---------------------------------|
| **Probing** | Initial state; <N results collected | **All UDP dials pass through**; outcomes are recorded to determine next state |
| **Allowed** | ≥ MinSuccesses in last N | All dials permitted |
| **Blocked** | < MinSuccesses in last N | UDP dials refused; every N-th request allowed as a probe so the state can recover |

When a dial is refused, `filterKnownUndialables()` returns:
```
dial refused because of black hole
```

**Read-only mode (used by AutoNAT v2 `dialerHost`) behaves differently:**
any state other than `Allowed` is treated as `Blocked`. In particular,
`Probing` is converted to `Blocked`, so UDP dials are filtered out
whenever the shared counter has not yet reached `Allowed`. See
`getFilterState` in `black_hole_detector.go`:

```go
if d.readOnly {
    if f.State() != blackHoleStateAllowed {
        return blackHoleStateBlocked  // Probing becomes Blocked
    }
    return blackHoleStateAllowed
}
```

### Why It Exists

On a network that blocks UDP entirely, every QUIC dial attempt is wasted:
the SYN-equivalent packet is silently dropped, the node waits for a timeout,
and the connection fails. Multiply this by hundreds of peers in the DHT
routing table and the node spends significant CPU, memory, and time on
connections that can never succeed. The detector learns this pattern quickly
and stops trying, falling back to TCP-only operation.

---

## How AutoNAT Uses the Detector

Both AutoNAT v1 and v2 servers use a separate internal host for dial-back
connections (separate peer ID, separate connections). The question is how
this internal host interacts with the black hole detector.

### PR History

Three PRs form the evolution of this interaction:

**[PR #2320](https://github.com/libp2p/go-libp2p/pull/2320)** (Jun 2023,
by sukunrt) — **Introduced the black hole detector.** Added
`BlackHoleSuccessCounter` to the swarm layer with the three-state model
(Probing → Allowed/Blocked). Integrated with `filterKnownUndialables()` so
that refused dials never leave the swarm. No special handling for AutoNAT
dialers — they inherited the main host's counter by default.

**[PR #2529](https://github.com/libp2p/go-libp2p/pull/2529)** (Aug 2023,
by sukunrt) — **Fixed the issue for AutoNAT v1.** When v1 servers received
many requests from private (NATed) nodes, the dial-back failures accumulated
in the shared counter and pushed it into `Blocked` state, causing the server
to refuse all subsequent QUIC dial-backs. The fix: **disable the detector
entirely** on the v1 dialer by passing nil counters via swarm options:

```go
// config/config.go:addAutoNAT() — v1 dialer config
autoNatCfg := Config{
    // ...
    SwarmOpts: []swarm.Option{
        swarm.WithUDPBlackHoleSuccessCounter(nil),   // disabled
        swarm.WithIPv6BlackHoleSuccessCounter(nil),  // disabled
    },
}
```

**[PR #2561](https://github.com/libp2p/go-libp2p/pull/2561)** (closed Jun
2024, by sukunrt) — **Attempted a different approach for AutoNAT v2.**
Instead of disabling the detector, this PR introduced a `ReadOnly` mode
where the v2 `dialerHost` shares the main host's counter but doesn't update
it. The idea was that AutoNAT v2 dial-backs should respect the main host's
network assessment without corrupting its statistics. This PR was not merged
independently — it was folded into the main AutoNAT v2 implementation
([PR #2469](https://github.com/libp2p/go-libp2p/pull/2469), merged Jun
2024).

### Why v1 Is Fixed but v2 Is Not

The v1 and v2 fixes took opposite approaches:

```go
// V1 dialer (config/config.go:addAutoNAT, line 711-714):
SwarmOpts: []swarm.Option{
    swarm.WithUDPBlackHoleSuccessCounter(nil),   // no counter at all
    swarm.WithIPv6BlackHoleSuccessCounter(nil),   // no counter at all
},

// V2 dialerHost (config/config.go:makeAutoNATV2Host, line 240-246):
UDPBlackHoleSuccessCounter:  cfg.UDPBlackHoleSuccessCounter,  // shared!
IPv6BlackHoleSuccessCounter: cfg.IPv6BlackHoleSuccessCounter,  // shared!
SwarmOpts: []swarm.Option{
    swarm.WithReadOnlyBlackHoleDetector(),  // reads state, doesn't update
},
```

| | AutoNAT v1 dialer | AutoNAT v2 `dialerHost` |
|--|---|---|
| **Counter** | `nil` (no detector) | Shared from main host |
| **Mode** | N/A | Read-only |
| **Effect** | All dials always allowed | Dials gated by main host's state |
| **Fresh server** | Works | Fails (main host → `Blocked` → QUIC refused) |

The v1 approach is simple and correct: the dialer doesn't need its own
network quality heuristic because it only dials addresses that clients
explicitly request to test.

The v2 approach tried to be smarter — "respect the main host's assessment"
— but this creates a dependency on the main host having accumulated enough
UDP traffic to be in `Allowed` state. On production Kubo nodes with diverse
long-running connections, this works. On fresh servers in isolated
environments, it doesn't.

---

## The Problem: Fresh Servers

The root cause is not that the main host's counter reaches `Blocked` —
it's that the v2 `dialerHost` uses the counter in **read-only mode**,
which converts the initial `Probing` state into `Blocked` for filter
purposes. As long as the main host's counter has not yet reached
`Allowed`, every QUIC dial-back is refused.

This design assumes the main host reliably reaches `Allowed` state
by accumulating successful outbound UDP dials. That holds for
**long-running production nodes** (Kubo) with diverse QUIC traffic
over hours and days.

It does **not** hold for **freshly started servers** — testbed
containers, CI environments, newly deployed infrastructure:

1. Server starts. Main host counter has zero history → `Probing`
   state.
2. The v2 `dialerHost` inherits the counter in read-only mode.
   Read-only `getFilterState()` returns `Blocked` whenever the
   state is anything other than `Allowed`:

   ```go
   if d.readOnly {
       if f.State() != blackHoleStateAllowed {
           return blackHoleStateBlocked
       }
       return blackHoleStateAllowed
   }
   ```
3. On an isolated testbed network the main host has few or no
   outbound UDP dials (no bootstrap nodes, no DHT walks). The
   counter does not collect the N=100 results it needs to
   transition to `Allowed` and stays in `Probing` indefinitely.
4. Every AutoNAT v2 QUIC dial-back request reaches the
   `dialerHost`. `FilterAddrs()` asks the read-only detector for
   UDP state → returns `Blocked` because the main host state is
   still `Probing` → UDP addresses filtered out.
5. The dialerHost has nothing to dial, responds `E_DIAL_REFUSED`
   to the client.
6. Every QUIC dial-back is refused. The client's QUIC addresses
   stay unreachable indefinitely.

This is distinct from the scenario where the main host's counter
has actively entered `Blocked` after 100 failed dials — on isolated
testbeds the counter never gets that far. It is stuck in `Probing`,
and read-only mode makes that indistinguishable from `Blocked` for
the dialerHost.

### Why read-only mode exists (and what it got wrong)

Read-only mode has two properties:
- **Does not record outcomes into the shared counter** — prevents
  AutoNAT-specific failure patterns (e.g., clients behind symmetric
  NAT) from polluting the main host's view of UDP viability. This
  was the same problem that motivated the v1 fix in PR #2529. This
  property is correct.
- **Treats non-`Allowed` as `Blocked`** — the code comment
  describes this as "refusing requests when black hole state is
  unknown." This is the root cause of the bug. For AutoNAT v2,
  "unknown" should *not* mean "refuse"; every dial-back carries
  information the client needs regardless of the main host's
  current UDP assessment.

### When this manifests

The detector itself reflects the main host's UDP history accurately.
The practical problem is that read-only mode reinterprets "we haven't
learned yet" (`Probing`) as "UDP is broken" (`Blocked`) — conflating
*unknown* with *bad*. That conflation is harmless on nodes where the
main host reliably reaches `Allowed` shortly after startup, and
harmful on nodes where it does not.

This specifically affects:

- **Testbed environments** with isolated Docker networks
- **CI/CD pipelines** with short-lived server instances
- **Freshly deployed infrastructure** before servers accumulate traffic

---

## Testbed Workaround

The testbed can't patch `makeAutoNATV2Host()` from the outside — it's
internal to go-libp2p. But it can control what counter the **main host**
starts with, which the `dialerHost` then inherits.

In `testbed/main.go`, server nodes are configured with:

```go
libp2p.UDPBlackHoleSuccessCounter(nil),
libp2p.IPv6BlackHoleSuccessCounter(nil),
```

Setting the main host's counter to `nil` means:

1. The main host has **no** black hole detector — it will attempt all
   UDP dials regardless of history.
2. `makeAutoNATV2Host()` copies `cfg.UDPBlackHoleSuccessCounter` (now
   `nil`) into the dialerHost's config, so the dialerHost also has a
   nil counter.
3. In `FilterAddrs()`, the filter is gated on
   `if d.udp != nil && hasUDP` — a nil counter bypasses filtering
   entirely, regardless of read-only mode. UDP addresses are never
   filtered out of dial attempts.
4. QUIC dial-backs work immediately.

### Tradeoff

The main server host loses its own black hole detection. For testbed
servers whose only job is to serve AutoNAT requests on a controlled Docker
network, this is acceptable. On a production node this would be undesirable
— a proper fix should be applied at the go-libp2p level.

---

## Impact on AutoNAT Results

When the black hole detector blocks QUIC dial-back, the server
responds to the client with `E_DIAL_REFUSED` for the QUIC address.
From the client's perspective:

| Scenario | Server behavior | Effect on client state |
|----------|----------------|------------------------|
| Detector allows QUIC | Server dials back, succeeds | Address confirmed Reachable |
| Detector allows QUIC | Server dials back, NAT blocks (`E_DIAL_ERROR`) | Counts as failure; enough of these → Unreachable |
| **Detector blocks QUIC** | **Server refuses to dial (`E_DIAL_REFUSED`)** | **Refusal discarded from confidence; probing paused after 5 consecutive refusals; address stays `Unknown`** |

The v2 reachability tracker handles `E_DIAL_REFUSED` via
`AddRefusal()`, which only updates a `consecutiveRefusals` counter.
It does **not** add an outcome to the confidence window and does
**not** flip state to Unreachable:

```go
// p2p/host/basic/addrs_reachability_tracker.go
if res.AllAddrsRefused {
    if s, ok := m.statuses[firstAddrKey]; ok {
        s.AddRefusal(now)
    }
    return   // returns without calling AddOutcome
}
```

After `maxConsecutiveRefusals` (=5) consecutive refusals, probing
for that address is paused for `recentProbeInterval` (=10 min); on
resume, if every reachable server still refuses, the cycle repeats.

The resulting failure mode is **"address stuck in `Unknown`"**, not
**"address reported Unreachable"**. This is an important distinction:

- `E_DIAL_REFUSED` does not erode confidence
- State does not flip to Private/Unreachable
- Downstream consumers that require a positive reachability signal
  simply never receive one

### Why This Matters

1. **It's server-side, not client-side** — the client can't
   distinguish "server refused because of its own detector" from any
   other reason the server might refuse. Both look like
   `E_DIAL_REFUSED`.

2. **It's transient for healthy nodes** — once the server's main
   host accumulates enough successful outbound UDP, the counter
   enters `Allowed` state and the read-only filter on the dialerHost
   stops returning `Blocked`. On Kubo nodes with diverse traffic this
   typically happens within minutes of startup.

3. **It's testbed-specific in severity** — on isolated Docker
   servers with no background UDP traffic the main host never leaves
   `Probing`, so the dialerHost's read-only filter never stops
   returning `Blocked`. The client can't find any v2 server that
   will confirm the QUIC address.

4. **TCP is unaffected** — the detector only gates UDP/QUIC. TCP
   dial-backs always proceed regardless of the detector state.

---

## Why the Detector Is Necessary

Despite causing problems for AutoNAT, the UDP black hole detector
serves an important purpose for regular libp2p operation:

### The Problem It Solves

On networks that silently drop UDP traffic (common in corporate
environments, some mobile carriers, restrictive WiFi portals):

- Every QUIC connection attempt is wasted — the SYN-equivalent packet
  is silently dropped, the node waits for a timeout (typically 5-30s)
- A DHT routing table with hundreds of peers means hundreds of wasted
  QUIC attempts
- Each failed attempt consumes CPU (crypto handshake prep), memory
  (connection state), and time (timeout wait)
- The node is effectively throttled by UDP timeouts even though TCP
  would work fine

The detector learns this pattern within ~100 dial attempts and switches
to TCP-only operation, dramatically improving performance on
UDP-hostile networks.

### Why Simply Disabling It Isn't the Answer

Disabling the detector network-wide would hurt nodes on UDP-hostile
networks. The fix must be scoped to the AutoNAT dial-back path
specifically, not the entire swarm.

---

## Proposed Upstream Fixes

### Option A: Disable detector on dialerHost (recommended)

The `dialerHost` should not have a black hole detector at all. Unlike
a regular node making speculative connections, the `dialerHost` only
dials addresses that a client has explicitly requested to test. If UDP
doesn't work for a particular dial-back, that failure IS the information
the client needs — the detector should not suppress it.

```go
// config/config.go:makeAutoNATV2Host()
autoNatCfg := Config{
    UDPBlackHoleSuccessCounter:        nil,
    CustomUDPBlackHoleSuccessCounter:  true,  // don't create default counter
    IPv6BlackHoleSuccessCounter:       nil,
    CustomIPv6BlackHoleSuccessCounter: true,
    // No SwarmOpts — no read-only detector either
}
```

This is equivalent to what
[PR #2529](https://github.com/libp2p/go-libp2p/pull/2529) did for the
AutoNAT v1 dialer, which has been running in production since August 2023
without issues.

**Pros:**
- Simplest fix, proven approach (same as v1)
- AutoNAT dial-backs always attempted — result reflects actual network state
- No counter state to manage or leak

**Cons:**
- If the server's network genuinely blocks UDP, every QUIC dial-back
  attempt wastes a timeout. But this is bounded: the server only handles
  AutoNAT requests at rate-limited intervals (60 RPM), not hundreds
  of DHT dials.

### Option B: Skip detector for AutoNAT dial-backs specifically

Add a dial option that bypasses the black hole detector for specific
dials, rather than disabling it on the entire `dialerHost`:

```go
// In server.go dialBack():
h.Connect(ctx, pi,
    swarm.WithSkipBlackHoleDetection(),  // new option
)
```

This keeps the `dialerHost`'s detector intact for any other potential
use while exempting AutoNAT dial-backs.

**Pros:**
- Narrowest scope — only AutoNAT dials skip the detector
- If `dialerHost` is ever used for other purposes, they still get protection

**Cons:**
- Requires adding a new swarm dial option
- In practice, `dialerHost` is only used for AutoNAT — same effect as Option A

### Option C: Seed counter from main host's success history

When creating the `dialerHost`, initialize its counter with the main
host's current success count instead of starting from zero:

```go
mainCount := cfg.UDPBlackHoleSuccessCounter.SuccessCount()
dialerCounter := swarm.NewBlackHoleSuccessCounter(N, MinSuccesses)
dialerCounter.SeedWith(mainCount)  // new method
```

**Pros:**
- Fresh servers with healthy main hosts work immediately
- Preserves detector behavior for long-running servers

**Cons:**
- Doesn't help fresh servers where the main host also has zero history
  (the exact testbed scenario)
- Counter could still transition to `Blocked` over time if many
  dial-backs fail (servers handling many symmetric NAT clients)
- More complex than Option A

### Option D: Start counter in Allowed state

Override the initial state to `Allowed` instead of `Probing`:

```go
dialerCounter := swarm.NewBlackHoleSuccessCounter(N, MinSuccesses)
dialerCounter.ForceState(Allowed)  // new method
```

**Pros:**
- Works immediately on fresh servers

**Cons:**
- Counter may transition to `Blocked` over time (same as Option C)
- Semantically incorrect — the counter claims "UDP works" without evidence
- Re-introduces the original problem after enough failed dial-backs

### Option E: Use main host's counter in read-write mode

Current code uses read-only mode. Switching to read-write would let
successful dial-backs feed back into the counter:

```go
// Remove WithReadOnlyBlackHoleDetector()
SwarmOpts: []swarm.Option{
    // counter is read-write — successful dial-backs count
},
```

**Pros:**
- Self-healing — successful QUIC dial-backs push counter toward `Allowed`

**Cons:**
- Failed dial-backs also count, potentially corrupting the main host's
  counter (the original problem that PR #2529 fixed for v1)
- Nodes behind symmetric NAT generate many failures, pushing the
  counter toward `Blocked`

### Recommendation

**Option A** is the recommended fix. It matches the proven v1 approach,
is the simplest to implement, and has the clearest semantics: the
AutoNAT dial-back host should attempt every requested dial regardless of
UDP history, because the dial result itself is the output the client needs.

Option B achieves the same practical effect with a narrower scope but
requires new API surface. It's worth considering if `dialerHost` gains
other responsibilities in the future, but today it's unnecessary
complexity.

Options C, D, and E all attempt to preserve the detector on the dial-back
path, which is fundamentally the wrong approach — the detector's purpose
(protect against wasted speculative dials) doesn't apply to AutoNAT
(one-shot requested dials where the failure itself is useful information).

---

## Cross-Implementation Comparison

| Implementation | UDP black hole detector | AutoNAT dial-back impact |
|----------------|----------------------|-------------------------|
| **go-libp2p** | Yes (swarm layer) | v2 dialerHost shares counter → QUIC blocked on fresh servers |
| **rust-libp2p** | No | No issue — but also no protection against UDP-hostile networks |
| **js-libp2p** | No | No issue — but also no protection |

Only go-libp2p has this detector. The issue is specific to go-libp2p's
design choice of sharing the counter between the main host and the
AutoNAT dial-back host.

---

## References

- [Findings Report — Issue 2](report.md#issue-2-quic-dial-back-failure-on-fresh-servers) — discovery narrative, evidence, investigation trail
- [go-libp2p Implementation](go-libp2p-autonat-implementation.md#black-hole-detector) — implementation context
- [PR #2320: swarm: implement blackhole detection](https://github.com/libp2p/go-libp2p/pull/2320) — introduced the detector
- [PR #2529: host: disable black hole detection on autonat dialer](https://github.com/libp2p/go-libp2p/pull/2529) — v1 fix (disabled detector on dialer)
- [PR #2561: swarm: use shared black hole filters for autonat](https://github.com/libp2p/go-libp2p/pull/2561) — v2 read-only approach (folded into #2469)
- [PR #2469: autonatv2: implement autonatv2 spec](https://github.com/libp2p/go-libp2p/pull/2469) — AutoNAT v2 implementation (includes shared counter from #2561)
- Source: `p2p/net/swarm/black_hole_detector.go` (go-libp2p)
