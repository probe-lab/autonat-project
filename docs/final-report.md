# Assessing libp2p's NAT Reachability Stack: AutoNAT v2 Cross-Implementation Study

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Background](#background)
3. [Findings](#findings)
4. [Cross-Implementation Comparison](#cross-implementation-comparison)
5. [Recommendations](#recommendations)
6. [Testbed](#testbed)
7. [Glossary](#glossary)

---

## Executive Summary

### Motivation

Peer-to-peer networks built on libp2p require nodes to determine whether
their addresses are reachable from the internet. Most residential and
mobile devices sit behind Network Address Translation (NAT) — a router
technique that maps private IP addresses to a shared public IP. While
NAT allows outbound connections, it blocks most inbound traffic. A node
that doesn't know it's behind NAT may advertise unreachable addresses,
participate as a DHT server when it can't serve queries, or fail to
reserve relay connections it needs.

libp2p's **AutoNAT** protocol solves this by having peers test whether a
node's addresses are actually dialable from outside. AutoNAT v1 uses a
simple majority vote; AutoNAT v2 (specified 2023, deployed 2024)
improves on this with per-address testing and nonce-based verification.

However, libp2p-based projects report **connectivity issues for
NATed nodes** that motivated this investigation:

- **Obol Network** ([Charon](https://github.com/ObolNetwork/charon),
  go-libp2p v0.47.0): Uses AutoNAT for reachability detection in
  distributed validator nodes behind home or corporate NAT. Operators
  reported NAT-related connectivity issues, although these were directly related
  to hole punching and relay behavior, not to AutoNAT v2 specifically.
  See [Obol/Charon GitHub issues](https://github.com/ObolNetwork/charon/issues/4233) for details.

- **Avail Network** ([avail-light](https://github.com/availproject/avail-light),
  rust-libp2p v0.55.0): Light clients reported persistent
  "autonat-over-quic libp2p errors" starting from v1.7.4, caused by
  QUIC connection reuse producing false positives
  ([rust-libp2p#3900](https://github.com/libp2p/rust-libp2p/issues/3900),
  since fixed by [PR #4568](https://github.com/libp2p/rust-libp2p/pull/4568)).
  However, Avail **disabled AutoNAT entirely** in v1.13.2 (September
  2025) before the upstream fix shipped, forcing operators to manually
  set `--external-address` for DHT server mode.

This project investigates AutoNAT v2 across go-libp2p, rust-libp2p, and
js-libp2p to evaluate whether it solves the reachability detection
problem. A companion [Nebula crawl analysis](nebula-autonat-analysis.md)
of the IPFS Amino DHT confirms that DHT-mode flipping exists in
production (2–12% per Kubo version), but most of it correlates with
disconnections and restarts. Only ~0.39% of stably-reachable peers
show flipping that cannot be explained by network events.

### Scope

This report evaluates AutoNAT v2's correctness, performance, and
integration across three libp2p implementations (go, rust, js). It
does NOT evaluate:

- Hole punching success rates (DCUtR) — see Trautwein et al. 2022/2025
- Relay performance (Circuit Relay v2)
- DHT performance itself (routing, lookup latency)
- AutoNAT v1 in isolation (only v1/v2 comparison)

### Findings

This study evaluates how libp2p's NAT reachability stack — AutoNAT
v1, AutoNAT v2, and the subsystems that consume their signals (DHT,
AutoRelay, address management) — behaves across go-libp2p,
rust-libp2p, and js-libp2p. In [controlled testbed
conditions](#testbed), AutoNAT v2 correctly determines reachability
for all standard NAT types (**0% FNR/FPR**, ~6s convergence). However,
we identified **5 findings** where the broader reachability stack
breaks — in how implementations wire the protocol results into
downstream decisions, in protocol-level limitations, and in
cross-implementation inconsistencies. See the
[cross-implementation comparison](cross-implementation-comparison.md)
for how each finding manifests per implementation.

The most impactful finding is that **global (v1) and per-address (v2)
reachability can disagree, and there is no canonical way to reconcile
them**. In go-libp2p — the most relevant implementation since Kubo
uses it to participate in the IPFS Amino DHT — DHT and AutoRelay
subscribe to v1's global flag and do not consume v2's per-address
signal. Both protocols select servers from
the same peer pool, but v1 counts all non-success results (including
timeouts from honest-but-unreliable servers) as evidence against
reachability, while v2 discards them entirely. This means **v1 can
oscillate due to server unreliability alone — no malicious peers
needed** (60% of testbed runs with 5/7 unreliable servers; 0% of v2
runs). The unstable global flag overrides v2's stable per-address
result and drives DHT/relay decisions. rust-libp2p and js-libp2p
are not affected today because their DHT already consumes v2-level
signals — but this is an implementation choice, not a spec
requirement. Neither documents what should happen if both protocols
run, and the spec doesn't define it either.

Cross-implementation analysis reveals that **only go-libp2p has v2
consumed by a production project** (Kubo). rust-libp2p's v2
implementation works correctly when properly configured but lacks a
safety net when TCP port reuse fails (F5). js-libp2p emits no
reachability events from v2. No rust or js project deploys v2 in
production (Substrate skips autonat entirely; Helia uses v1 only).

### Findings at a Glance

| # | Finding | Category | Severity |
|---|---------|----------|----------|
| 1 | [Inconsistent global vs per-address reachability (v1 vs v2)](#finding-1-inconsistent-global-vs-per-address-reachability-v1-vs-v2) | go-libp2p | High |
| 2 | [UDP black hole blocks QUIC dial-back](#finding-2-udp-black-hole-detector-blocks-quic-dial-back) | go-libp2p | Medium |
| 3 | [ADF false positive (100% FPR)](#finding-3-address-restricted-nat-false-positive) | Protocol | Low |
| 4 | [Symmetric NAT missing signal](#finding-4-symmetric-nat-missing-signal) | Cross-impl | Low |
| 5 | [Rust: TCP port reuse safety net](#finding-5-rust-libp2p-tcp-port-reuse-safety-net) | rust-libp2p | Low |

For the cross-implementation findings matrix, feature comparison, and
adoption status, see
[cross-implementation-comparison.md](cross-implementation-comparison.md).

---

## Background

### NAT Types

NAT behavior is defined by two independent properties: **mapping**
(how the router assigns external ports) and **filtering** (which
inbound packets are allowed through).

| NAT Type | Mapping | Filtering | Inbound from strangers | Prevalence |
|----------|---------|-----------|----------------------|------------|
| **No NAT** | — | — | Always works | Servers, cloud |
| **Full-cone** | EIM (Endpoint-Independent Mapping) | EIF (Endpoint-Independent Filtering) | Always works | Rare (intentional DMZ/forward) |
| **Address-restricted** | EIM | ADF (Address-Dependent Filtering) | Only from previously contacted IPs | Rare in modern routers |
| **Port-restricted** | EIM | APDF (Address- and Port-Dependent Filtering) | Only from exact previously contacted IP:port | Most common home router default |
| **Symmetric** | ADPM (Address- and Port-Dependent Mapping) | APDF | Never (different port per destination) | CGNAT, mobile carriers |

For the full mapping/filtering taxonomy
([RFC 4787](https://www.rfc-editor.org/rfc/rfc4787)), see
[autonat-v2.md](autonat-v2.md).

### Related Protocols in libp2p

AutoNAT does not operate in isolation. It is part of a protocol stack
where each component handles a different aspect of connectivity:

**Identify** (`/ipfs/id/1.0.0`) — When two peers connect, they exchange
metadata including the `ObservedAddr` — the address each peer sees the
other connecting from. This is how a node discovers its external address
(the NAT-mapped public IP:port). Identify is the **input** to AutoNAT:
the observed addresses become candidates for reachability testing.

**AutoNAT v1** (`/libp2p/autonat/1.0.0`) — The original reachability
protocol. A node asks a random connected peer to dial it back. The peer
reports success or failure. v1 produces a **global** verdict
(Public/Private/Unknown) based on a majority vote across recent probes.

**AutoNAT v2** (`/libp2p/autonat/2/dial-request`,
`/libp2p/autonat/2/dial-back`) — The improved protocol tested in this
report. Tests **individual addresses** with nonce-based verification and
amplification protection. Produces per-address reachability.

**Circuit Relay v2** (`/libp2p/circuit/relay/0.2.0/hop`,
`/libp2p/circuit/relay/0.2.0/stop`) — When a node is determined to be
behind NAT, it reserves a relay slot on a public node. Other peers
connect through the relay as a fallback.

**DCUtR** (`/libp2p/dcutr`) — Direct Connection Upgrade through Relay.
After connecting via relay, peers attempt hole punching to establish a
direct connection, eliminating the relay overhead.

**Kademlia DHT** — Uses the reachability signal to decide server vs
client mode. Server-mode nodes accept and serve DHT queries; client-mode
nodes only issue queries. The DHT subscribes to AutoNAT v1's global
flag (not v2's per-address signal).

The dependency chain:

```
Identify (discover external address)
  → ObservedAddrManager (consolidate observations, activation threshold)
    → AutoNAT v2 (test address reachability)
      → EvtHostReachableAddrsChanged (per-address result)
    → AutoNAT v1 (test global reachability)
      → EvtLocalReachabilityChanged (global result)
        → DHT mode (server/client)
        → AutoRelay (reserve relay if private)
          → DCUtR (hole punch if relayed)
```

### NAT Behavior During AutoNAT v2 Dial-Back

AutoNAT v2 tests reachability by having a server dial back to the
client's external address. Whether the dial-back succeeds depends on
the NAT's filtering rules, not on AutoNAT:

| NAT type | Filtering rule | Dial-back | AutoNAT conclusion |
|---|---|---|---|
| **Full-cone** (EIF) | Any source allowed | PASS | Reachable |
| **Address-restricted** (ADF) | Only from contacted IPs | PASS (server IP was contacted) | Reachable |
| **Port-restricted** (APDF) | Only from contacted IP:port | BLOCK (server uses random port) | Unreachable |
| **Symmetric** (ADPM) | Different external port per destination | N/A (address never activated) | No signal |

AutoNAT correctly interprets the full-cone and port-restricted cases.
The address-restricted PASS is a protocol limitation (Finding #3) —
however, ADF is not known to be used by modern consumer routers, so
this case is unlikely to occur in practice. The symmetric case
(Finding #4) produces no signal because the activation threshold is
never reached.

For the full protocol walkthrough, see [autonat-v2.md](autonat-v2.md).

### AutoNAT v1 vs v2

| Aspect | v1 | v2 |
|--------|----|----|
| **Protocol** | `/libp2p/autonat/1.0.0` | `/libp2p/autonat/2/dial-request` + `dial-back` |
| **Scope** | Global (whole-node: Public/Private) | Per-address (each address independently) |
| **Probing** | Random peer, majority vote | Specific server selection, per-address confidence |
| **Confidence** | Sliding window of 3 (maxConfidence=3) | Per-address window of 5 probes, confirmed at 3 net successes |
| **Nonce verification** | No | Yes (prevents spoofing) |
| **Amplification protection** | No | Yes (30-100KB data when IP differs) |
| **Dial-back identity** | Same peer ID | Separate peer ID (go-libp2p) |
| **Event (go-libp2p)** | `EvtLocalReachabilityChanged` | `EvtHostReachableAddrsChanged` |
| **DHT consumes** | **Yes** | **No** (Finding #1) |
| **Spec** | Informal, no RFC | [specs/autonat/autonat-v2.md](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md) |

---

## Findings

Each finding below follows a Problem / Impact / Solution structure and
links to a dedicated deep-dive document for full root-cause analysis.
The testbed methodology and full numerical results are in
[testbed.md](testbed.md) and
[measurement-results.md](measurement-results.md).

### Finding 1: Inconsistent Global vs Per-Address Reachability (v1 vs v2)

**Category:** go-libp2p | **Severity:** High

**Problem:** AutoNAT v1 produces a **global** reachability flag — one
of {Public, Unknown, Private} for the whole node — while AutoNAT v2
produces **per-address** reachability — one of {Reachable, Unreachable,
Unknown} for each multiaddr. When both protocols run on the same node,
the two signals can disagree, and **there is no canonical reduction
defined by the spec or by any implementation**.

Both protocols select probe servers from the **same pool** of connected
peers (most Kubo nodes advertise both v1 and v2 server protocols), so
the same unreliable servers are candidates for both. The critical
difference is **how each protocol handles probe failures**:

- **v1** uses a sliding window of 3 (`maxConfidence=3`). **All
  non-success results — including timeouts, stream resets, and
  refusals — erode confidence.** After 4 consecutive non-success
  probes, confidence drains to 0 and the state flips to Unknown.
  Since the DHT treats Unknown the same as Private (both trigger
  client mode), **timeouts from honest-but-unreliable servers are
  sufficient to disrupt the DHT — no malicious peers needed.** With
  5/7 unreliable servers, v1 oscillates between Public and Unknown
  as the random probe selection alternates between reliable and
  unreliable peers. To flip to Private specifically (which also
  disables the v1 server), at least one `E_DIAL_ERROR` is needed
  after confidence is already drained to 0.
- **v2** uses a per-address sliding window of 5
  (`targetConfidence=3`). **Server failures (timeouts, stream resets,
  refusals, `E_DIAL_REFUSED`) are discarded entirely** — they do not
  affect the address's confidence. Only explicit `E_DIAL_ERROR`
  (server successfully connected and tried to dial back, but the NAT
  blocked it) counts as a failure. This means **server unreliability
  cannot cause state flips** — v2 only changes state based on
  genuine reachability changes. To flip from reachable (+3) to
  unreachable (-3), v2 requires 6 consecutive `E_DIAL_ERROR`
  results.

**Tradeoff:** v2's stability comes at the cost of slower reaction to
genuine changes. Once an address reaches high confidence, v2 re-probes
only every **1 hour** (primary address) or **3 hours** (secondary).
If reachability genuinely changes (NAT mapping expires, port forward
removed), v2 may take up to 1 hour to notice — whereas v1 rechecks
every 15 minutes. There is no event-driven re-probe trigger (e.g., on
connection loss or address change) defined by any implementation or
the spec.

For the full comparison of how each event (success, error, timeout,
refusal) changes state across all implementations and protocol
versions, see
[v1-v2-analysis.md § What Counts as Success vs Failure](v1-v2-analysis.md#4-what-counts-as-success-vs-failure).

[Testbed evidence](#key-metrics): with 5/7 unreliable servers,
**60% of v1 runs oscillate; 0% of v2 runs oscillate** (in go-libp2p
— rust and js have different v2 stability characteristics, see
[v1-v2-analysis.md](v1-v2-analysis.md)).

The disagreement only matters when a downstream subsystem consumes one
signal and not the other. **In go-libp2p this happens by default**:
DHT, AutoRelay, Address Manager, and NAT Service all subscribe to v1's
`EvtLocalReachabilityChanged` and do not consume v2's
`EvtHostReachableAddrsChanged`. Even when v2 has confirmed an address
is reachable, DHT/relay decisions follow v1's flipping signal — the
v1's global flag takes precedence over v2's per-address result.

| Consumer (go-libp2p) | Event consumed | v2 aware? | Affected by v1 oscillation? |
|---|---|---|---|
| Kademlia DHT | `EvtLocalReachabilityChanged` (v1) | No | Yes — DHT mode flips |
| AutoRelay | `EvtLocalReachabilityChanged` (v1) | No | Yes — relay reservation churn |
| Address Manager | `EvtLocalReachabilityChanged` (v1) | No | Yes |
| NAT Service | `EvtLocalReachabilityChanged` (v1) | No | Yes |

**rust-libp2p and js-libp2p are not affected today** because their
DHTs already consume v2-level signals: rust-libp2p's DHT uses
`ExternalAddrConfirmed` from the swarm; js-libp2p's DHT uses
`self:peer:update` from the address manager. Helia currently uses v1
only, so the question of v1/v2 coexistence hasn't arisen in practice.
However, **neither implementation documents a canonical behavior for
when both v1 and v2 run simultaneously**, and the spec doesn't define
one. A future project that wires both protocols into the same swarm
without defining the reduction could encounter the same
inconsistency.

**Impact:** Every go-libp2p deployment that relies on DHT participation
or relay decisions experiences intermittent routing degradation. DHT
queries fail when v1 flips the node to client mode; direct connections
are replaced by higher-latency relay paths; the cycle repeats as v1
flips back. Validator networks see higher-latency relay paths during
startup; IPFS nodes delay DHT participation and waste relay
reservations. This is the production phenomenon Obol observed (see
[Obol/Charon](https://github.com/ObolNetwork/charon)). For rust-libp2p and js-libp2p the impact is
latent — the inconsistency doesn't manifest today, but it isn't
prevented by anything either.

**Solution:** When v2 is enabled, **v2's per-address reachability
should be the source of truth for global reachability**. The canonical
reduction is: *PUBLIC if any v2-confirmed address is reachable;
UNREACHABLE if all v2 addresses are unreachable; UNKNOWN otherwise.*
This applies to all three implementations:

1. **go-libp2p (corrective):** Bridge v2 into v1's global flag with the
   reduction function above. Existing v1 consumers (DHT, AutoRelay,
   Address Manager, NAT Service) benefit from v2 without changing
   their code. As a stronger variant, suppress v1 probing entirely
   once v2 reaches `targetConfidence` — eliminating both the wasted
   probes and the oscillation at the source.
2. **rust-libp2p (preventive):** Document the reduction. The current
   behavior (DHT consumes v2 via `ExternalAddrConfirmed`) is correct,
   but nothing in the spec or in `libp2p-autonat` guarantees it. A
   future project that adds v1 alongside v2 should know how to combine
   them.
3. **js-libp2p (preventive):** Same. Once Helia or another consumer
   enables v2, the reduction should be in place from day one.

**Expected outcome:** DHT and AutoRelay stop flipping on v1
oscillation; nodes remain in server mode once v2 confirms any
address. Eliminates the oscillation-driven relay churn and DHT mode
switches observed in testbed (60% → 0%).

The deeper fix is at the **spec level**: AutoNAT v2 should mandate
the reduction so all implementations agree on what "global
reachability" means when v2 is the authoritative source. This is
consistent with the spec-gap framing in
[Cross-Implementation Comparison](#cross-implementation-comparison) —
the differences across implementations are the symptom; the spec
leaving behavior undefined is the root cause.

![v1/v2 Gap Comparison](../results/figures/10_v1_v2_gap_comparison.png)
*Figure 1: v1 oscillates (red segments) while v2 stays stable (green) across three unreliable-server ratios. go-libp2p testbed.*

**Code-level evidence:**

- **DHT subscribes to v1**: [`go-libp2p-kad-dht/subscriber_notifee.go#L32-L39`](https://github.com/libp2p/go-libp2p-kad-dht/blob/v0.39.0/subscriber_notifee.go#L32-L39) (subscription) and [`#L72-L75`](https://github.com/libp2p/go-libp2p-kad-dht/blob/v0.39.0/subscriber_notifee.go#L72-L75) (handler dispatch on `event.EvtLocalReachabilityChanged`)
- **DHT does NOT subscribe to v2**: zero references to `EvtHostReachableAddrsChanged` in `go-libp2p-kad-dht/subscriber_notifee.go` (verified by file inspection)
- **v1 sliding window** (`maxConfidence = 3`): [`go-libp2p/p2p/host/autonat/autonat.go#L24`](https://github.com/libp2p/go-libp2p/blob/v0.48.0/p2p/host/autonat/autonat.go#L24) (constant) and [`#L314-L372`](https://github.com/libp2p/go-libp2p/blob/v0.48.0/p2p/host/autonat/autonat.go#L314-L372) (`recordObservation` state machine)
- **v2 emits `EvtHostReachableAddrsChanged`** (zero consumers): [`go-libp2p/p2p/host/basic/addrs_manager.go#L396-L401`](https://github.com/libp2p/go-libp2p/blob/v0.48.0/p2p/host/basic/addrs_manager.go#L396-L401)

**[Testbed reproduction](measurement-results.md#6-v1v2-gap):** `v1-v2-gap.yaml` scenario — 20 runs comparing v1 and v2 under unreliable servers. v1 oscillates in 55% of runs; v2 is stable in all 20.

**Full analysis:** [v1-v2-analysis.md](v1-v2-analysis.md) — state transitions, wiring gap, fix options, and testbed performance data

### Finding 2: UDP Black Hole Detector Blocks QUIC Dial-Back

**Category:** go-libp2p | **Severity:** Medium

**Problem:** This is a **server-side** issue in go-libp2p. The UDP
black hole detector is a performance optimization that tracks UDP
connection success rates and blocks outbound UDP dials when too few
succeed — protecting nodes on networks that silently drop UDP traffic.
The AutoNAT v2 `dialerHost` (the internal host that performs dial-backs)
shares the main host's `UDPBlackHoleSuccessCounter`. On fresh servers
with zero UDP history, the counter enters `Blocked` state. When a client
requests a QUIC address test, the server's `dialerHost` refuses to
attempt the dial-back and responds with `E_DIAL_REFUSED`. From the
client's perspective, this is indistinguishable from "the server tried
and my NAT blocked it" — both count as failures toward the confidence
target. The result is a **false negative**: a genuinely QUIC-reachable
address is reported as unreachable.

**Impact:** Every go-libp2p AutoNAT v2 server is affected after startup,
until the main host accumulates enough successful UDP connections for the
counter to reach `Allowed` state. On long-running Kubo nodes with
diverse traffic this happens within minutes; on freshly deployed
infrastructure or isolated testbeds, the counter stays `Blocked`
indefinitely. TCP addresses are unaffected — the detector only gates
UDP/QUIC.

rust-libp2p and js-libp2p do not implement a black hole detector, so
they are not affected by this specific issue.

**Solution:** Disable the UDP black hole detector on `dialerHost`,
matching the existing v1 fix
([PR #2529](https://github.com/libp2p/go-libp2p/pull/2529)). The
`dialerHost` only dials addresses that clients explicitly request to
test — the dial result itself is the information the client needs, so
the detector should not suppress it (5 fix options analyzed in the
full analysis below).

**Expected outcome:** QUIC addresses correctly detected as reachable
on fresh go-libp2p servers without waiting for the main host's UDP
counter to accumulate history.

**Code-level evidence:**

- **`dialerHost` shares the main host's UDP counter** (the bug): [`go-libp2p/config/config.go#L313-L314`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/config/config.go#L313-L314) — `cfg.UDPBlackHoleSuccessCounter` is passed through to the v2 dialerHost
- **`BlackHoleSuccessCounter` state machine** with `Probing`/`Allowed`/`Blocked` states: [`go-libp2p/p2p/net/swarm/black_hole_detector.go#L45-L62`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/net/swarm/black_hole_detector.go#L45-L62)
- **`FilterAddrs` removes UDP addresses when state is `Blocked`** (the line that turns the false negative on): [`go-libp2p/p2p/net/swarm/black_hole_detector.go#L138-L175`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/net/swarm/black_hole_detector.go#L138-L175)
- **Existing v1 fix to mirror**: [PR #2529](https://github.com/libp2p/go-libp2p/pull/2529) — disables the detector for v1's dialerHost by passing `nil` counters; v2 needs the same treatment

**Testbed reproduction:** The testbed disables the black hole detector on servers via `libp2p.UDPBlackHoleSuccessCounter(nil)` as a workaround (see [udp-black-hole-detector.md § Testbed Workaround](udp-black-hole-detector.md#testbed-workaround)). Without this workaround, all QUIC scenarios in `matrix.yaml` fail with `E_DIAL_REFUSED`.

**Full analysis:** [udp-black-hole-detector.md](udp-black-hole-detector.md)

### Finding 3: Address-Restricted NAT False Positive

**Category:** Protocol design | **Severity:** Low

**Problem:** AutoNAT v2 produces 100% false positive rate for nodes
behind address-restricted NAT (EIM + ADF). The dial-back comes from the
same IP the client already contacted, so the NAT allows it through.
The node is technically reachable *from the testing server's IP* — but
AutoNAT declares it **globally reachable**, which is incorrect. ADF NAT
only permits inbound connections from IPs the client has previously
contacted. Peers connecting from other IPs will be blocked by the NAT.

**Impact:** Nodes behind ADF NAT would advertise addresses as globally
reachable when they are only reachable from previously contacted IPs.
In practice, **ADF is not known to be used by any modern consumer
router** — RFC 7857 moved recommendations toward APDF, and all
routers we are aware of default to port-restricted (APDF) filtering.
No measurement data exists to confirm ADF prevalence in the wild. This
is a protocol-level limitation — all implementations are affected
identically if ADF were encountered.

**Solution:** Require dial-back from a different IP than the one the
client contacted, when the server is multihomed (has multiple public
IPs available). This would distinguish ADF from full-cone NAT — if
the dial-back comes from an IP the client never contacted, an ADF NAT
blocks it, correctly revealing the restriction. When multihomed
servers are not available, the limitation should be documented in the
spec so implementations can flag the result as "reachable from
contacted IPs only" rather than "globally reachable."

**Expected outcome:** ADF nodes correctly classified as partially
reachable (not globally reachable), preventing them from advertising
addresses that most peers cannot reach.

**[Testbed evidence](measurement-results.md):** 120 runs (`adf-false-positive.yaml` scenario) — deterministic, not probabilistic.

| NAT type | Runs | Reported reachable | FPR |
|----------|------|-------------------|-----|
| Address-restricted (ADF) | 60 | 60/60 | **100%** |
| Port-restricted (APDF) | 60 | 0/60 | **0%** |

**Cross-implementation:** Protocol-level issue — affects all
implementations identically.

**Code-level evidence:**

- This is a **protocol-level limitation**, not an implementation issue: [autonat-v2 spec](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md) — the dial-back is performed by the same server that received the request, so the source IP is always one the client has previously contacted, which any ADF NAT would permit. There is no spec mechanism for cross-IP verification.
- All three implementations follow the spec faithfully; the testbed evidence (60/60 ADF, 0/60 APDF) confirms the limitation is deterministic and protocol-level. However, **no real-world deployment is known to be affected** since ADF is not used by modern consumer routers.

**Full analysis:** [adf-false-positive.md](adf-false-positive.md)

### Finding 4: Symmetric NAT Missing Signal

**Category:** Cross-implementation | **Severity:** Low

**Problem:** Under symmetric NAT (ADPM), each outbound connection uses a
different external port. The expected signal is UNREACHABLE (symmetric
NAT nodes are definitively unreachable — no external peer can initiate
a connection). Instead, go-libp2p and js-libp2p produce **no signal at
all** — the status remains Unknown indefinitely. All three
implementations fail to produce a timely reachability signal, but for
different reasons:

- **go-libp2p:** No address reaches `ActivationThresh=4` → AutoNAT v2
  never runs. However, the `ObservedAddrManager` does detect symmetric
  NAT at ~60s via `getNATType()` (classifies as `EndpointDependent`,
  emits `EvtNATDeviceTypeChanged`) — but no subsystem subscribes to this
  event. The detection exists, the response doesn't.
- **js-libp2p (TCP):** All TCP observed addresses are unconditionally
  dropped in Identify (`maybeAddObservedAddress()` returns early for any
  TCP address — see [js-libp2p#2620](https://github.com/libp2p/js-libp2p/issues/2620)).
  No candidates ever reach the address manager.
- **js-libp2p (QUIC):** Observed addresses do enter the pipeline and
  AutoNAT v2 runs, but every dial-back fails (the ephemeral port mapping
  only accepts traffic from the original destination). After 8 failures
  the address is removed. Since js-libp2p emits no reachability events,
  the failure is silent from the application's perspective.
- **rust-libp2p:** Not affected — no activation threshold, probes run
  immediately and correctly produce UNREACHABLE.

**Impact:** The status stays Unknown instead of transitioning to
Private. In go-libp2p, Private nodes disable their v1 AutoNAT server
(`service.Disable()`) because NAT adds failure modes to outbound
dial-backs (mapping timeouts, connection tracking limits), making
them statistically less reliable servers. Symmetric NAT nodes in
Unknown state **keep serving** — contributing less reliable servers to
other nodes' v1 probe pools, which increases the unreliable fraction
and can amplify v1 oscillation (F1). Additionally, AutoRelay only
activates on Private (not Unknown), so these nodes miss relay-based
inbound connectivity.

**Solution:** For go-libp2p: `AmbientAutoNAT` should subscribe to
`EvtNATDeviceTypeChanged` and transition to Private when symmetric
NAT (`EndpointDependent`) is detected. This disables the v1 server
(removing the node from other peers' server pools), activates
AutoRelay, and provides operator observability. The detection already
works (~60s via `getNATType()`); only the subscription is missing.
For js-libp2p: emit reachability events so QUIC dial-back failures
surface as UNREACHABLE rather than silent removal.

**Expected outcome:** Symmetric NAT nodes transition to Private
within ~60s — v1 server disabled, AutoRelay can be activated,
operators see a clear signal.

**[Testbed evidence](#experiment-matrix)** (`threshold-sensitivity.yaml` scenario):

| Threshold | NAT type | Result |
|-----------|----------|--------|
| 4 (default) | symmetric | NO SIGNAL |
| 1 | symmetric | **UNREACHABLE** (correct) |

**UPnP note:** UPnP-mapped addresses bypass the activation threshold
in go-libp2p (enter via `appendNATAddrs()`). However, this is not
relevant for real-world symmetric NAT: CGNAT and mobile carrier NAT
do not expose UPnP, and port forwarding is not available to users.
The UPnP bypass was confirmed on a port-restricted (cone) NAT home
router, not a symmetric NAT device.

**Cross-implementation:**
| | go-libp2p | rust-libp2p | js-libp2p |
|-|-----------|-------------|-----------|
| Affected? | **Yes** — NO SIGNAL (threshold blocks; `getNATType()` detects but nothing subscribes) | **No** — no threshold, produces UNREACHABLE | **Yes** — NO SIGNAL (TCP: excluded in Identify; QUIC: probes fail silently) |

**Code-level evidence:**

- **`ActivationThresh = 4` constant**: [`go-libp2p/p2p/host/observedaddrs/manager.go#L27`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/host/observedaddrs/manager.go#L27)
- **Threshold gate filters out v2 candidates**: [`manager.go#L230`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/host/observedaddrs/manager.go#L230) — `getTopExternalAddrs(..., ActivationThresh)` requires 4 distinct observers before an address reaches the v2 client
- **`getNATType()` correctly classifies symmetric NAT as `EndpointDependent`**: [`manager.go#L568`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/host/observedaddrs/manager.go#L568)
- **`EvtNATDeviceTypeChanged` emission with zero subscribers**: [`manager.go#L343`](https://github.com/libp2p/go-libp2p/blob/v0.47.0/p2p/host/observedaddrs/manager.go#L343) — emitted on symmetric NAT detection, but no go-libp2p subsystem subscribes to this event
- **js-libp2p drops all TCP observed addresses in identify**: [`js-libp2p/packages/protocol-identify/src/identify.ts#L130-L136`](https://github.com/libp2p/js-libp2p/blob/main/packages/protocol-identify/src/identify.ts#L130-L136) — early return for TCP, blocked on [js-libp2p#2620](https://github.com/libp2p/js-libp2p/issues/2620)

**Full analysis:** [symmetric-nat-silent-failure.md](symmetric-nat-silent-failure.md),
[#89](https://github.com/probe-lab/autonat-project/issues/89),
[upnp-nat-detection.md](upnp-nat-detection.md)

### Finding 5: rust-libp2p TCP Port Reuse Incorrect Metadata

**Category:** rust-libp2p | **Severity:** Low

**Problem:** rust-libp2p's TCP transport produces incorrect `PortUse`
metadata. When an outbound dial requests `PortUse::Reuse` but `bind()`
falls back to an ephemeral port (because the listen port isn't
available yet), the connection metadata still says `PortUse::Reuse`.
This is wrong — the connection used an ephemeral port, so the metadata
should say `PortUse::New`.

Identify's address-translation logic correctly trusts `PortUse`
metadata: when it sees `Reuse`, it skips port translation because the
peer already observed the listen port. This is the right behavior —
the bug is that the TCP transport lies about what happened. With
incorrect metadata, Identify passes through the ephemeral port,
AutoNAT v2 probes the wrong port, and the address is reported
UNREACHABLE.

**When this triggers:** The fallback happens when outbound dials begin
before the TCP listener finishes registering. In rust-libp2p,
`swarm.listen_on()` returns immediately but registration is
asynchronous; `local_dial_addr()` cannot find the listen address
until it completes. Applications that wait for `NewListenAddr` before
dialing do not trigger the fallback, so the incorrect metadata path
is never reached. QUIC is unaffected (single bound UDP socket,
observed address always correct).

**Impact:** The incorrect metadata causes an ephemeral-port address
to enter the address manager when it shouldn't. AutoNAT v2 triggers
an unnecessary probe on this address and reports it UNREACHABLE —
which is the correct result, since nothing listens on the ephemeral
port. When port reuse fails behind NAT, the node is genuinely
unreachable regardless of metadata: no NAT mapping exists for the
listen port. The final reachability outcome is the same whether the
metadata is correct or not — the bug does not cause a false negative.
Once port reuse succeeds on subsequent connections, the correct
address enters the pipeline separately and gets its own probe with
the right result. The practical impact is limited to confusing
diagnostics: an address that shouldn't exist appears in the address
manager, gets tested, and fails.

**Solution:** Fix `PortUse` metadata in `libp2p-tcp`: when the
outbound dial falls back to an ephemeral port, construct the
connection with `PortUse::New` instead of `PortUse::Reuse`. The
transport can detect the fallback by comparing `stream.local_addr()`
against the requested listen port. Identify's existing translation
logic then works as designed. No public API changes; the fix lives
entirely inside `libp2p-tcp`.

As a longer-term defense in depth, rust-libp2p could adopt
go-libp2p's `ObservedAddrManager` pattern — a second address-
consolidation layer that groups observations by thin waist (IP +
transport, port-independent) and replaces observed ports with the
listen port after enough consistent observations. This provides a
safety net independent of per-connection metadata.

**Expected outcome:** Correct `PortUse` metadata eliminates the
spurious ephemeral address from the address manager, removing the
unnecessary probe and the confusing diagnostic signal.

**Testbed reproduction:** Observed in the cross-implementation
testbed with a rust-libp2p client that dials immediately on startup
without waiting for `NewListenAddr`. The ephemeral address appeared
in the address manager and was probed unnecessarily. Adding the wait
ensured port reuse succeeded, so the correct address entered the
pipeline and the spurious one never appeared. No dedicated scenario
file; observed during cross-implementation validation runs.

**Cross-implementation:** go-libp2p is unaffected — its
`ObservedAddrManager` corrects ports independently of `PortUse`
metadata. js-libp2p is structurally not affected because Node.js TCP
doesn't support `SO_REUSEPORT` at all and js-libp2p drops all TCP
observed addresses in identify (see F4); if it didn't drop them, it
would have the same bug as rust-libp2p.

**Code-level evidence:**

- **`PortUse` enum** (`Reuse` is the default): [`rust-libp2p/core/src/transport.rs#L70-L81`](https://github.com/libp2p/rust-libp2p/blob/v0.56.0/core/src/transport.rs#L70-L81)
- **TCP transport `bind_addr` lookup that silently falls back to ephemeral** (the metadata-lying line — `PortUse::Reuse` stays even when the actual local port is ephemeral): [`rust-libp2p/transports/tcp/src/lib.rs#L323-L337`](https://github.com/libp2p/rust-libp2p/blob/v0.56.0/transports/tcp/src/lib.rs#L323-L337)
- **Identify only tracks ephemeral ports for connections marked `PortUse::New`**: [`rust-libp2p/protocols/identify/src/behaviour.rs#L284-L286`](https://github.com/libp2p/rust-libp2p/blob/v0.56.0/protocols/identify/src/behaviour.rs#L284-L286) — this is the trap; the failed-reuse connection is never inserted because its metadata claims `Reuse`
- **Address translation gated by the ephemeral-port set**: [`protocols/identify/src/behaviour.rs#L339-L341`](https://github.com/libp2p/rust-libp2p/blob/v0.56.0/protocols/identify/src/behaviour.rs#L339-L341) — translation is skipped, the wrong (ephemeral) port reaches AutoNAT v2 as the candidate

**Full analysis:** [rust-libp2p-autonat-implementation.md](rust-libp2p-autonat-implementation.md#address-candidate-selection)

---

## Cross-Implementation Comparison

The full cross-implementation evidence — findings matrix, feature
comparison, platform limitations, and v2 availability vs. production
deployment — is collected in
[cross-implementation-comparison.md](cross-implementation-comparison.md).

---

## Recommendations

### For go-libp2p (highest impact)

- **Make v2 the source of truth for global reachability (F1)** —
  Bridge v2 into the global flag with the reduction "PUBLIC if any
  v2-confirmed address is reachable", and suppress v1 probing once v2
  reaches `targetConfidence`. This eliminates v1 oscillation reaching
  DHT/AutoRelay/Address Manager and makes existing consumers benefit
  from v2's stability without changing their code.

- **Disable black hole detector on dialerHost (F2)** — Match the v1
  fix (PR #2529). [5 options analyzed](udp-black-hole-detector.md#proposed-upstream-fixes).

### For rust-libp2p

- **Fix silent `PortUse::Reuse` fallback (F5)** — When TCP port reuse
  falls back to an ephemeral port, the TCP transport should construct
  the connection with `PortUse::New` so identify's existing translation
  logic works. The fix is contained inside `libp2p-tcp` (no public API
  change). As a longer-term hardening, add an `ObservedAddrManager`-
  equivalent that consolidates observed addresses by thin waist
  independently of per-connection metadata. **Note:** application
  authors should also wait for `NewListenAddr` events before starting
  outbound dials — without that, the race condition triggers regardless
  of the library fix.

### For js-libp2p

- **Emit reachability events (F4)** — Expose autonat v2 probe results
  to consumers.

### For the AutoNAT v2 spec

- **Define state transitions, confidence semantics, and v2-priority reduction (F1–F5)** —
  The spec defines the wire protocol but not the client-side state
  machine. Each implementation has independently designed different
  confidence systems, error classification, and re-probe schedules.
  The spec should define confidence thresholds, how each server
  response affects state, re-probe triggers, and the event surface.
  It should also mandate that when both v1 and v2 run, v2's per-address
  result determines global reachability. Today rust-libp2p and js-libp2p
  are not affected because their DHT already consumes v2-level signals,
  but this is an implementation choice, not a spec requirement.

- **Document ADF limitation (F3)** — The protocol cannot distinguish
  address-restricted from full-cone NAT because the dial-back always
  comes from a previously contacted IP. Require dial-back from a
  different IP when multihomed servers are available; document the
  limitation for single-IP servers. ADF is not known to be used by
  modern consumer routers, so this may be accepted as a known
  limitation.

### Proposed upstream issues

The following table includes issues could be opened to the original repos to discuss the findings. Each maps to a specific finding
and includes the proposed fix.

| Repository | Title | Finding | Proposed fix |
|---|---|---|---|
| [specs](https://github.com/libp2p/specs) | AutoNAT v2: define state transitions, confidence semantics, and v2-priority reduction | F1–F5 | The spec defines the wire protocol but not the client-side state machine. Each implementation has independently designed different confidence systems, error classification, and re-probe schedules (see [v1-v2-analysis.md](v1-v2-analysis.md)). The spec should define: (a) confidence thresholds, (b) how each server response affects state, (c) re-probe triggers for connectivity changes, (d) the event surface for consumers, and (e) canonical reduction for global reachability when both v1 and v2 run ("PUBLIC if any v2-confirmed address is reachable; UNREACHABLE if all unreachable; UNKNOWN otherwise"). This directly fixes the go-libp2p wiring gap (DHT/AutoRelay consuming v1 instead of v2) by making the expected behavior a spec requirement, not an implementation choice. *Requires follow-up PRs in all three implementations.* |
| [specs](https://github.com/libp2p/specs) | AutoNAT v2: ADF false positive — dial-back always from trusted IP | F3 | Require dial-back from a different IP when multihomed servers available. Document the limitation for single-IP servers. ADF is rare (most routers default to APDF) so this may be accepted as a known limitation. *If adopted, requires follow-up PRs in all three implementations to support multi-IP dial-back.* |
| [go-libp2p](https://github.com/libp2p/go-libp2p) | AutoNAT v2 `dialerHost` should disable UDP black hole detector | F2 | Set `UDPBlackHoleSuccessCounter: nil` in `makeAutoNATV2Host()`, matching the v1 fix ([PR #2529](https://github.com/libp2p/go-libp2p/pull/2529)). The dial-back result is the information the client needs; the detector should not suppress it. |
| [go-libp2p](https://github.com/libp2p/go-libp2p) | Symmetric NAT: `EvtNATDeviceTypeChanged` emitted but has zero subscribers | F4 | Wire `getNATType()` detection (`EndpointDependent`) into either lowering `ActivationThresh` or emitting UNREACHABLE directly. Testbed confirms `ActivationThresh=1` produces correct UNREACHABLE. |
| [rust-libp2p](https://github.com/libp2p/rust-libp2p) | Identify skips address translation when TCP port reuse silently falls back to ephemeral | F5 | TCP transport should construct the connection with `PortUse::New` when `bind()` falls back to ephemeral (compare `stream.local_addr()` against listen port). No public API change; fix contained in `libp2p-tcp`. |
| [js-libp2p](https://github.com/libp2p/js-libp2p) | AutoNAT v2 emits no reachability events to consumers | F4 | Expose v2 probe results via EventEmitter or observable. Currently tracked internally in `dialResults` Map but not surfaced. Related: [js-libp2p#2620](https://github.com/libp2p/js-libp2p/issues/2620) (TCP observed addrs dropped). |

---

## Testbed

Findings were explored and validated using a Docker-based testbed with
configurable NAT types (iptables), transport protocols (TCP/QUIC),
network conditions (latency, packet loss), and server configurations
(reliable/unreliable, server count). Clients run go-libp2p (primary),
rust-libp2p, or js-libp2p behind a NAT router; servers run go-libp2p
with OTel instrumentation. Each scenario produces OTel traces analyzed
for FNR, FPR, convergence time, and oscillation.

**183 runs** across 7 scenario files, covering 5 NAT types, 2
transports, latency/loss injection, v1/v2 oscillation comparison
(20 runs), and cross-implementation validation.

Key results: **0% FNR/FPR** for all non-edge-case NAT types, ~6s
convergence, v1 oscillates in 55% of runs with unreliable servers
while v2 is stable in all 20. Both TCP and QUIC maintain correctness
under packet loss with no consistent convergence advantage for either.

For full details:
- [testbed.md](testbed.md) — architecture, NAT rules, scenario parameters
- [measurement-results.md](measurement-results.md) — all results, trace timelines, convergence heatmaps
- [scenario-schema.md](scenario-schema.md) — YAML scenario format

---

## Glossary

| Acronym | Full Name | Description |
|---------|-----------|-------------|
| **NAT** | Network Address Translation | Maps private IPs to public ones |
| **AutoNAT** | Automatic NAT Detection | libp2p protocol for testing address reachability |
| **EIM** | Endpoint-Independent Mapping | Same external port regardless of destination (cone NAT) |
| **ADPM** | Address- and Port-Dependent Mapping | Different external port per destination (symmetric NAT) |
| **EIF** | Endpoint-Independent Filtering | Allows inbound from any source (full-cone) |
| **ADF** | Address-Dependent Filtering | Allows inbound only from previously contacted IPs (address-restricted) |
| **APDF** | Address- and Port-Dependent Filtering | Allows inbound only from exact previously contacted IP:port (port-restricted) |
| **DHT** | Distributed Hash Table | Kademlia-based peer discovery in libp2p |
| **TTC** | Time-to-Confidence | Time from node start to stable reachability determination |
| **TTU** | Time-to-Update | Time to detect a mid-session reachability change |
| **FNR** | False Negative Rate | Fraction of reachable nodes incorrectly classified as unreachable |
| **FPR** | False Positive Rate | Fraction of unreachable nodes incorrectly classified as reachable |
| **DCUtR** | Direct Connection Upgrade through Relay | libp2p hole punching protocol |
