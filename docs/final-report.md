# AutoNAT v2: Performance Analysis and Cross-Implementation Study

**Date:** 2026-03-19
**Protocol:** AutoNAT v2 (`/libp2p/autonat/2/dial-request`, `/libp2p/autonat/2/dial-back`)
**Spec:** https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md
**Implementations tested:** go-libp2p v0.47.0, rust-libp2p v0.54, js-libp2p v3.1
**Testbed:** Docker-based lab with configurable NAT types (iptables)
**Repository:** https://github.com/probe-lab/autonat-project

---

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
  report NAT-related connectivity issues — primarily low hole-punching
  success rates and relay churn — although these are directly related
  to hole punching and relay behavior, not to AutoNAT v2 specifically.
  See [obol.md](obol.md) for details.

- **Avail Network** ([avail-light](https://github.com/availproject/avail-light),
  rust-libp2p v0.55.0): Light clients reported persistent
  "autonat-over-quic libp2p errors" starting from v1.7.4, caused by
  QUIC connection reuse producing false positives
  ([rust-libp2p#3900](https://github.com/libp2p/rust-libp2p/issues/3900),
  since fixed by [PR #4568](https://github.com/libp2p/rust-libp2p/pull/4568)).
  However, Avail **disabled AutoNAT entirely** in v1.13.2 (September
  2025) before the upstream fix shipped, forcing operators to manually
  set `--external-address` for DHT server mode. See [avail.md](avail.md)
  for details.

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

AutoNAT v2 is a significant improvement over v1 in per-address
reachability detection. In [controlled testbed conditions](#testbed),
it produces **0% false negative rate and 0% false positive rate**
across all non-edge-case NAT types, converges in ~6 seconds, and
maintains correctness under high latency and packet loss (both TCP
and QUIC show 0% FNR/FPR at all tested loss levels; convergence time
increases but neither transport shows a consistent advantage — see
[Transport Resilience](#transport-resilience-under-packet-loss)).

However, we identified **5 findings** that affect its real-world
effectiveness — ranging from protocol-level design issues to
implementation gaps. A separate
[cross-implementation comparison](cross-implementation-comparison.md)
collects the survey data on how each finding manifests in go-libp2p,
rust-libp2p, and js-libp2p.

The most impactful finding is that **global (v1) and per-address (v2)
reachability can disagree, and there is no canonical way to reconcile
them**. In go-libp2p — the only implementation where v2 is deployed in
production — DHT and AutoRelay subscribe to v1's global flag and are
blind to v2's per-address signal. v1 oscillates under realistic
conditions (60% of testbed runs with 5/7 unreliable servers flip
Public ↔ Private; 0% of v2 runs do), so the unstable global flag
overrides v2's stable per-address result and drives DHT/relay decisions.
This directly explains the oscillation observed by Obol. rust-libp2p
and js-libp2p don't have the bug today, but only by accident — neither
documents what should happen if both protocols run, and the spec
doesn't either.

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

For the cross-implementation findings matrix, feature comparison,
adoption status, and local UPnP test results, see
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

### How NAT Filtering Affects AutoNAT v2 Dial-Back

When the server's `dialerHost` dials back to the client, the NAT's
filtering decision determines whether the connection reaches the client.
In this example, 203.0.113.1 is the client's NAT-mapped external IP
and 1.2.3.4 is the server's IP:

```
Client behind NAT contacted Server at 1.2.3.4:5000
NAT mapping: client:4001 → 203.0.113.1:50000 (external)

Server's dialerHost dials back from 1.2.3.4:random_port to 203.0.113.1:50000

Full-cone (EIF):       "Any source allowed"                → PASS
Addr-restricted (ADF): "Is 1.2.3.4 contacted? YES"        → PASS ← Finding #3
Port-restricted (APDF):"Is 1.2.3.4:random contacted? NO"  → BLOCK
Symmetric (ADPM+APDF): N/A — address never activated       ← Finding #4
```

The PASS/BLOCK outcomes are determined by the NAT type, not by AutoNAT.
Finding #3 (ADF) is notable because the dial-back succeeds — the server's
IP is already in the NAT's filter — making the node appear globally
reachable when it is only reachable from previously contacted IPs.
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
The testbed methodology, experiment matrix, and full numerical results
that back the findings are in the [Testbed](#testbed) section at the
end of this report — most-cited numbers (FNR/FPR — False Negative /
False Positive Rate, oscillation percentages, TTC — Time to
Confidence) are summarized in the [Key Metrics](#key-metrics)
subsection there.

### Finding 1: Inconsistent Global vs Per-Address Reachability (v1 vs v2)

**Category:** go-libp2p | **Severity:** High

**Problem:** AutoNAT v1 produces a **global** reachability flag — one
of {Public, Unknown, Private} for the whole node — while AutoNAT v2
produces **per-address** reachability — one of {Reachable, Unreachable,
Unknown} for each multiaddr. When both protocols run on the same node,
the two signals can disagree, and **there is no canonical reduction
defined by the spec or by any implementation**.

The two protocols disagree because they use different state-update
mechanisms:

- **v1** uses a sliding window of 3 with random server selection.
  Critically, server failures (timeouts, stream resets, refusals)
  don't directly flip state but **erode confidence** — after 4
  consecutive failures, confidence drains to 0 and the state flips.
  With 5/7 unreliable servers, the probability of selecting 2
  unreliable peers in a row is (5/7)² ≈ 51%, and 2 consecutive
  failures are enough to flip the window from Public [S,S,S] →
  [S,F,F] (Private). v1 oscillates under realistic conditions.
- **v2** uses explicit server selection and per-address confidence
  (`targetConfidence = 3` in go-libp2p). Server failures are
  **discarded entirely** — only explicit `E_DIAL_ERROR` (server tried
  and failed to reach the address) counts as evidence. v2 does not
  oscillate in our testbed.

For the full comparison of how each event (success, error, timeout,
refusal) changes state across all implementations and protocol
versions, see
[v1-v2-state-transitions.md § What Counts as Success vs Failure](v1-v2-state-transitions.md#4-what-counts-as-success-vs-failure).

[Testbed evidence](#key-metrics): with 5/7 unreliable servers,
**60% of v1 runs oscillate; 0% of v2 runs oscillate** (in go-libp2p
— rust and js have different v2 stability characteristics, see
[v1-v2-state-transitions.md](v1-v2-state-transitions.md)).

The disagreement only matters when a downstream subsystem consumes one
signal and not the other. **In go-libp2p this happens by default**:
DHT, AutoRelay, Address Manager, and NAT Service all subscribe to v1's
`EvtLocalReachabilityChanged` and are blind to v2's
`EvtHostReachableAddrsChanged`. Even when v2 has confirmed an address
is reachable, DHT/relay decisions follow v1's flipping signal — the
unstable global flag dominates the stable per-address result.

| Consumer (go-libp2p) | Event consumed | v2 aware? | Affected by v1 oscillation? |
|---|---|---|---|
| Kademlia DHT | `EvtLocalReachabilityChanged` (v1) | No | Yes — DHT mode flips |
| AutoRelay | `EvtLocalReachabilityChanged` (v1) | No | Yes — relay reservation churn |
| Address Manager | `EvtLocalReachabilityChanged` (v1) | No | Yes |
| NAT Service | `EvtLocalReachabilityChanged` (v1) | No | Yes |

**rust-libp2p and js-libp2p don't have the bug today**, but only by
accident: rust-libp2p's DHT consumes `ExternalAddrConfirmed` from the
swarm, which v2 emits directly; js-libp2p's DHT consumes
`self:peer:update` from the address manager, which v2 also feeds.
Helia currently uses v1 only, so the question hasn't come up. **Neither
implementation has a documented behavior for what should happen if
both v1 and v2 run simultaneously**, and the spec doesn't say either.
A future project that wires both into the same swarm could rediscover
the same bug independently.

**Impact:** Every go-libp2p deployment that relies on DHT participation
or relay decisions experiences intermittent routing degradation. DHT
queries fail when v1 flips the node to client mode; direct connections
are replaced by higher-latency relay paths; the cycle repeats as v1
flips back. Validator networks see higher-latency relay paths during
startup; IPFS nodes delay DHT participation and waste relay
reservations. This is the production phenomenon Obol observed (see
[obol.md](obol.md)). For rust-libp2p and js-libp2p the impact is
latent — the bug doesn't manifest today, but it isn't prevented by
anything either.

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

| Metric | v1 | v2 |
|--------|----|----|
| Oscillation rate (5/7 unreliable, go-libp2p) | [60% of runs](#key-metrics) | **0%** |
| Stability after convergence | Flips on random peer failure | Stable (`targetConfidence`=3) |

**Cross-implementation:**

| | go-libp2p | rust-libp2p | js-libp2p |
|-|-----------|-------------|-----------|
| DHT consumes v1 global flag? | **Yes** — `EvtLocalReachabilityChanged` | No — uses `ExternalAddrConfirmed` (v2-fed) | No — uses `self:peer:update` (v2-fed) |
| Bug manifests today? | **Yes** — DHT/relay mode flips with v1 oscillation | No — DHT already uses v2-level signal | No — Helia uses v1 only, v2 not deployed |
| Documented reduction if both run? | No | No | No |

**Code-level evidence:**

- **DHT subscribes to v1**: [`go-libp2p-kad-dht/subscriber_notifee.go#L32-L39`](https://github.com/libp2p/go-libp2p-kad-dht/blob/v0.39.0/subscriber_notifee.go#L32-L39) (subscription) and [`#L72-L75`](https://github.com/libp2p/go-libp2p-kad-dht/blob/v0.39.0/subscriber_notifee.go#L72-L75) (handler dispatch on `event.EvtLocalReachabilityChanged`)
- **DHT does NOT subscribe to v2**: zero references to `EvtHostReachableAddrsChanged` in `go-libp2p-kad-dht/subscriber_notifee.go` (verified by file inspection)
- **v1 sliding window** (`maxConfidence = 3`): [`go-libp2p/p2p/host/autonat/autonat.go#L24`](https://github.com/libp2p/go-libp2p/blob/v0.48.0/p2p/host/autonat/autonat.go#L24) (constant) and [`#L314-L372`](https://github.com/libp2p/go-libp2p/blob/v0.48.0/p2p/host/autonat/autonat.go#L314-L372) (`recordObservation` state machine)
- **v1 random server selection** (Fisher-Yates shuffle in `getPeerToProbe`): [`go-libp2p/p2p/host/autonat/autonat.go#L400-L425`](https://github.com/libp2p/go-libp2p/blob/v0.48.0/p2p/host/autonat/autonat.go#L400-L425)
- **v2 emits `EvtHostReachableAddrsChanged`** (zero consumers): [`go-libp2p/p2p/host/basic/addrs_manager.go#L396-L401`](https://github.com/libp2p/go-libp2p/blob/v0.48.0/p2p/host/basic/addrs_manager.go#L396-L401)

**Full analysis:** [v1-v2-reachability-gap.md](v1-v2-reachability-gap.md), [v1-vs-v2-performance.md](v1-vs-v2-performance.md), [v1-v2-state-transitions.md](v1-v2-state-transitions.md)

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

**Impact:** Nodes behind ADF NAT advertise addresses as globally
reachable. Peers from other IPs attempting direct connections are
blocked by the NAT, adding latency before relay fallback. Real-world
impact is likely low (ADF is rare in modern routers, most default to
APDF), but no measurement data exists to quantify prevalence. This is
a protocol-level issue — all implementations are affected identically.

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

**[Testbed evidence](#experiment-matrix):** 120 runs (`adf-false-positive.yaml` scenario) — deterministic, not probabilistic.

| NAT type | Runs | Reported reachable | FPR |
|----------|------|-------------------|-----|
| Address-restricted (ADF) | 60 | 60/60 | **100%** |
| Port-restricted (APDF) | 60 | 0/60 | **0%** |

![Detection Correctness](../results/figures/05_detection_correctness.png)
*Figure 2: Detection correctness heatmap — address-restricted reports reachable (false positive).*

**Cross-implementation:** Protocol-level issue — affects all
implementations identically.

**Code-level evidence:**

- The bug is in the **AutoNAT v2 spec design**, not in any implementation: [autonat-v2 spec](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md) — the dial-back is performed by the same server that received the request, so the dial-back source IP is always an IP the client has previously contacted, which any ADF NAT will permit. There is no spec mechanism for cross-IP verification.
- All three implementations follow the spec faithfully and produce 100% FPR; the testbed evidence above (60/60 ADF runs report reachable, 0/60 APDF runs report reachable) confirms the failure is deterministic and protocol-level, not implementation-specific.

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

**Impact:** Nodes behind symmetric NAT are **definitively unreachable
on their IPv4 NAT path** by definition — CGNAT and mobile carrier NAT
do not support UPnP or port forwarding, so there is no way to open an
inbound IPv4 path. **DCUtR hole punching cannot rescue symmetric NAT
either:** DCUtR is a coordinated simultaneous-dial protocol that
relies on the public port being stable across destinations (EIM
mapping). Symmetric NAT picks a different external port per
destination — exactly the property DCUtR depends on. The only working
inbound path is through a relay, and that path is permanent (never
upgraded to direct). The practical outcome (no DHT server queries, no
direct connections) is the same whether the signal is "unknown" or
"unreachable."

*Exception:* peers with native IPv6 alongside CGNAT'd IPv4 are
reachable directly on the v6 path without any NAT involved — most
modern mobile carriers (T-Mobile, Verizon, EE, Deutsche Telekom)
issue dual-stack. Applications that advertise and prefer v6 addresses
can sometimes reach "CGNAT'd" peers without needing relay or hole
punching at all. This is not hole punching — it's just preferring the
unNATted path.

The real impact is operational, not functional:

- **Missing relay activation:** In go-libp2p, AutoRelay activates on
  `Private`, not `Unknown`. Without an explicit UNREACHABLE signal,
  the node may never reserve a relay path — and since the relay path
  is the *only* viable inbound path for symmetric NAT (DCUtR cannot
  help), the node is silently isolated. There is no false-positive
  risk: the system never incorrectly claims a symmetric NAT node is
  reachable.
- **No observability:** Operators cannot distinguish "still waiting for
  AutoNAT" from "definitively behind symmetric NAT." There is no
  metric or event to diagnose the situation.

Estimated ~11% of peers are behind symmetric NAT (Halkes 2011; current
numbers unknown). go-libp2p and js-libp2p are both affected; only
rust-libp2p correctly produces UNREACHABLE.

**Solution:** For go-libp2p: wire the existing `getNATType()` detection
(which correctly identifies symmetric NAT as `EndpointDependent`) into
either lowering the activation threshold or emitting UNREACHABLE
directly. With `ActivationThresh=1`, testbed confirms correct
UNREACHABLE determination. The security tradeoff is small: observer-IP
deduplication means a single attacker IP can only contribute 1
observation regardless of sybil count. For js-libp2p: emit reachability
events so that QUIC dial-back failures surface as UNREACHABLE rather
than silent removal.

**Expected outcome:** Symmetric NAT nodes receive explicit UNREACHABLE
within ~60s; AutoRelay activates and establishes a relay path — the
only viable inbound connectivity for these nodes.

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

### Finding 5: rust-libp2p TCP Port Reuse Safety Net

**Category:** rust-libp2p | **Severity:** Low

**Problem:** rust-libp2p's TCP transport defaults to `PortUse::Reuse`
for outbound dials — the outbound socket is bound to the listen port so
peers observe the correct address. If `bind()` fails (e.g., the listener
isn't registered yet because TCP listener setup is asynchronous), the
kernel silently picks an ephemeral port, but **the connection metadata
still says `PortUse::Reuse`**. Identify's address-translation logic
trusts that metadata and skips translation; AutoNAT v2 then probes the
wrong (ephemeral) port and reports the address UNREACHABLE. Result:
100% false negative on TCP for any node where outbound dialing races
ahead of TCP listener registration.

**Impact:** This bug only triggers under a specific startup-timing race
— outbound dials must begin **before** the TCP listener finishes
registering. In rust-libp2p, calling `swarm.listen_on()` returns
immediately but listener registration is asynchronous;
`local_dial_addr()` cannot find the listen address until it completes.
**Applications that wait for the corresponding `NewListenAddr` event
before dialing peers do not hit this bug.** Applications that start
dialing peers immediately on startup do.

We hit it in our testbed because the runner connected to peers
immediately on startup, without waiting for listeners. After adding a
wait for `NewListenAddr` events, both TCP and QUIC reported REACHABLE
— the bug disappeared with **no library change**, just an
application-side fix. So in practice this is a **latent library bug
guarded by application discipline**: the library accepts a wrong-input
pattern (dial-before-listen) silently instead of either failing loudly
or correcting itself.

When the race does trigger, the affected node is TCP-unreachable from
AutoNAT v2's perspective despite having working public TCP listeners.
QUIC is unaffected (single bound UDP socket, observed address always
correct). Operators see a confusing asymmetry — QUIC works, TCP
doesn't, same node, same IP. Because rust-libp2p's DHT consumes
`ExternalAddrConfirmed` from v2, the TCP address never gets confirmed
and never enters the routing table; nodes that don't enable QUIC stay
out of the DHT entirely.

**Real-world prevalence is unknown.** No survey has measured how many
production rust-libp2p applications wait for `NewListenAddr` before
dialing. The pattern isn't documented as required, and there is no
warning if you skip it.

**Solution:** Two-part fix.

1. **Make `PortUse` accurate (immediate, contained in `libp2p-tcp`):**
   when the TCP transport's outbound dial falls back from `Reuse` to
   an ephemeral port, the connection should be constructed with
   `PortUse::New` instead of `PortUse::Reuse`. The transport can detect
   the fallback by comparing `stream.local_addr()` against the
   requested listen port. Identify's existing translation logic then
   works as designed. No public API changes; the fix lives entirely
   inside `libp2p-tcp`.
2. **Add an `ObservedAddrManager`-equivalent (longer term, defense in
   depth):** rust-libp2p should adopt go-libp2p's pattern of a second
   independent address-consolidation layer that groups observations by
   thin waist (IP + transport, port-independent) and replaces observed
   ports with the listen port after enough consistent observations.
   This provides a safety net independent of per-connection metadata,
   protecting against future bugs of this class.

**Expected outcome:** TCP addresses correctly detected as reachable
regardless of listener startup timing. Nodes that only enable TCP
(no QUIC) can enter DHT server mode.

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
comparison, local UPnP test results, platform limitations, and v2
availability vs. production deployment — is collected in
[cross-implementation-comparison.md](cross-implementation-comparison.md).

It is intentionally separate from the F1–F5 findings because it is a
survey, not a finding with a single fix. The differences across
implementations are the symptom; the root cause is that the AutoNAT v2
spec standardizes only the wire protocol, not the surrounding
behaviors (activation thresholds, address selection, event surface,
port translation, DHT wiring). Each implementation has independently
made different choices, and those choices are what produce F1, F4, F5,
and parts of F2. The proposed fix to the root cause is in the
[Recommendations](#recommendations) section below.

---

## Recommendations

### For go-libp2p (highest impact)

1. **Make v2 the source of truth for global reachability (F1)** —
   Bridge v2 into the global flag with the reduction "PUBLIC if any
   v2-confirmed address is reachable", and suppress v1 probing once v2
   reaches `targetConfidence`. This eliminates v1 oscillation reaching
   DHT/AutoRelay/Address Manager and makes existing consumers benefit
   from v2's stability without changing their code.

2. **Disable black hole detector on dialerHost (F2)** — Match the v1
   fix (PR #2529). [5 options analyzed](udp-black-hole-detector.md#proposed-upstream-fixes).

### For rust-libp2p

3. **Fix silent `PortUse::Reuse` fallback (F5)** — When TCP port reuse
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

4. **Emit reachability events (F4)** — Expose autonat v2 probe results
   to consumers.

5. **Upgrade Helia to v2** — v1's monotonic counters are
   oscillation-resistant but v2 provides per-address granularity.

### For all implementations

6. **Document and adopt the v2-priority reduction (F1)** — When both
   v1 and v2 run, v2's per-address result should determine global
   reachability via the reduction "PUBLIC if any v2-confirmed address
   is reachable". Today rust-libp2p and js-libp2p don't have go-libp2p's
   bug, but only by accident — neither implementation documents the
   rule, and the spec doesn't mandate it. Future projects that wire
   both protocols should not rediscover this independently.

### For the ecosystem

7. **Measure real-world NAT type distribution** — Deploy monitoring to
   quantify ADF prevalence, symmetric NAT fraction, and v2 adoption.

### Proposed upstream issues

The following issues should be discussed internally and/or with
libp2p maintainers before filing. Each maps to a specific finding
and includes the proposed fix.

| # | Repository | Title | Finding | Proposed fix |
|---|---|---|---|---|
| 1 | [specs](https://github.com/libp2p/specs) | AutoNAT v2: define state transitions and confidence semantics | F1–F5 | The spec defines the wire protocol but not the client-side state machine: how many probes are needed, how failures/timeouts/refusals affect confidence, when to re-probe, and what events to emit. Each implementation has independently designed different confidence systems (go: sliding window of 5, targetConfidence=3; rust: single probe, no accumulation; js: fixed thresholds 4/8, monotonic counters + TTL), different error classification (go v1 treats timeouts as evidence; go v2 discards them; rust ignores errors), and different re-probe schedules. The spec should define: (a) confidence thresholds, (b) how each server response (OK, E_DIAL_ERROR, E_DIAL_REFUSED, E_DIAL_BACK_ERROR, timeout) affects state, and (c) the event surface for consumers. See [v1-v2-state-transitions.md](v1-v2-state-transitions.md). *Requires follow-up PRs in go-libp2p, rust-libp2p, and js-libp2p to align their implementations with the agreed spec.* |
| 2 | [specs](https://github.com/libp2p/specs) | AutoNAT v2: mandate v2-priority reduction for global reachability | F1 | Spec should define canonical reduction: "PUBLIC if any v2-confirmed address is reachable; UNREACHABLE if all unreachable; UNKNOWN otherwise." This directly fixes the go-libp2p wiring gap (DHT/AutoRelay consuming v1 instead of v2) by making the expected behavior a spec requirement, not an implementation choice. *Requires follow-up PRs in each implementation to wire the reduction into their DHT/relay/address-manager subsystems.* |
| 3 | [specs](https://github.com/libp2p/specs) | AutoNAT v2: ADF false positive — dial-back always from trusted IP | F3 | Require dial-back from a different IP when multihomed servers available. Document the limitation for single-IP servers. ADF is rare (most routers default to APDF) so this may be accepted as a known limitation. *If adopted, requires follow-up PRs in all three implementations to support multi-IP dial-back.* |
| 4 | [go-libp2p](https://github.com/libp2p/go-libp2p) | AutoNAT v2 `dialerHost` should disable UDP black hole detector | F2 | Set `UDPBlackHoleSuccessCounter: nil` in `makeAutoNATV2Host()`, matching the v1 fix ([PR #2529](https://github.com/libp2p/go-libp2p/pull/2529)). The dial-back result is the information the client needs; the detector should not suppress it. |
| 5 | [go-libp2p](https://github.com/libp2p/go-libp2p) | Symmetric NAT: `EvtNATDeviceTypeChanged` emitted but has zero subscribers | F4 | Wire `getNATType()` detection (`EndpointDependent`) into either lowering `ActivationThresh` or emitting UNREACHABLE directly. Testbed confirms `ActivationThresh=1` produces correct UNREACHABLE. |
| 6 | [rust-libp2p](https://github.com/libp2p/rust-libp2p) | Identify skips address translation when TCP port reuse silently falls back to ephemeral | F5 | TCP transport should construct the connection with `PortUse::New` when `bind()` falls back to ephemeral (compare `stream.local_addr()` against listen port). No public API change; fix contained in `libp2p-tcp`. |
| 7 | [js-libp2p](https://github.com/libp2p/js-libp2p) | AutoNAT v2 emits no reachability events to consumers | F4 | Expose v2 probe results via EventEmitter or observable. Currently tracked internally in `dialResults` Map but not surfaced. Related: [js-libp2p#2620](https://github.com/libp2p/js-libp2p/issues/2620) (TCP observed addrs dropped). |

---

## Testbed

This section is the methodology and numerical-results appendix for the
report. Each subsection below backs a different part of the main body:
the [Architecture](#architecture) and
[Scenario Parameters](#scenario-parameters) describe how the experiments
are run; the [Experiment Matrix](#experiment-matrix) lists the
scenarios that produced the data cited in [Findings](#findings) (e.g.,
F3's `adf-false-positive.yaml`, F4's `threshold-sensitivity.yaml`, F1's
`v1-v2-gap.yaml`); [Metrics Collected](#metrics-collected) defines what
`analyze.py` extracts; [Key Metrics](#key-metrics) summarizes the
headline numbers used throughout Findings and Recommendations; and
[Transport Resilience Under Packet Loss](#transport-resilience-under-packet-loss)
plus [Convergence Heatmaps](#convergence-heatmaps) document the deeper
investigation behind the resilience claims in the
[Executive Summary findings](#findings).

Docker-based lab with configurable NAT types via iptables on a Linux
host. All experiments run in isolated Docker networks with no external
traffic. For full architecture details, see [testbed.md](testbed.md).
Scenario format reference: [scenario-schema.md](scenario-schema.md).

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  public-net (73.0.0.0/24)                               │
│                                                         │
│  ┌──────────┐ ┌──────────┐     ┌──────────┐            │
│  │ Server 1 │ │ Server 2 │ ... │ Server 7 │  (go-libp2p)│
│  │ 73.0.0.3 │ │ 73.0.0.4 │     │ 73.0.0.9 │            │
│  └──────────┘ └──────────┘     └──────────┘            │
│                                                         │
│  ┌──────────┐              ┌──────────┐                 │
│  │  Jaeger  │              │  Router  │                 │
│  │ 73.0.0.50│              │ 73.0.0.2 │                 │
│  └──────────┘              └────┬─────┘                 │
└─────────────────────────────────┼───────────────────────┘
                                  │ NAT (iptables)
┌─────────────────────────────────┼───────────────────────┐
│  private-net (10.0.1.0/24)      │                       │
│                            ┌────┴─────┐                 │
│                            │  Router  │                 │
│                            │ 10.0.1.2 │                 │
│                            └──────────┘                 │
│  ┌──────────┐                                           │
│  │  Client  │  (go / rust / js libp2p)                  │
│  │ 10.0.1.10│                                           │
│  └──────────┘                                           │
└─────────────────────────────────────────────────────────┘
```

**Networks:**
- `public-net` (73.0.0.0/24) — uses a "public-looking" range because
  go-libp2p's `manet.IsPublicAddr()` filters out private/CGNAT ranges.
  AutoNAT v2 only probes addresses that pass this filter.
- `private-net` (10.0.1.0/24) — standard private range, matching
  real-world deployments.

**Components:**
- **Router** — Alpine container with iptables. Implements all 5 NAT
  types via masquerade + filtering rules (see
  [`router/entrypoint.sh`](../testbed/docker/router/entrypoint.sh) for
  the iptables configuration per NAT type). Also supports `tc netem` for
  latency/packet-loss injection, static port forwarding (DNAT), and
  miniupnpd for UPnP emulation.
- **Servers** (3-7) — go-libp2p nodes running AutoNAT v2 server with
  our probe-lab fork ([OTel instrumentation + UDP black hole fix](https://github.com/probe-lab/go-libp2p/tree/v0.47.0-autonat_otel)).
  Write multiaddrs to a shared Docker volume for client discovery.
- **Client** — go-libp2p (primary), rust-libp2p, or js-libp2p node
  behind the router. Reads server addresses from shared volume.
  Exports OTel spans to Jaeger.
- **Jaeger** — OTel trace collector on both networks. `run.py` queries
  Jaeger API for convergence detection and trace export.
- **Orchestrator** — `run.py` reads YAML scenario files, manages Docker
  Compose lifecycle, waits for convergence via Jaeger polling, exports
  traces as JSONL for `analyze.py`.

### Scenario Parameters

Experiments are defined in YAML scenario files with the following
configurable parameters:

| Parameter | Values tested | Description |
|-----------|--------------|-------------|
| `nat_type` | none, full-cone, address-restricted, port-restricted, symmetric | NAT filtering/mapping behavior (maps to iptables rules in [`router/entrypoint.sh`](../testbed/docker/router/entrypoint.sh)) |
| `transport` | tcp, quic, both | Client transport protocol |
| `server_count` | 3, 5, 7 | Number of AutoNAT servers |
| `latency_ms` | 10, 200, 500 | One-way added latency via `tc netem` (RTT = 2×) |
| `packet_loss` | 0, 1, 5, 10 (%) | Packet loss via `tc netem` on router |
| `port_forward` | true/false | Static DNAT from router public IP to client |
| `upnp` | true/false | miniupnpd on router for dynamic port mapping |
| `obs_addr_thresh` | 1, 2, 4 | Override observed address activation threshold |
| `unreliable_servers` | 0, 5 | Servers with dial-back blocked (for v1 oscillation) |
| `autonat_refresh` | 0, 30 (s) | **v1 only:** refresh interval override (default 15 min). Shortened to 30s in v1/v2 gap scenarios to observe oscillation within testbed timeouts. |
| `timeout_s` | 120, 600 | Per-scenario timeout |
| `runs` | 1, 20 | Repeated runs for statistical confidence |

### Experiment Matrix

| Scenario file | Scenarios | Runs | What it tests |
|--------------|-----------|------|---------------|
| `matrix.yaml` | 10 | 1 each | Baseline: 5 NATs × 2 transports (server_count=7) |
| `high-latency.yaml` | 16 | 1 each | 4 NATs × 2 transports × {200ms, 500ms} latency |
| `packet-loss.yaml` | 24 | 1 each | 4 NATs × 2 transports × {1%, 5%, 10%} loss |
| `adf-false-positive.yaml` | 6 | 20 each | ADF vs APDF × 3 transports (120 total) |
| `reachable-forwarded.yaml` | 5 | 1 each | Port forwarding toggle detection (600s timeout, 2 phases) |
| `v1-v2-gap.yaml` | 2 | 1 each | 2 reliable + 5 unreliable servers (600s observation) |
| `threshold-sensitivity.yaml` | 6 | 1 each | obs_addr_thresh {1,2,4} × {no-NAT, symmetric} |

**Total: 183 runs** producing OTel traces analyzed by `analyze.py`.
Individual scenarios take 2-10 minutes each (depending on timeout);
the full matrix was run in batches across multiple sessions.

### Metrics Collected

From each run, `analyze.py` extracts:

- **FNR (False Negative Rate)** — was a reachable node incorrectly
  detected as unreachable?
- **FPR (False Positive Rate)** — was an unreachable node incorrectly
  detected as reachable?
- **TTC (Time-to-Confidence)** — time from node start to first
  `reachable_addrs_changed` or `reachability_changed` event with a
  definitive result
- **TTU (Time-to-Update)** — time from port forwarding toggle to
  detection of the change
- **Probe count** — number of `autonatv2.probe` spans per session
- **v1 flips** — number of `reachability_changed` events (oscillation indicator)

### Key Metrics

From 183 testbed runs:

| Metric | Value |
|--------|-------|
| False Negative Rate (non-symmetric) | **0%** |
| False Positive Rate (non-ADF) | **0%** |
| ADF False Positive Rate | **100%** |
| Baseline TTC (TCP) | ~6,000ms |
| Baseline TTC (QUIC) | ~6,000-11,000ms |
| Probes to converge | 3 (= targetConfidence) |
| v1 oscillation rate (5/7 unreliable) | 60% |
| v2 oscillation rate | **0%** |
| TTU: port forward added | ~30s |
| TTU: port forward removed | ~69s |
| UPnP TTC (local, port-restricted NAT) | ~22s (v2) vs ~106s (v1) |

### Transport Resilience Under Packet Loss

Both TCP and QUIC maintain **0% FNR/FPR** under all tested packet loss
conditions — correctness is unaffected. Convergence time increases for
both transports as loss increases, but **neither shows a consistent
advantage over the other**.

Initial single-run data suggested a dramatic QUIC advantage (+1% vs
+147% TTC increase at 10% loss). A follow-up investigation with 3 runs
per scenario across 7 loss levels (2-15%) showed this was a
**statistical artifact from insufficient runs**:

| Loss % | TCP avg (ms) | QUIC avg (ms) | Difference |
|--------|-------------|---------------|------------|
| 2% | 5,010 | 5,010 | None |
| 5% | 8,052 | 9,408 | QUIC slightly slower |
| 10% | 10,014 | 13,757 | QUIC slower |
| 15% | 16,813 | 9,734 | TCP slower |

Convergence times are quantized to ~5s intervals (the probe refresh
cycle). A lost probe retries on the next cycle regardless of transport.
The variance is dominated by **which probe cycle gets hit by loss**,
not by transport-level retransmission differences. With only 3 runs
per scenario, neither transport shows a statistically significant
advantage. Under latency (no loss), the gap is also within noise
(TCP +432% vs QUIC +233% at 500ms, single runs).

See [#87](https://github.com/probe-lab/autonat-project/issues/87)
for full investigation data.

### Convergence Heatmaps

![Convergence Heatmap TCP](../results/figures/08_convergence_heatmap_tcp.png)
*Figure 3: Convergence time heatmap (TCP) across NAT types and network conditions.*

![Convergence Heatmap QUIC](../results/figures/08_convergence_heatmap_quic.png)
*Figure 4: Convergence time heatmap (QUIC) — more resilient to degradation than TCP.*

For complete per-scenario data and additional figures, see
[measurement-results.md](measurement-results.md).

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
