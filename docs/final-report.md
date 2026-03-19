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
2. [Glossary](#glossary)
3. [Background](#background)
4. [Testbed](#testbed)
5. [Findings](#findings)
6. [Key Metrics](#key-metrics)
7. [Cross-Implementation Comparison](#cross-implementation-comparison)
8. [Recommendations](#recommendations)
9. [Future Work](#future-work)
10. [References](#references)

---

## Executive Summary

AutoNAT v2 is a significant improvement over v1 in per-address
reachability detection. In controlled testbed conditions, it produces
**0% false negative rate and 0% false positive rate** across all
non-edge-case NAT types, converges in ~6 seconds, and is resilient to
high latency and packet loss (QUIC adds only +1% convergence time at 10%
packet loss vs TCP's +147%).

However, we identified **10 findings** that affect its real-world
effectiveness — ranging from protocol-level design issues to
implementation gaps and cross-implementation inconsistencies.

The most impactful finding is that **v2's results are not consumed by the
systems that matter most** (DHT, AutoRelay) in go-libp2p, the only
implementation where v2 is deployed in production. v1 still controls the
global reachability flag, and v1 oscillates under real-world conditions
(3 out of 5 testbed runs with unreliable servers show v1 flipping between
Public and Private while v2 remains stable).

Cross-implementation analysis reveals that **only go-libp2p has a
functional AutoNAT v2 deployment**. rust-libp2p has a critical address
selection bug (probes ephemeral ports, 100% false negative). js-libp2p
emits no reachability events. Neither has a production consumer
(Substrate skips autonat entirely; Helia uses v1 only).

### Findings at a Glance

| # | Finding | Category | Severity |
|---|---------|----------|----------|
| 1 | [ADF false positive (100% FPR)](#finding-1-address-restricted-nat-false-positive) | Protocol | Medium |
| 2 | [Symmetric NAT silent failure](#finding-2-symmetric-nat-silent-failure) | Protocol | Medium |
| 3 | [v1/v2 reachability gap](#finding-3-v1v2-reachability-gap) | go-libp2p | High |
| 4 | [v1 oscillation → DHT oscillation](#finding-4-v1-oscillation--dht-oscillation) | go-libp2p | High |
| 5 | [UDP black hole blocks QUIC dial-back](#finding-5-udp-black-hole-blocks-quic-dial-back) | go-libp2p | Medium |
| 6 | [Rust: ephemeral port probing](#finding-6-rust-libp2p-ephemeral-port-probing) | Cross-impl | High |
| 7 | [JS: no reachability events](#finding-7-js-libp2p-no-reachability-events) | Cross-impl | Medium |
| 8 | [No v2 production deployment outside Kubo](#finding-8-no-production-deployment-outside-kubo) | Cross-impl | Info |
| 9 | [QUIC resilience to packet loss](#finding-9-quic-resilience-to-packet-loss) | Performance | Info |
| 10 | [Threshold sensitivity and symmetric NAT fix](#finding-10-observed-address-threshold-and-symmetric-nat) | Performance | Info |

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

---

## Background

For full background on NAT types, mapping/filtering behaviors, and
how they affect AutoNAT v2 dial-back, see [autonat-v2.md](autonat-v2.md).

### How NAT Filtering Affects AutoNAT v2 Dial-Back

When the server's `dialerHost` dials back to the client, the NAT's
filtering decision determines whether the connection reaches the client:

```
Client behind NAT contacted Server at 1.2.3.4:5000
NAT mapping: client:4001 → 203.0.113.1:50000

Server's dialerHost dials back from 1.2.3.4:random_port to 203.0.113.1:50000

Full-cone (EIF):       "Any source allowed"                → PASS
Addr-restricted (ADF): "Is 1.2.3.4 trusted? YES"          → PASS ← Finding #1
Port-restricted (APDF):"Is 1.2.3.4:random trusted? NO"    → BLOCK (correct)
Symmetric (APDF):      N/A — v2 never reaches this stage   ← Finding #2
```

### AutoNAT v2 vs v1

| Aspect | v1 | v2 |
|--------|----|----|
| Scope | Global (whole-node) | Per-address |
| Probing | Random peer, majority vote | Specific server, per-address confidence |
| Confidence | Sliding window of 3 | Sliding window of 5, targetConfidence=3 |
| Nonce verification | No | Yes |
| Amplification protection | No | Yes (30-100KB) |
| Event (go-libp2p) | `EvtLocalReachabilityChanged` | `EvtHostReachableAddrsChanged` |
| DHT consumes | **Yes** | **No** |

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

Docker-based lab with configurable NAT types via iptables. For full
architecture details, see [testbed.md](testbed.md).

**Networks:**
- `public-net` (73.0.0.0/24) — servers and router public side
- `private-net` (10.0.1.0/24) — client and router private side

**Components:**
- Router with configurable NAT (none, full-cone, address-restricted,
  port-restricted, symmetric), latency/packet-loss injection, port
  forwarding, UPnP
- 3-7 go-libp2p AutoNAT servers
- Client nodes: go-libp2p (primary), rust-libp2p, js-libp2p
- Jaeger for OTel trace collection
- Python orchestrator (`run.py`) with YAML scenario definitions

**Traces collected:** 178 runs total
- Full matrix: 10 (5 NATs × 2 transports)
- High latency: 16 (4 NATs × 2 transports × 2 latencies)
- Packet loss: 24 (4 NATs × 2 transports × 3 loss rates)
- ADF false positive: 120 (2 NATs × 3 transports × 20 runs)
- Threshold sensitivity: 6
- Time-to-update toggles: 5 (with 2 phases each)
- v1/v2 gap: 5 (with 600s observation windows)

---

## Findings

### Finding 1: Address-Restricted NAT False Positive

**Category:** Protocol design | **Severity:** Medium

AutoNAT v2 reports 100% false positive rate for nodes behind
address-restricted NAT (EIM + ADF). The dial-back succeeds because the
server's IP is already in the NAT's "allowed" list from the client's
initial connection.

**Testbed evidence:** 120 runs across TCP, QUIC, and both transports.

| NAT type | Runs | Reported reachable | FPR |
|----------|------|-------------------|-----|
| Address-restricted (ADF) | 60 | 60/60 | **100%** |
| Port-restricted (APDF) | 60 | 0/60 | **0%** |

The false positive is deterministic, not probabilistic — the protocol
design guarantees this outcome for ADF NATs.

**Real-world impact:** Likely low. ADF is rare in modern routers (most
default to APDF). But no measurement data exists to quantify prevalence.

![Detection Correctness](../results/figures/05_detection_correctness.png)
*Figure 5: Detection correctness heatmap — address-restricted reports reachable (false positive).*

**Full analysis:** [adf-false-positive.md](adf-false-positive.md)

### Finding 2: Symmetric NAT Silent Failure

**Category:** Protocol design | **Severity:** Medium

Under symmetric NAT (ADPM), each outbound connection uses a different
external port. No address reaches the observed address activation
threshold (`ActivationThresh=4`) → AutoNAT v2 never runs → no
reachability signal at all.

**Testbed evidence:** All symmetric NAT scenarios produce zero events:

```
symmetric-tcp-7:     NO SIGNAL
symmetric-quic-7:    NO SIGNAL
symmetric-*-lat*:    NO SIGNAL
symmetric-*-loss*:   NO SIGNAL
```

**Key finding:** This is threshold-caused and fixable. With
`obs_addr_thresh=1`, symmetric NAT nodes receive an UNREACHABLE
determination (correct) instead of silence. The tradeoff is lower
confidence in the observed address.

**Toggle scenarios:** Port forwarding changes are NOT detected for
symmetric NAT (autonat v2 never runs, so it can't detect changes).

![Time-to-Update](../results/figures/06_time_to_update.png)
*Figure 6: Time-to-update timeline — 30s to detect added port forward, 69s to detect removal.*

### Finding 3: v1/v2 Reachability Gap

**Category:** go-libp2p | **Severity:** High

v1 and v2 produce independent, incompatible reachability signals. All
go-libp2p subsystems that react to reachability consume v1 only:

| Consumer | Event consumed | v2 aware? |
|----------|---------------|-----------|
| Kademlia DHT | `EvtLocalReachabilityChanged` (v1) | **No** |
| AutoRelay | `EvtLocalReachabilityChanged` (v1) | **No** |
| Address Manager | `EvtLocalReachabilityChanged` (v1) | **No** |
| NAT Service | `EvtLocalReachabilityChanged` (v1) | **No** |

A node can have v2-confirmed reachable addresses while v1 simultaneously
reports Private — triggering unnecessary relay usage and DHT client mode.

**Source references:**
- DHT subscribes to v1: [subscriber_notifee.go#L30](https://github.com/libp2p/go-libp2p-kad-dht/blob/master/subscriber_notifee.go#L30)
- `EvtHostReachableAddrsChanged` (v2) does NOT appear in go-libp2p-kad-dht

**Full analysis:** [v1-v2-reachability-gap.md](v1-v2-reachability-gap.md)

### Finding 4: v1 Oscillation → DHT Oscillation

**Category:** go-libp2p | **Severity:** High

v1 uses random peer selection and a sliding window of 3. A single failed
dial-back from an unreliable peer can flip Public→Private.

**Testbed evidence** (full-cone NAT, 2 reliable + 5 unreliable servers):

Best trace (`v1v2-gap-fullcone-tcp`, run 2):
```
  3,026ms   v1  PUBLIC
  6,018ms   v2  reachable=["/ip4/73.0.0.2/tcp/4001"]  ← stable
108,027ms   v1  PRIVATE  ← flipped!
183,027ms   v1  PUBLIC   ← flipped back!
```

v2 reached reachable at 6s and **never changed**. v1 oscillated.

![v1/v2 Gap Comparison](../results/figures/10_v1_v2_gap_comparison.png)
*Figure 10: v1 oscillates (red segments) while v2 stays stable (green). Three unreliable server ratios.*

| Metric | v1 | v2 |
|--------|----|----|
| Oscillation rate (5/7 unreliable) | 60% of runs | **0%** |
| Stability after convergence | Flips on random peer failure | Stable (targetConfidence=3) |

**Full analysis:** [v1-vs-v2-performance.md](v1-vs-v2-performance.md)

### Finding 5: UDP Black Hole Detector Blocks QUIC Dial-Back

**Category:** go-libp2p | **Severity:** Medium

The AutoNAT v2 `dialerHost` shares the main host's
`UDPBlackHoleSuccessCounter`. On fresh servers with zero UDP history,
the counter enters Blocked state → QUIC dial-backs refused → false
negative for QUIC addresses.

This produces a **false negative**, not just "unknown" — the server
actively reports the address as unreachable.

**Workaround:** Disable detector on dialerHost (matching the v1 fix
from [PR #2529](https://github.com/libp2p/go-libp2p/pull/2529)).

**Source:** `dialerHost` shares counter at [config.go#L240](https://github.com/libp2p/go-libp2p/blob/master/config/config.go#L240). v1 fix disables it at [config.go#L712](https://github.com/libp2p/go-libp2p/blob/master/config/config.go#L712).

**Full analysis:** [udp-black-hole-detector.md](udp-black-hole-detector.md)

### Finding 6: rust-libp2p Ephemeral Port Probing

**Category:** Cross-implementation | **Severity:** High

The rust-libp2p autonat v2 client probes observed connection addresses
(ephemeral source ports from identify) instead of listen addresses. Every
probe targets a port nothing listens on → 100% false negative rate.

**Root cause:** rust-libp2p has no equivalent of go-libp2p's
`ObservedAddrManager` ([manager.go#L24](https://github.com/libp2p/go-libp2p/blob/master/p2p/host/observedaddrs/manager.go#L24),
`ActivationThresh=4`) that consolidates observed addresses by listen port.

**DHT impact:** The DHT uses `ExternalAddrConfirmed` for mode switching
([behaviour.rs#L1169](https://github.com/libp2p/rust-libp2p/blob/master/protocols/kad/src/behaviour.rs#L1169)).
Since no address is ever confirmed, the DHT stays in client mode
permanently.

**Production status:** Substrate/Polkadot does not enable autonat at all.

**Full analysis:** [rust-libp2p-autonat-implementation.md](rust-libp2p-autonat-implementation.md)

### Finding 7: js-libp2p No Reachability Events

**Category:** Cross-implementation | **Severity:** Medium

The `@libp2p/autonat-v2` package emits no events to external consumers.
The DHT receives reachability signals indirectly through:

```
autonat → confirmObservedAddr() → peerStore.patch() → self:peer:update → DHT
```

This works but is imprecise — the DHT checks for public addresses in the
address list, not autonat v2's actual probe results.

**Oscillation resistance:** js-libp2p v1 uses monotonic counters (4
successes to confirm, 8 failures to unconfirm) with TTL-based
re-evaluation — significantly more oscillation-resistant than go-libp2p
v1's sliding window. See [js-libp2p analysis](js-libp2p-autonat-implementation.md#confidence-system).

**Production status:** Helia uses `@libp2p/autonat` v1 only, not v2.

**Full analysis:** [js-libp2p-autonat-implementation.md](js-libp2p-autonat-implementation.md)

### Finding 8: No Production Deployment Outside Kubo

**Category:** Cross-implementation | **Severity:** Info

| Project | Language | AutoNAT version | Source |
|---------|----------|----------------|--------|
| **Kubo** | Go | v1 + v2 (both) | [nat.go#L29](https://github.com/ipfs/kubo/blob/master/core/node/libp2p/nat.go#L29) |
| **Helia** | JS | **v1 only** | [package.json](https://github.com/ipfs/helia/blob/main/packages/helia/package.json) (`@libp2p/autonat ^3.0.5`) |
| **Substrate** | Rust | **None** | Cargo.toml: no `autonat` feature |

AutoNAT v2 exists in three implementations but is battle-tested in only
one. The issues found in rust (#6) and js (#7) may not have been caught
because nobody runs them in production.

### Finding 9: QUIC Resilience to Packet Loss

**Category:** Performance | **Severity:** Info (positive finding)

| Condition | TCP TTC increase | QUIC TTC increase |
|-----------|-----------------|------------------|
| 1% packet loss | +0% | +0% |
| 5% packet loss | +17% | +4% |
| 10% packet loss | **+147%** | **+1%** |
| 200ms latency | +210% | +110% |
| 500ms latency | +432% | +233% |

QUIC handles retransmission at the transport layer, making AutoNAT v2
significantly more reliable over lossy networks. Both transports maintain
0% FNR/FPR even under degraded conditions — correctness is unaffected,
only convergence time increases.

![Packet Loss Impact](../results/figures/04_packet_loss_impact.png)
*Figure 4: Packet loss impact — QUIC (right) flat lines vs TCP (left) steep increase at 10% loss.*

![Latency Impact](../results/figures/03_latency_impact.png)
*Figure 3: Latency impact — both transports scale linearly, QUIC more resilient.*

### Finding 10: Observed Address Threshold and Symmetric NAT

**Category:** Performance | **Severity:** Info

Testbed verification of `ActivationThresh` behavior:

| Scenario | Result |
|----------|--------|
| thresh=4, 3 servers, no-NAT | Converges (threshold doesn't block public addresses) |
| thresh=2, 3 servers, no-NAT | Converges |
| thresh=1, 3 servers, symmetric | **UNREACHABLE** (correct — v2 finally runs!) |

**Key insight:** The threshold only affects *observed* address promotion
(NATted nodes). No-NAT nodes converge regardless because their listen
address is directly public. Lowering the threshold for symmetric NAT
enables v2 to produce a correct determination instead of silence.

**Time-to-Update** (port forwarding toggle detection):

| NAT type | Add forward | Remove forward |
|----------|------------|----------------|
| Port-restricted (TCP/QUIC/both) | **30s** | **69s** |
| Symmetric | NOT detected | NOT detected |
| Address-restricted | NOT detected (already FP reachable) | NOT detected |

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

![v1 vs v2 Convergence](../results/figures/01_convergence.png)
*Figure 1: v1 vs v2 time to first convergence event by NAT type. Symmetric NAT: v2 produces no event.*

![FNR/FPR Summary](../results/figures/07_fnr_fpr_summary.png)
*Figure 7: False negative and false positive rates across all conditions — 0% for v2 in all standard scenarios.*

![Convergence Heatmap TCP](../results/figures/08_convergence_heatmap_tcp.png)
*Figure 8a: Convergence time heatmap (TCP) across NAT types and network conditions.*

![Convergence Heatmap QUIC](../results/figures/08_convergence_heatmap_quic.png)
*Figure 8b: Convergence time heatmap (QUIC) — more resilient to degradation than TCP.*
| QUIC TTC increase at 10% loss | **+1%** |
| TCP TTC increase at 10% loss | +147% |

---

## Cross-Implementation Comparison

| Feature | go-libp2p | rust-libp2p | js-libp2p |
|---------|-----------|-------------|-----------|
| **Maturity** | Primary (May 2024) | Second (Aug 2024) | Third (June 2025) |
| **Production consumer** | Kubo (tens of thousands) | None (Substrate skips autonat) | None (Helia uses v1) |
| **Confidence system** | Sliding window, targetConfidence=3 | None (single probe) | Fixed thresholds (4/8) |
| **Address filtering** | ObservedAddrManager (threshold=4) | None → ephemeral port bug | Address manager + cuckoo filter |
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

4. **Add observed address consolidation** — Implement equivalent of
   go-libp2p's `ObservedAddrManager`. Without this, autonat v2 is
   non-functional.

### For js-libp2p

5. **Emit reachability events** — Expose autonat v2 probe results to
   consumers.

6. **Upgrade Helia to v2** — v1's monotonic counters are
   oscillation-resistant but v2 provides per-address granularity.

### For the AutoNAT v2 specification

7. **Address the ADF blind spot** — Consider requiring dial-back from a
   different IP (multi-server verification).

8. **Add timeout-based inference for symmetric NAT** — Emit "likely
   unreachable" after N minutes with no address activation.

### For the ecosystem

9. **Measure real-world NAT type distribution** — Deploy monitoring to
   quantify ADF prevalence and v2 adoption. See [Future Work](#future-work).

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
| [udp-black-hole-detector.md](udp-black-hole-detector.md) | QUIC dial-back issue + 5 fix options |
| [go-libp2p-autonat-implementation.md](go-libp2p-autonat-implementation.md) | go-libp2p internals |
| [rust-libp2p-autonat-implementation.md](rust-libp2p-autonat-implementation.md) | rust-libp2p analysis |
| [js-libp2p-autonat-implementation.md](js-libp2p-autonat-implementation.md) | js-libp2p analysis |
| [future-work-nat-monitoring.md](future-work-nat-monitoring.md) | NAT monitoring proposal |
| [analysis-summary.md](../results/testbed/data/analysis-summary.md) | Quantitative metrics |
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
