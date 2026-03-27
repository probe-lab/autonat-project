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
3. [Testbed](#testbed)
4. [Findings](#findings)
5. [Key Metrics](#key-metrics)
6. [Cross-Implementation Comparison](#cross-implementation-comparison)
7. [Recommendations](#recommendations)
8. [Future Work](#future-work)
9. [What v2 Got Right](#what-v2-got-right)
10. [References](#references)
11. [Glossary](#glossary)

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

However, multiple libp2p-based projects have reported that reachability
detection **does not work reliably in production**:

- **Obol Network** ([Charon](https://github.com/ObolNetwork/charon),
  go-libp2p v0.47.0): Distributed validator nodes running behind home
  or corporate NAT experience oscillating reachability status. Their
  Prometheus metric `p2p_reachability_status` flips between public and
  private, triggering unnecessary relay activation and DHT client mode
  — degrading validator coordination through higher-latency relay paths.

- **Avail Network** ([avail-light](https://github.com/availproject/avail-light),
  rust-libp2p v0.55.0): Light clients reported persistent
  "autonat-over-quic libp2p errors" starting from v1.7.4. The team
  ultimately **disabled AutoNAT entirely** in v1.13.2 (September 2025),
  forcing operators to manually set `--external-address` for DHT server
  mode — defeating the purpose of automatic reachability detection.

These are not isolated incidents. They reflect fundamental issues in how
AutoNAT determines and communicates reachability across the libp2p
ecosystem. This project investigates AutoNAT v2 and evaluates whether it succeeds.

See [obol.md](obol.md) and [avail.md](avail.md) for detailed impact
analysis on each project.

### Findings

AutoNAT v2 is a significant improvement over v1 in per-address
reachability detection. In controlled testbed conditions, it produces
**0% false negative rate and 0% false positive rate** across all
non-edge-case NAT types, converges in ~6 seconds, and is resilient to
high latency and packet loss (QUIC adds only +1% convergence time at 10%
packet loss vs TCP's +147%).

However, we identified **7 findings** that affect its real-world
effectiveness — ranging from protocol-level design issues to
implementation gaps and cross-implementation inconsistencies.

The most impactful finding is that **v2's results are not consumed by the
systems that matter most** (DHT, AutoRelay) in go-libp2p, the only
implementation where v2 is deployed in production. v1 still controls the
global reachability flag, and v1 oscillates under real-world conditions
(3 out of 5 testbed runs with unreliable servers show v1 flipping between
Public and Private while v2 remains stable). This directly explains the
oscillation observed by Obol.

Cross-implementation analysis reveals that **only go-libp2p has a
functional AutoNAT v2 deployment**. rust-libp2p works correctly when
properly configured but lacks a safety net when TCP port reuse fails — which,
combined with the QUIC dial-back issue, explains the errors that led
Avail to disable AutoNAT. js-libp2p emits no reachability events.
Neither has a production consumer (Substrate skips autonat entirely;
Helia uses v1 only).

### Findings at a Glance

| # | Finding | Category | Severity |
|---|---------|----------|----------|
| 1 | [v1/v2 reachability gap](#finding-1-v1v2-reachability-gap) | go-libp2p | High |
| 2 | [v1 oscillation → DHT oscillation](#finding-2-v1-oscillation--dht-oscillation) | go-libp2p | High |
| 3 | [ADF false positive (100% FPR)](#finding-3-address-restricted-nat-false-positive) | Protocol | Medium |
| 4 | [Symmetric NAT missing signal](#finding-4-symmetric-nat-missing-signal) | Cross-impl | Medium |
| 5 | [UDP black hole blocks QUIC dial-back](#finding-5-udp-black-hole-blocks-quic-dial-back) | go-libp2p | Medium |
| 6 | [Rust: TCP port reuse safety net](#finding-6-rust-libp2p-tcp-port-reuse-and-address-translation) | Cross-impl | Low |
| 7 | [v2 adoption gap](#finding-7-v2-adoption-gap) | Cross-impl | Info |

---

## Background

### NAT Types

NAT behavior is defined by two independent properties: **mapping**
(how the router assigns external ports) and **filtering** (which
inbound packets are allowed through).

| NAT Type | Mapping | Filtering | Inbound from strangers | Prevalence |
|----------|---------|-----------|----------------------|------------|
| **No NAT** | — | — | Always works | Servers, cloud |
| **Full-cone** | EIM | EIF | Always works | Rare (intentional DMZ/forward) |
| **Address-restricted** | EIM | ADF | Only from previously contacted IPs | Rare in modern routers |
| **Port-restricted** | EIM | APDF | Only from exact previously contacted IP:port | Most common home router default |
| **Symmetric** | ADPM | APDF | Never (different port per destination) | CGNAT, mobile carriers |

For the full mapping/filtering taxonomy (RFC 4787), see
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
filtering decision determines whether the connection reaches the client:

```
Client behind NAT contacted Server at 1.2.3.4:5000
NAT mapping: client:4001 → 203.0.113.1:50000

Server's dialerHost dials back from 1.2.3.4:random_port to 203.0.113.1:50000

Full-cone (EIF):       "Any source allowed"                → PASS
Addr-restricted (ADF): "Is 1.2.3.4 trusted? YES"          → PASS ← Finding #4
Port-restricted (APDF):"Is 1.2.3.4:random trusted? NO"    → BLOCK (correct)
Symmetric (APDF):      N/A — v2 never reaches this stage   ← Finding #5
```

### AutoNAT v1 vs v2

| Aspect | v1 | v2 |
|--------|----|----|
| **Protocol** | `/libp2p/autonat/1.0.0` | `/libp2p/autonat/2/dial-request` + `dial-back` |
| **Scope** | Global (whole-node: Public/Private) | Per-address (each address independently) |
| **Probing** | Random peer, majority vote | Specific server selection, per-address confidence |
| **Confidence** | Sliding window of 3 | Sliding window of 5, targetConfidence=3 |
| **Nonce verification** | No | Yes (prevents spoofing) |
| **Amplification protection** | No | Yes (30-100KB data when IP differs) |
| **Dial-back identity** | Same peer ID | Separate peer ID (go-libp2p) |
| **Event (go-libp2p)** | `EvtLocalReachabilityChanged` | `EvtHostReachableAddrsChanged` |
| **DHT consumes** | **Yes** | **No** (Finding #1) |
| **Spec** | Informal, no RFC | [specs/autonat/autonat-v2.md](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md) |

### Scope of This Study

This report evaluates AutoNAT v2's correctness, performance, and
integration across three libp2p implementations (go, rust, js). It
does NOT evaluate:

- Hole punching success rates (DCUtR) — see Trautwein et al. 2022/2025
- Relay performance (Circuit Relay v2)
- DHT performance itself (routing, lookup latency)
- AutoNAT v1 in isolation (only v1/v2 comparison)

### NAT Traversal: libp2p vs Traditional

| Step | Traditional (STUN/ICE) | libp2p |
|------|----------------------|--------|
| Discover external address | STUN binding request | Identify protocol (ObservedAddr) |
| Test reachability | STUN from **multiple IPs** (RFC 5780) | AutoNAT from **same IP** |
| Direct connection | ICE candidate exchange | DCUtR via relay |
| Fallback relay | TURN server | Circuit Relay v2 |

The key difference at step 2: STUN tests from multiple IPs, which
distinguishes full-cone from address-restricted. AutoNAT v2 tests from
the same IP the client already contacted, making these indistinguishable.

---

## Testbed

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
  types via masquerade + filtering rules. Also supports `tc netem` for
  latency/packet-loss injection, static port forwarding (DNAT), and
  miniupnpd for UPnP emulation.
- **Servers** (3-7) — go-libp2p nodes running AutoNAT v2 server with
  our probe-lab fork (OTel instrumentation + UDP black hole fix).
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
| `nat_type` | none, full-cone, address-restricted, port-restricted, symmetric | NAT filtering/mapping behavior |
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

**Total: 178 runs** producing OTel traces analyzed by `analyze.py`.

### Metrics Collected

From each run, `analyze.py` extracts:

- **FNR** — was a reachable node detected as reachable?
- **FPR** — was an unreachable node incorrectly detected as reachable?
- **TTC** — time from node start to first `reachable_addrs_changed` or
  `reachability_changed` event with a definitive result
- **TTU** — time from port forwarding toggle to detection of the change
- **Probe count** — number of `autonatv2.probe` spans per session
- **v1 flips** — number of `reachability_changed` events (oscillation indicator)

---

## Findings

### Finding 1: v1/v2 Reachability Gap

**Category:** go-libp2p | **Severity:** High

**Problem:** v2's per-address reachability results are ignored by every
go-libp2p subsystem that matters. DHT, AutoRelay, Address Manager, and
NAT Service all consume v1's global flag (`EvtLocalReachabilityChanged`)
and are blind to v2's per-address signal (`EvtHostReachableAddrsChanged`).

**Impact:** Any go-libp2p application where the node is reachable
experiences a window where the DHT treats it as private — forcing
traffic through relays, refusing DHT server queries, and delaying
direct connections from other peers. The gap persists until v1
independently confirms reachability, which can take significantly
longer than v2. This affects every go-libp2p deployment relying on
DHT or relay decisions: validator networks see higher-latency relay
paths during startup, IPFS nodes delay DHT participation and waste
relay reservations, and any application consuming the global
reachability flag gets a stale answer. rust-libp2p and js-libp2p are
not affected — their DHT consumes v2-level signals directly.

**Solution:** Bridge v2 into v1's global flag with a reduction function:
"PUBLIC if any v2-confirmed address is reachable." This makes all
existing consumers benefit from v2 without changing their code.

| Consumer | Event consumed | v2 aware? |
|----------|---------------|-----------|
| Kademlia DHT | `EvtLocalReachabilityChanged` (v1) | **No** |
| AutoRelay | `EvtLocalReachabilityChanged` (v1) | **No** |
| Address Manager | `EvtLocalReachabilityChanged` (v1) | **No** |
| NAT Service | `EvtLocalReachabilityChanged` (v1) | **No** |

**Cross-implementation:** Only go-libp2p affected. rust-libp2p's DHT uses
`ExternalAddrConfirmed` (v2 path); js-libp2p uses address-level events.

**Full analysis:** [v1-v2-reachability-gap.md](v1-v2-reachability-gap.md)

### Finding 2: v1 Oscillation → DHT Oscillation

**Category:** go-libp2p | **Severity:** High

**Problem:** v1 uses random peer selection and a sliding window of 3. A
single failed dial-back from an unreliable peer flips Public→Private,
causing DHT mode switches and relay churn.

**Impact:** In decentralized networks where peers join and leave freely,
a fraction of AutoNAT servers will be unreliable (behind NAT themselves,
overloaded, or temporarily unreachable). Each failed dial-back from
such a server can flip a node's reachability from Public to Private,
triggering a DHT mode switch (server→client) and relay reservation
churn. Applications experience intermittent routing degradation: DHT
queries fail when the node drops to client mode, direct connections are
replaced by higher-latency relay paths, and the cycle repeats as
reachability flips back. Only go-libp2p is affected — js-libp2p's v1
uses monotonic counters with TTL that resist oscillation, and
rust-libp2p doesn't consume v1.

**Solution:** Suppress v1 probing once v2 reaches targetConfidence.
v2's explicit server selection and per-address confidence system
eliminate the random-peer problem entirely.

![v1/v2 Gap Comparison](../results/figures/10_v1_v2_gap_comparison.png)
*Figure 1: v1 oscillates (red segments) while v2 stays stable (green). Three unreliable server ratios.*

| Metric | v1 | v2 |
|--------|----|----|
| Oscillation rate (5/7 unreliable) | 60% of runs | **0%** |
| Stability after convergence | Flips on random peer failure | Stable (targetConfidence=3) |

**Cross-implementation:** go-libp2p affected. js-libp2p's v1 is
mitigated (monotonic counters + TTL). rust-libp2p doesn't consume v1.

**Full analysis:** [v1-vs-v2-performance.md](v1-vs-v2-performance.md)

### Finding 3: Address-Restricted NAT False Positive

**Category:** Protocol design | **Severity:** Medium

**Problem:** AutoNAT v2 produces 100% false positive rate for nodes
behind address-restricted NAT (EIM + ADF). The dial-back comes from the
same IP the client already contacted, so the NAT allows it through —
making the node appear reachable when it isn't.

**Impact:** Nodes behind ADF NAT advertise unreachable addresses. Peers
attempting direct connections fail, adding latency before relay fallback.
Real-world impact is likely low (ADF is rare in modern routers, most
default to APDF), but no measurement data exists to quantify prevalence.
This is a protocol-level issue — all implementations are affected
identically.

**Solution:** Require dial-back from a different IP than the one the
client contacted (multi-server verification). This would distinguish ADF
from full-cone NAT. Alternatively, document the limitation in the spec.

**Testbed evidence:** 120 runs — deterministic, not probabilistic.

| NAT type | Runs | Reported reachable | FPR |
|----------|------|-------------------|-----|
| Address-restricted (ADF) | 60 | 60/60 | **100%** |
| Port-restricted (APDF) | 60 | 0/60 | **0%** |

![Detection Correctness](../results/figures/05_detection_correctness.png)
*Figure 2: Detection correctness heatmap — address-restricted reports reachable (false positive).*

**Cross-implementation:** Protocol-level issue — affects all
implementations identically.

**Full analysis:** [adf-false-positive.md](adf-false-positive.md)

### Finding 4: Symmetric NAT Missing Signal

**Category:** Cross-implementation | **Severity:** Medium

**Problem:** Under symmetric NAT (ADPM), each outbound connection uses a
different external port. All three implementations fail to produce a
timely reachability signal, but for different reasons:

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

**Impact:** Nodes behind symmetric NAT are **definitively unreachable**
by definition — CGNAT and mobile carrier NAT do not support UPnP or
port forwarding, so there is no path to inbound connectivity. The
practical outcome (node does not serve DHT queries, does not accept
direct connections) is the same whether the signal is "unknown" or
"unreachable." There is **no false positive** — the system never
incorrectly claims a symmetric NAT node is reachable.

The real impact is operational, not functional:

- **Missing relay activation:** In go-libp2p, AutoRelay activates on
  `Private`, not `Unknown`. Without an explicit UNREACHABLE signal,
  the node may never reserve a relay path — leaving it with no
  connectivity fallback and no opportunity for DCUtR hole punching,
  which depends on relay connections being established first.
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

**Testbed evidence:**

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

**Full analysis:** [symmetric-nat-silent-failure.md](symmetric-nat-silent-failure.md),
[#89](https://github.com/probe-lab/autonat-project/issues/89),
[upnp-nat-detection.md](upnp-nat-detection.md)

### Finding 5: UDP Black Hole Detector Blocks QUIC Dial-Back

**Category:** go-libp2p | **Severity:** Medium

**Problem:** The AutoNAT v2 `dialerHost` shares the main host's
`UDPBlackHoleSuccessCounter`. On fresh servers with zero UDP history,
the counter enters Blocked state → QUIC dial-backs refused → the server
actively reports QUIC addresses as unreachable (false negative).

**Impact:** QUIC addresses are incorrectly reported as unreachable on
new or restarted AutoNAT servers, until sufficient UDP traffic builds
the counter history. Affects every go-libp2p node acting as an AutoNAT
server. rust-libp2p and js-libp2p are not affected — neither implements
a black hole detector.

**Solution:** Disable the UDP black hole detector on `dialerHost`,
matching the existing v1 fix ([PR #2529](https://github.com/libp2p/go-libp2p/pull/2529)).
5 fix options analyzed in [udp-black-hole-detector.md](udp-black-hole-detector.md).

**Cross-implementation:** go-libp2p only. rust-libp2p and js-libp2p
have no black hole detector.

**Full analysis:** [udp-black-hole-detector.md](udp-black-hole-detector.md)

### Finding 6: rust-libp2p TCP Port Reuse Safety Net

**Category:** Cross-implementation | **Severity:** Low

**Problem:** When TCP port reuse fails silently in rust-libp2p, the
connection metadata still says `PortUse::Reuse` despite using an
ephemeral port. The identify protocol skips address translation, causing
AutoNAT v2 to probe the wrong port (100% false negative).

**Impact:** Low in practice — the issue only manifests when TCP listeners
aren't ready before outbound connections start (a startup timing issue).
When properly configured, rust-libp2p v2 produces correct results for
all NAT types, matching go-libp2p.

**Cross-implementation:** go-libp2p is unaffected (its `ObservedAddrManager`
corrects ports independently). rust-libp2p has no equivalent safety net.

**Full analysis:** [rust-libp2p-autonat-implementation.md](rust-libp2p-autonat-implementation.md)

### Finding 7: v2 Adoption Gap

**Category:** Cross-implementation | **Severity:** Info

AutoNAT v2 exists in all three libp2p implementations but **only Kubo
deploys it in production**. Across the broader ecosystem (~25 projects
using libp2p), only Kubo and Pactus use v2. Most projects either use
v1, use UPnP instead, or skip AutoNAT entirely. See
[libp2p-autonat-ecosystem.md](libp2p-autonat-ecosystem.md) for the full
survey.

| Project | Language | AutoNAT status | v2 functional? |
|---------|----------|---------------|----------------|
| **Kubo** | Go | v1 + v2 (both active) | **Yes** — 0% FNR/FPR |
| **Helia** | JS | v1 only | Untested in production |
| **Substrate** | Rust | Disabled entirely | Works when properly configured (Finding #6) |
| **Avail** | Rust | **Disabled** (v1.13.2) | Broke in production, turned off |

The protocol itself works when the implementation is correct (go-libp2p:
0% FNR/FPR). The cross-implementation issues are in the surrounding
infrastructure (address management, event model), not in the AutoNAT v2
protocol logic.

**Full analysis:**
[rust-libp2p](rust-libp2p-autonat-implementation.md) ·
[js-libp2p](js-libp2p-autonat-implementation.md) ·
[go-libp2p](go-libp2p-autonat-implementation.md)

---

## Key Metrics

From 178 testbed runs:

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

## Cross-Implementation Comparison

| Feature | go-libp2p | rust-libp2p | js-libp2p |
|---------|-----------|-------------|-----------|
| **Maturity** | Primary (May 2024) | Second (Aug 2024) | Third (June 2025) |
| **Production consumer** | Kubo (tens of thousands) | None (Substrate skips autonat) | None (Helia uses v1) |
| **Confidence system** | Sliding window, targetConfidence=3 | None (single probe) | Fixed thresholds (4/8) |
| **Address filtering** | ObservedAddrManager (threshold=4) | Identify translation (when `PortUse::New`) | Address manager + cuckoo filter |
| **Reachability events** | `EvtHostReachableAddrsChanged` | Per-probe `Event` struct | **None** |
| **v2 → DHT wiring** | No (DHT reads v1 only) | Indirect (`ExternalAddrConfirmed`) | Indirect (`self:peer:update`) |
| **Dial-back identity** | Separate dialerHost | Same swarm | Same identity |
| **Rate limiting** | 60 RPM global, 12/peer | Basic concurrent limit | Stream limits only |
| **Black hole detection** | Yes (causes issue #5) | No | No |
| **v1 oscillation resistance** | Low (sliding window) | N/A | High (monotonic counters + TTL) |

---

## Recommendations

### For go-libp2p (highest impact)

1. **Bridge v2 into v1 global flag** — Add a reduction function: "PUBLIC
   if any v2-confirmed address is reachable." This makes DHT, AutoRelay,
   and Address Manager benefit from v2 without changing their code.

2. **Disable black hole detector on dialerHost** — Match the v1 fix
   (PR #2529). [5 options analyzed](udp-black-hole-detector.md#proposed-upstream-fixes).

3. **Deprecate v1 probing when v2 has data** — Suppress v1 once v2
   reaches targetConfidence to prevent oscillation.

### For rust-libp2p

4. **Fix silent `PortUse::Reuse` fallback** — When TCP port reuse
   fails and falls back to an ephemeral port, identify skips address
   translation because the connection is still marked `Reuse`. Either
   identify should check the actual local port, or the TCP transport
   should report the actual outcome. Optionally, add an
   `ObservedAddrManager`-equivalent as a safety net.

### For js-libp2p

5. **Emit reachability events** — Expose autonat v2 probe results to
   consumers.

6. **Upgrade Helia to v2** — v1's monotonic counters are
   oscillation-resistant but v2 provides per-address granularity.

### For the ecosystem

7. **Measure real-world NAT type distribution** — Deploy monitoring to
   quantify ADF prevalence, symmetric NAT fraction, and v2 adoption.
   See [Future Work](#future-work).

> **Note:** Recommendations for the AutoNAT v2 specification (addressing
> ADF blind spot and symmetric NAT missing signal) are under
> investigation — see [#89](https://github.com/probe-lab/autonat-project/issues/89).
> go-libp2p already detects symmetric NAT via `getNATType()` but how to
> act on it (and whether this is a spec or implementation concern)
> requires further analysis.

---

## Future Work

### Tier 1: Query Existing Nebula Data

The [Nebula crawler](https://github.com/probe-lab/nebula) already stores
protocol lists, agent versions, and multiaddresses per peer. SQL queries
on the existing database can provide:

- AutoNAT v2 adoption rate (% of peers supporting `/libp2p/autonat/2/dial-request`)
- go-libp2p version distribution (v0.42.0+ has v2 as primary)
- Platform distribution (Go/Rust/JS inferred from `agent_version`)
- TCP vs QUIC address patterns
- Relay-dependent peer count

**Effort:** Days. **Limitation:** Only sees DHT server-mode nodes.

### Tier 2: Full NAT Classification via ants-watch

Use [ants-watch](https://github.com/probe-lab/ants-watch) to deploy
sybil nodes across the full DHT keyspace on 2-3 VPS with different
public IPs. Peers connect to sybils during normal DHT operations,
capturing the **entire active population** including NATted peers
invisible to crawlers.

Multi-vantage observed port comparison classifies all 4 NAT types:
- **Step 1:** Compare observed ports across vantage points (EIM vs ADPM)
- **Step 2:** Unsolicited dial from uncontacted vantage (EIF vs restricted)
- **Step 3:** Contacted vantage dials from different port (ADF vs APDF)

**Effort:** Weeks-months. **Value:** Definitive answer to "how common is
ADF?" and "what fraction is symmetric?"

**Full proposal:** [future-work-nat-monitoring.md](future-work-nat-monitoring.md)

---

## What v2 Got Right

Despite the issues found, AutoNAT v2 is a substantial improvement:

- **Per-address testing** eliminates v1's "one bad peer ruins everything"
- **Nonce verification** prevents spoofing
- **Amplification protection** (30-100KB) prevents DDoS via protocol abuse
- **Confidence system** (targetConfidence=3) provides stable results
- **0% FNR/FPR** in all non-edge-case scenarios
- **~6s convergence** — fast enough for interactive use
- **UPnP integration** — correctly detects reachability through UPnP-mapped
  ports (both TCP and QUIC), confirmed on a real home router

The protocol design is sound. The issues are in how implementations
integrate v2 into their broader subsystems, and in edge cases (ADF,
symmetric) that the protocol doesn't handle.

---

## References

### Project Documents

| Document | Scope |
|----------|-------|
| [autonat-v2.md](autonat-v2.md) | Protocol walkthrough and NAT type reference |
| [v1-vs-v2-performance.md](v1-vs-v2-performance.md) | v1 vs v2 quantitative comparison |
| [v1-v2-reachability-gap.md](v1-v2-reachability-gap.md) | v1/v2 event model gap analysis |
| [adf-false-positive.md](adf-false-positive.md) | ADF false positive with 120-run evidence |
| [symmetric-nat-silent-failure.md](symmetric-nat-silent-failure.md) | Symmetric NAT cross-implementation root cause analysis |
| [udp-black-hole-detector.md](udp-black-hole-detector.md) | QUIC dial-back issue + 5 fix options |
| [go-libp2p-autonat-implementation.md](go-libp2p-autonat-implementation.md) | go-libp2p internals |
| [rust-libp2p-autonat-implementation.md](rust-libp2p-autonat-implementation.md) | rust-libp2p analysis |
| [js-libp2p-autonat-implementation.md](js-libp2p-autonat-implementation.md) | js-libp2p analysis |
| [upnp-nat-detection.md](upnp-nat-detection.md) | UPnP interaction with AutoNAT v2 + local test results |
| [future-work-nat-monitoring.md](future-work-nat-monitoring.md) | NAT monitoring proposal |
| [measurement-results.md](measurement-results.md) | Complete testbed results (all 178 runs) |
| [testbed.md](testbed.md) | Testbed architecture |
| [scenario-schema.md](scenario-schema.md) | Scenario format reference |

### External References

- [AutoNAT v2 Specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
- [RFC 4787: NAT Behavioral Requirements](https://www.rfc-editor.org/rfc/rfc4787) — EIM/ADPM/EIF/ADF/APDF taxonomy
- [RFC 5780: NAT Behavior Discovery Using STUN](https://www.rfc-editor.org/rfc/rfc5780)
- [Trautwein et al., "Decentralized Hole Punching" (DINPS 2022)](https://research.protocol.ai/publications/decentralized-hole-punching/)
- [Trautwein et al., "Challenging Tribal Knowledge" (2025)](https://arxiv.org/html/2510.27500v1) — 4.4M+ traversal attempts
- [go-libp2p](https://github.com/libp2p/go-libp2p) · [rust-libp2p](https://github.com/libp2p/rust-libp2p) · [js-libp2p](https://github.com/libp2p/js-libp2p)
- [Kubo](https://github.com/ipfs/kubo) · [Helia](https://github.com/ipfs/helia) · [Substrate](https://github.com/nickcen/polkadot-sdk)
- [Nebula crawler](https://github.com/probe-lab/nebula) · [ants-watch](https://github.com/probe-lab/ants-watch)

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
