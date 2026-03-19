# AutoNAT v2: Conclusions

**Date:** 2026-03-19
**GitHub Issue:** [#60](https://github.com/probe-lab/autonat-project/issues/60)

---

## Executive Summary

AutoNAT v2 is a significant improvement over v1 in per-address
reachability detection. In controlled testbed conditions, it produces
**0% false negative rate and 0% false positive rate** across all
non-symmetric NAT types, converges in ~6 seconds, and is resilient to
high latency and packet loss. However, we identified 10 findings that
affect its real-world effectiveness — ranging from protocol-level design
issues to implementation gaps and cross-implementation inconsistencies.

The most impactful finding is that **v2's results are not consumed by
the systems that matter most** (DHT, AutoRelay) in go-libp2p, the only
implementation where v2 is deployed in production. v1 still controls the
global reachability flag, and v1 oscillates.

---

## Findings Summary

### Protocol-Level Issues

These affect any compliant AutoNAT v2 implementation.

**1. Address-Restricted NAT (ADF) False Positive**
AutoNAT v2 reports 100% false positive rate for nodes behind
address-restricted NAT. The dial-back succeeds because the server's IP
is already in the NAT's allowed list. The protocol cannot distinguish
ADF from full-cone NAT. Real-world impact is likely low (ADF is rare in
modern routers) but unquantified. [Full analysis](adf-false-positive.md)

**2. Symmetric NAT Silent Failure**
Under symmetric NAT, no address reaches the observed address activation
threshold → AutoNAT v2 never runs → the node receives no reachability
signal at all. The node can't distinguish "haven't been probed yet" from
"behind symmetric NAT." [Analysis](../results/testbed/data/analysis-summary.md)

### go-libp2p Implementation Issues

These are specific to go-libp2p, the only implementation with production
deployment.

**3. v1/v2 Reachability Gap**
v1 and v2 produce independent, incompatible reachability signals. v2
emits per-address reachability (`EvtHostReachableAddrsChanged`) but no
subsystem except the address manager consumes it. DHT, AutoRelay, and
NAT service all consume v1's global flag (`EvtLocalReachabilityChanged`).
A node can have v2-confirmed reachable addresses while v1 simultaneously
reports Private. [Full analysis](v1-v2-reachability-gap.md)

**4. v1 Oscillation → DHT Oscillation**
v1 uses random peer selection and a sliding window of 3. With unreliable
peers (common on the IPFS DHT), a single failed dial-back can flip the
node from Public to Private. This directly causes DHT server↔client
oscillation and routing table churn. Our testbed shows 3/5 runs
oscillate while v2 stays stable. [Full analysis](v1-vs-v2-performance.md)

**5. UDP Black Hole Detector Blocks QUIC Dial-Back**
The AutoNAT v2 `dialerHost` shares the main host's UDP black hole
detector counter. On fresh servers with zero UDP history, the counter
enters Blocked state → QUIC dial-backs refused → false negative for QUIC
addresses. Workaround: disable detector on dialerHost (matching v1 fix).
[Full analysis](udp-black-hole-detector.md)

### Cross-Implementation Issues

**6. rust-libp2p: Ephemeral Port Probing**
The autonat v2 client probes observed connection addresses (ephemeral
source ports) instead of listen addresses. Without observed address
consolidation (like go-libp2p's `ObservedAddrManager`), every probe
targets a port nothing listens on → 100% false negative. The DHT then
stays in client mode permanently. [Full analysis](rust-libp2p-autonat-implementation.md)

**7. js-libp2p: No Reachability Events**
The `@libp2p/autonat-v2` package emits no events to external consumers.
Reachability state is tracked internally but not observable. The DHT
receives reachability signals indirectly through the address manager
(`self:peer:update`), which works but is imprecise.
[Full analysis](js-libp2p-autonat-implementation.md)

**8. No Production Deployment Outside Kubo**
Substrate/Polkadot (rust-libp2p's primary consumer) does not enable
autonat at all. Helia (js-libp2p's primary consumer) uses v1 only.
AutoNAT v2 is deployed in production only by Kubo (go-libp2p). The
rust and js implementations exist but are untested at scale.
[Evidence](js-libp2p-autonat-implementation.md#dht-and-autonat-interaction)

### Performance Characteristics

**9. QUIC Resilience to Packet Loss**
At 10% packet loss, QUIC convergence time increases by 1% vs TCP's
147%. QUIC handles retransmission at the transport layer, making
AutoNAT v2 significantly more reliable over lossy networks when using
QUIC. [Data](../results/testbed/data/analysis-summary.md)

**10. Observed Address Threshold and Symmetric NAT**
The `ActivationThresh=4` controls observed address promotion. Testbed
verification revealed two key findings:

- **No-NAT nodes are unaffected by the threshold** — even with
  threshold=4 and only 3 servers, no-NAT nodes converge normally
  because their listen address is directly public (no observation
  needed).
- **Symmetric NAT silence is threshold-caused and fixable** — with
  `obs_addr_thresh=1`, symmetric NAT nodes receive an UNREACHABLE
  determination (correct) instead of silence. The threshold prevents
  any observed address from activating because each server sees a
  different port. Lowering the threshold trades confidence for coverage.

---

## Key Metrics

From 52 analyzed testbed traces (full-matrix, high-latency, packet-loss):

| Metric | Value |
|--------|-------|
| False Negative Rate (non-symmetric) | **0%** |
| False Positive Rate (non-ADF) | **0%** |
| ADF False Positive Rate | **100%** (protocol design issue) |
| Baseline TTC (TCP) | ~6,000ms |
| Baseline TTC (QUIC) | ~6,000-11,000ms |
| Probes to converge | 3 (matches targetConfidence) |
| v1 oscillation rate (5/7 unreliable) | 60% of runs |
| v2 oscillation rate | 0% |
| TTU: port forward added | ~30s |
| TTU: port forward removed | ~69s |

---

## Recommendations

### For go-libp2p (highest impact)

1. **Bridge v2 into v1 global flag** — Add a reduction function: "PUBLIC
   if any v2-confirmed address is reachable, PRIVATE if all unreachable."
   This makes DHT, AutoRelay, and Address Manager benefit from v2 without
   changing their code. Immediate impact for Kubo users.

2. **Disable black hole detector on dialerHost** — Match the v1 fix
   (PR #2529). The dialerHost should attempt every requested dial
   regardless of UDP history. [5 options analyzed](udp-black-hole-detector.md#proposed-upstream-fixes)

3. **Deprecate v1 probing when v2 has data** — Once v2 reaches
   targetConfidence on any address, suppress v1 probing to prevent
   oscillation from overriding v2's stable determination.

### For rust-libp2p

4. **Add observed address consolidation** — Implement an equivalent of
   go-libp2p's `ObservedAddrManager` that groups observations by listen
   port and requires N consistent observations before promoting an
   address. Without this, autonat v2 is non-functional.

### For js-libp2p

5. **Emit reachability events** — Add an event (or extend
   `self:peer:update`) that exposes autonat v2 probe results to
   consumers. Currently, reachability state is internal-only.

6. **Upgrade Helia to v2** — Helia still uses `@libp2p/autonat` (v1).
   js-libp2p v1's monotonic counter design is more oscillation-resistant
   than go-libp2p v1, but v2 provides per-address granularity and nonce
   verification that v1 lacks.

### For the AutoNAT v2 specification

7. **Address the ADF blind spot** — Document that the protocol cannot
   distinguish ADF from full-cone NAT. Consider requiring dial-back
   from a different IP than the one the client connected to (multi-server
   verification).

8. **Add timeout-based inference for symmetric NAT** — If a node has
   been connected to N v2-capable peers for M minutes with no external
   addresses activated, it should receive a "likely behind symmetric NAT"
   signal rather than silence.

### For the ecosystem

9. **Measure real-world NAT type distribution** — Deploy a NAT
   monitoring service to quantify ADF prevalence, symmetric NAT
   fraction, and v2 adoption rate. Without this data, we can't assess
   real-world impact. [Proposal](future-work-nat-monitoring.md)

---

## What v2 Got Right

Despite the issues found, AutoNAT v2 is a substantial improvement:

- **Per-address testing** eliminates v1's "one bad peer ruins everything"
  problem
- **Nonce verification** prevents spoofing
- **Amplification protection** (30-100KB data transfer) prevents DDoS
  via protocol abuse
- **Confidence system** (targetConfidence=3, sliding window of 5)
  provides stable results
- **Primary/secondary address grouping** reduces probe load
- **0% FNR/FPR** in all tested non-edge-case scenarios
- **~6s convergence** — fast enough for interactive use

The protocol design is sound. The issues are in how implementations
integrate v2 results into their broader subsystems, and in edge cases
(ADF, symmetric) that the protocol explicitly doesn't handle.

---

## Open Questions

1. **How common is ADF NAT?** If <5% of nodes, the false positive is
   a known edge case. If >20%, the protocol needs a fix. No measurement
   data exists.

2. **Should v1 be deprecated?** v2 is strictly better in every
   measurable dimension. But v1 controls critical subsystems (DHT,
   relay) that haven't been updated to consume v2. Deprecation requires
   the v2→v1 bridge or consumer refactoring first.

3. **Should the spec address symmetric NAT?** The current behavior
   (silence) is correct but unhelpful. A timeout-based "likely
   unreachable" signal would be more useful than no signal.

4. **Are rust-libp2p and js-libp2p autonat v2 production-ready?** Based
   on our analysis: rust has a critical address selection bug; js lacks
   observable events. Neither has a production consumer. Both need work
   before they can match go-libp2p's v2 in functionality.

---

## Document Index

| Document | Scope |
|----------|-------|
| [report.md](report.md) | Original findings report (issues 1-2) |
| [v1-vs-v2-performance.md](v1-vs-v2-performance.md) | v1 vs v2 quantitative comparison |
| [v1-v2-reachability-gap.md](v1-v2-reachability-gap.md) | v1/v2 event model gap analysis |
| [adf-false-positive.md](adf-false-positive.md) | ADF false positive with testbed evidence |
| [udp-black-hole-detector.md](udp-black-hole-detector.md) | QUIC dial-back issue + 5 fix options |
| [go-libp2p-autonat-implementation.md](go-libp2p-autonat-implementation.md) | go-libp2p internals |
| [rust-libp2p-autonat-implementation.md](rust-libp2p-autonat-implementation.md) | rust-libp2p analysis + ephemeral port issue |
| [js-libp2p-autonat-implementation.md](js-libp2p-autonat-implementation.md) | js-libp2p analysis + confidence system |
| [future-work-nat-monitoring.md](future-work-nat-monitoring.md) | NAT monitoring service proposal |
| [analysis-summary.md](../results/testbed/data/analysis-summary.md) | Quantitative metrics from 52 traces |
| [scenario-schema.md](scenario-schema.md) | Testbed scenario format |
| [testbed.md](testbed.md) | Testbed architecture |
