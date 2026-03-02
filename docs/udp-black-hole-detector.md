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
MinSuccesses=5):

| State | Condition | Behavior |
|-------|-----------|----------|
| **Probing** | < N results collected | Allows 1-in-N dials as probes to gather data |
| **Allowed** | ≥ MinSuccesses in last N | All dials permitted |
| **Blocked** | < MinSuccesses in last N | All dials refused |

When a dial is refused, `filterKnownUndialables()` returns:
```
dial refused because of black hole
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

This design assumes the main host has a healthy counter state — that it has
seen enough successful UDP connections to be in `Allowed` state. This
assumption holds for **long-running production nodes** (Kubo) that
accumulate many successful QUIC connections over hours and days.

It does **not** hold for **freshly started servers** — testbed containers,
CI environments, newly deployed infrastructure:

1. Server starts. Main host counter has zero history → `Probing` state.
2. The only UDP traffic the server handles is AutoNAT dial-back requests.
   But `WithReadOnlyBlackHoleDetector()` means these results are not
   recorded in the counter.
3. Meanwhile, the main host may attempt a few outbound UDP connections
   (DHT, relay) that fail or don't happen at all in an isolated testbed.
4. Counter transitions to `Blocked` (< 5 successes in 100 attempts).
5. `dialerHost` reads `Blocked` state from the shared counter.
6. Client sends a QUIC dial-back request → `filterKnownUndialables()`
   returns `"dial refused because of black hole"` → server responds
   `E_DIAL_REFUSED`.
7. Every QUIC dial-back is refused. Client's QUIC addresses stay
   `unknown` indefinitely.

### Why This Is a Limitation, Not a Bug

The detector works correctly for its intended purpose: it accurately
reflects that the server hasn't seen successful UDP traffic. The problem is
that "no UDP traffic on a controlled Docker network" means something
different than "UDP is blocked by the network." The detector can't
distinguish between these two cases.

This is unlikely to cause issues on live networks where nodes are
long-running and have diverse connection patterns. It specifically affects:

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

1. The main host has **no** black hole detector — it will attempt all UDP
   dials regardless of history.
2. When `makeAutoNATV2Host()` copies `cfg.UDPBlackHoleSuccessCounter`
   (now `nil`), the `dialerHost` also gets `nil`.
3. The `dialerHost`'s swarm creates a fresh independent counter in
   `Probing` state. Combined with `WithReadOnlyBlackHoleDetector()`, the
   read-only probing state allows dials through (only `Blocked` state
   refuses dials, not `Probing`).
4. QUIC dial-backs work immediately.

### Tradeoff

The main server host loses its own black hole detection. For testbed
servers whose only job is to serve AutoNAT requests on a controlled Docker
network, this is acceptable. On a production node this would be undesirable
— a proper fix should be applied at the go-libp2p level.

---

## Proper Upstream Fix

The `dialerHost` should not share the main host's black hole counter.
Unlike a regular node making speculative connections, the `dialerHost`
only dials addresses that a client has explicitly requested to test. If
UDP doesn't work for a particular dial-back, that's the information the
client needs — the detector shouldn't suppress it.

The fix in `config/config.go:makeAutoNATV2Host()`:

```go
autoNatCfg := Config{
    UDPBlackHoleSuccessCounter:        nil,
    CustomUDPBlackHoleSuccessCounter:  true,  // don't create default counter
    IPv6BlackHoleSuccessCounter:       nil,
    CustomIPv6BlackHoleSuccessCounter: true,
    SwarmOpts: []swarm.Option{
        swarm.WithReadOnlyBlackHoleDetector(),
    },
}
```

Setting the counter to `nil` with `Custom=true` tells go-libp2p not to
create a default counter. The `dialerHost` will attempt all dials
regardless of UDP success history.

This approach aligns with how
[PR #2529](https://github.com/libp2p/go-libp2p/pull/2529) solved the same
problem for AutoNAT v1.

### Alternative: Start in Allowed State

Starting the counter in `Allowed` state (instead of disabling it) would
also fix the initial problem but introduces a subtlety: the counter could
later transition to `Blocked` if enough dial-backs fail (e.g., the
`dialerHost` handles many requests for nodes behind symmetric NAT where
QUIC dial-backs always fail). This would re-introduce the same problem
over time. Disabling the counter entirely is safer and semantically
correct — the `dialerHost` doesn't need network quality heuristics.

---

## References

- [Findings Report — Issue 2](report.md#issue-2-quic-dial-back-failure-on-fresh-servers) — discovery narrative, evidence, investigation trail
- [go-libp2p Implementation](go-libp2p-autonat-implementation.md#black-hole-detector) — implementation context
- [PR #2320: swarm: implement blackhole detection](https://github.com/libp2p/go-libp2p/pull/2320) — introduced the detector
- [PR #2529: host: disable black hole detection on autonat dialer](https://github.com/libp2p/go-libp2p/pull/2529) — v1 fix (disabled detector on dialer)
- [PR #2561: swarm: use shared black hole filters for autonat](https://github.com/libp2p/go-libp2p/pull/2561) — v2 read-only approach (folded into #2469)
- [PR #2469: autonatv2: implement autonatv2 spec](https://github.com/libp2p/go-libp2p/pull/2469) — AutoNAT v2 implementation (includes shared counter from #2561)
- Source: `p2p/net/swarm/black_hole_detector.go` (go-libp2p)
