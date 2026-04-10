# Testbed Measurement Results

Complete results from all testbed experiments. For findings and
interpretation, see [final-report.md](final-report.md). For testbed
architecture, see [testbed.md](testbed.md). For the v1/v2 wiring gap
and state-machine analysis, see
[v1-v2-analysis.md](v1-v2-analysis.md).

**Total: 183 runs** across 69 scenarios, 7 scenario files.

---

## 1. Baseline Matrix (10 scenarios, 1 run each)

**Scenario file:** `testbed/scenarios/matrix.yaml`
**Traces:** `results/testbed/full-matrix-20260312T223319Z/`

5 NAT types × 2 transports, 7 servers, no degradation.

| Scenario | NAT Type | Transport | Result | TTC (ms) | FNR | FPR | Probes |
|----------|----------|-----------|--------|----------|-----|-----|--------|
| none-tcp-7 | none | TCP | reachable | 6,022 | 0% | — | 3 |
| none-quic-7 | none | QUIC | reachable | 6,019 | 0% | — | 3 |
| full-cone-tcp-7 | full-cone | TCP | reachable | 6,022 | 0% | — | 3 |
| full-cone-quic-7 | full-cone | QUIC | reachable | 6,021 | 0% | — | 3 |
| addr-restricted-tcp-7 | address-restricted | TCP | reachable | 6,021 | 0% | — | 3 |
| addr-restricted-quic-7 | address-restricted | QUIC | reachable | 6,021 | 0% | — | 3 |
| port-restricted-tcp-7 | port-restricted | TCP | unreachable | 6,013 | — | 0% | 3 |
| port-restricted-quic-7 | port-restricted | QUIC | unreachable | 11,015 | — | 0% | 4 |
| symmetric-tcp-7 | symmetric | TCP | **NO SIGNAL** | — | — | — | 0 |
| symmetric-quic-7 | symmetric | QUIC | **NO SIGNAL** | — | — | — | 0 |

**Observations:**
- 0% FNR and 0% FPR across all non-symmetric NATs
- TTC consistently ~6s for TCP, ~6-11s for QUIC
- Port-restricted QUIC needs extra probe round (11s vs 6s)
- Address-restricted reports reachable — false positive (Finding #4)
- Symmetric produces zero events (Finding #5)

![v1 vs v2 Convergence](../results/figures/01_convergence.png)
*v1 vs v2 time to first convergence event by NAT type.*

![Detection Correctness](../results/figures/05_detection_correctness.png)
*Detection correctness heatmap.*

---

## 2. High Latency (16 scenarios, 1 run each)

**Scenario file:** `testbed/scenarios/high-latency.yaml`
**Traces:** `results/testbed/high-latency-20260313T085635Z/`

4 NAT types × 2 transports × {200ms, 500ms} one-way latency.

| Scenario | NAT | Transport | Latency | TTC (ms) | vs baseline | FNR/FPR |
|----------|-----|-----------|---------|----------|-------------|---------|
| full-cone-tcp-7-lat200 | full-cone | TCP | 200ms | 18,670 | +210% | 0% |
| full-cone-tcp-7-lat500 | full-cone | TCP | 500ms | 32,023 | +432% | 0% |
| full-cone-quic-7-lat200 | full-cone | QUIC | 200ms | 12,617 | +110% | 0% |
| full-cone-quic-7-lat500 | full-cone | QUIC | 500ms | 20,022 | +233% | 0% |
| addr-restricted-tcp-7-lat200 | addr-restricted | TCP | 200ms | 18,421 | +206% | 0% |
| addr-restricted-tcp-7-lat500 | addr-restricted | TCP | 500ms | 32,021 | +432% | 0% |
| addr-restricted-quic-7-lat200 | addr-restricted | QUIC | 200ms | 12,623 | +110% | 0% |
| addr-restricted-quic-7-lat500 | addr-restricted | QUIC | 500ms | 20,020 | +232% | 0% |
| port-restricted-tcp-7-lat200 | port-restricted | TCP | 200ms | 16,874 | +181% | 0% |
| port-restricted-tcp-7-lat500 | port-restricted | TCP | 500ms | 27,519 | +358% | 0% |
| port-restricted-quic-7-lat200 | port-restricted | QUIC | 200ms | 16,417 | +173% | 0% |
| port-restricted-quic-7-lat500 | port-restricted | QUIC | 500ms | 22,015 | +100% | 0% |
| symmetric-tcp-7-lat200 | symmetric | TCP | 200ms | — | — | NO SIGNAL |
| symmetric-tcp-7-lat500 | symmetric | TCP | 500ms | — | — | NO SIGNAL |
| symmetric-quic-7-lat200 | symmetric | QUIC | 200ms | — | — | NO SIGNAL |
| symmetric-quic-7-lat500 | symmetric | QUIC | 500ms | — | — | NO SIGNAL |

**Observations:**
- Correctness unaffected (0% FNR/FPR at all latencies)
- QUIC more latency-resilient: +110% at 200ms vs TCP +210%
- At 500ms (1s RTT), TCP TTC reaches 32s but still within AutoNAT v2's 15s stream timeout
- Symmetric NAT still NO SIGNAL at all latencies

![Latency Impact](../results/figures/03_latency_impact.png)
*Convergence time vs added latency.*

---

## 3. Packet Loss (24 scenarios, 1 run each)

**Scenario file:** `testbed/scenarios/packet-loss.yaml`
**Traces:** `results/testbed/packet-loss-20260313T093822Z/`

4 NAT types × 2 transports × {1%, 5%, 10%} loss.

| Scenario | NAT | Transport | Loss | TTC (ms) | vs baseline | FNR/FPR |
|----------|-----|-----------|------|----------|-------------|---------|
| full-cone-tcp-7-loss1 | full-cone | TCP | 1% | 6,018 | +0% | 0% |
| full-cone-tcp-7-loss5 | full-cone | TCP | 5% | 7,036 | +17% | 0% |
| full-cone-tcp-7-loss10 | full-cone | TCP | 10% | 14,877 | +147% | 0% |
| full-cone-quic-7-loss1 | full-cone | QUIC | 1% | 6,025 | +0% | 0% |
| full-cone-quic-7-loss5 | full-cone | QUIC | 5% | 6,245 | +4% | 0% |
| full-cone-quic-7-loss10 | full-cone | QUIC | 10% | 6,089 | +1% | 0% |
| addr-restricted-tcp-7-loss1 | addr-restricted | TCP | 1% | 6,223 | +3% | 0% |
| addr-restricted-tcp-7-loss5 | addr-restricted | TCP | 5% | 6,024 | +0% | 0% |
| addr-restricted-tcp-7-loss10 | addr-restricted | TCP | 10% | 8,501 | +41% | 0% |
| addr-restricted-quic-7-loss1 | addr-restricted | QUIC | 1% | 6,020 | +0% | 0% |
| addr-restricted-quic-7-loss5 | addr-restricted | QUIC | 5% | 6,226 | +3% | 0% |
| addr-restricted-quic-7-loss10 | addr-restricted | QUIC | 10% | 6,225 | +3% | 0% |
| port-restricted-tcp-7-loss1 | port-restricted | TCP | 1% | 6,015 | +0% | 0% |
| port-restricted-tcp-7-loss5 | port-restricted | TCP | 5% | 6,018 | +0% | 0% |
| port-restricted-tcp-7-loss10 | port-restricted | TCP | 10% | 19,430 | +223% | 0% |
| port-restricted-quic-7-loss1 | port-restricted | QUIC | 1% | 11,018 | +0% | 0% |
| port-restricted-quic-7-loss5 | port-restricted | QUIC | 5% | 11,014 | +0% | 0% |
| port-restricted-quic-7-loss10 | port-restricted | QUIC | 10% | 11,088 | +1% | 0% |
| symmetric-tcp-7-loss1 | symmetric | TCP | 1% | — | — | NO SIGNAL |
| symmetric-tcp-7-loss5 | symmetric | TCP | 5% | — | — | NO SIGNAL |
| symmetric-tcp-7-loss10 | symmetric | TCP | 10% | — | — | NO SIGNAL |
| symmetric-quic-7-loss1 | symmetric | QUIC | 1% | — | — | NO SIGNAL |
| symmetric-quic-7-loss5 | symmetric | QUIC | 5% | — | — | NO SIGNAL |
| symmetric-quic-7-loss10 | symmetric | QUIC | 10% | — | — | NO SIGNAL |

**Observations:**
- Correctness unaffected (0% FNR/FPR at all loss rates)
- QUIC dramatically more loss-resilient: +1% at 10% loss vs TCP +147%
- 1% loss has negligible impact on both transports
- Symmetric NAT still NO SIGNAL at all loss rates

**QUIC vs TCP gap — observed but not fully explained.** The +1% vs
+147% difference at 10% loss is much larger than expected from the
transport differences alone. Both TCP and QUIC have retransmission,
but the gap is ~147x under packet loss vs ~2x under latency (432%
vs 233% at 500ms). Possible contributing factors:

- **TCP RTO penalty**: Linux TCP initial retransmission timeout is 1s
  with exponential backoff. A dropped SYN during the 3-way handshake
  adds 1-3s per retry. QUIC implementations typically use shorter
  initial timeouts (100-500ms).
- **Handshake exposure**: TCP dial-back requires a new 3-way handshake
  (SYN, SYN-ACK, ACK = 3 packets through the lossy path). QUIC's
  1-RTT handshake exposes fewer packets to loss.
- **Compound loss probability**: `tc netem` applies on both router
  interfaces (bidirectional). For TCP, both the dial-back SYN and
  SYN-ACK traverse the lossy path. At 10% per-direction, ~19% chance
  at least one handshake packet is lost.
- **Possible testbed artifact**: QUIC connections may partially reuse
  existing UDP flows that receive different treatment from `tc netem`
  than new TCP connections. Packet captures would be needed to verify
  that TCP and QUIC packets are dropped at the same rate.

**Suggested further tests:**
- Capture packets on router during loss scenarios to verify equal drop rates
- Test with loss on one direction only (isolate compound effect)
- Test finer loss increments (2%, 3%, 4%, 7%) to find the TCP inflection point
- Verify QUIC dial-back actually performs a new handshake (not reusing existing connection)

![Packet Loss Impact](../results/figures/04_packet_loss_impact.png)
*Convergence time vs packet loss rate.*

---

## 4. ADF False Positive (6 scenarios, 20 runs each = 120 total)

**Scenario file:** `testbed/scenarios/adf-false-positive.yaml`
**Traces:** `results/testbed/adf-false-positive-{tcp,quic,both}/`

Address-restricted (ADF) vs port-restricted (APDF) × 3 transports.

| Scenario | NAT | Transport | Runs | Reported reachable | FPR |
|----------|-----|-----------|------|-------------------|-----|
| adf-tcp | address-restricted | TCP | 20 | 20/20 | **100%** |
| adf-quic | address-restricted | QUIC | 20 | 20/20 | **100%** |
| adf-both | address-restricted | both | 20 | 20/20 | **100%** |
| apdf-tcp | port-restricted | TCP | 20 | 0/20 | **0%** |
| apdf-quic | port-restricted | QUIC | 20 | 0/20 | **0%** |
| apdf-both | port-restricted | both | 20 | 0/20 | **0%** |

**Observations:**
- ADF false positive is deterministic (100% FPR), not probabilistic
- Consistent across all transports
- Port-restricted control shows 0% FPR (correct behavior)
- See [adf-false-positive.md](adf-false-positive.md) for protocol analysis

---

## 5. Time-to-Update / Toggle Scenarios (5 scenarios, 1 run each)

**Scenario file:** `testbed/scenarios/reachable-forwarded.yaml` (section 3)
**Traces:** `results/testbed/toggle-all/`

Port forwarding added then removed mid-session. 600s timeout per scenario.

| Scenario | NAT | Transport | Add forward | Remove forward |
|----------|-----|-----------|-------------|----------------|
| toggle-port-restricted-tcp | port-restricted | TCP | **30s** | **69s** |
| toggle-port-restricted-quic | port-restricted | QUIC | **30s** | **69s** |
| toggle-port-restricted-both | port-restricted | both | **30s** | **69s** |
| toggle-symmetric-both | symmetric | both | NOT detected (180s) | NOT detected (180s) |
| toggle-address-restricted-both | address-restricted | both | NOT detected (180s) | NOT detected (180s) |

**Observations:**
- Port-restricted: perfectly consistent 30s add / 69s remove across all transports
- Removal takes longer (existing confirmation must expire before re-probing)
- Symmetric: NOT detected — autonat v2 never runs, can't detect changes
- Address-restricted: NOT detected — already falsely reported as reachable

![Time-to-Update](../results/figures/06_time_to_update.png)
*Time-to-update timeline for port-restricted NAT toggle.*

---

## 6. v1/v2 Gap (2 scenarios, 5 runs total)

**Scenario file:** `testbed/scenarios/v1-v2-gap.yaml`
**Traces:** `results/testbed/v1-v2-gap-20260313T{111857,122413,123118,131939}Z/`

Full-cone NAT, 2 reliable + 5 unreliable servers, v1 refresh=30s,
600s observation window.

### Event Timelines

**Run: v1v2-gap-fullcone-tcp (122413Z) — clearest oscillation:**
```
  3,026ms   v1  PUBLIC
  5,010ms   v2  reachable=0 (initial)
  6,018ms   v2  reachable=1 [/ip4/73.0.0.2/tcp/4001]  ← stable
108,027ms   v1  PRIVATE  ← flipped!
183,027ms   v1  PUBLIC   ← flipped back!
```

**Run: v1v2-gap-fullcone-tcp (111857Z):**
```
  5,008ms   v2  reachable=0
 16,025ms   v2  reachable=1 [/ip4/73.0.0.2/tcp/4001]
 18,024ms   v1  PRIVATE  ← v1 starts private (unreliable server hit first)
 95,025ms   v1  PUBLIC   ← takes 95s to reach public
```

**Run: v1v2-gap-fullcone-tcp (131939Z):**
```
  5,014ms   v2  reachable=0
 18,028ms   v1  PRIVATE
185,030ms   v1  PUBLIC   ← 3+ minutes to reach public
```

**Run: v1v2-gap-fullcone-both (111857Z):**
```
  3,033ms   v1  PUBLIC
  5,014ms   v2  reachable=0
  6,028ms   v2  reachable=1 [/ip4/73.0.0.2/udp/4001/quic-v1]
```

**Run: v1v2-gap-fullcone-both (123118Z):**
```
  3,030ms   v1  PUBLIC
  5,012ms   v2  reachable=0
  6,034ms   v2  reachable=1 [/ip4/73.0.0.2/udp/4001/quic-v1]
```

### Summary

| Trace | Scenario | v1 flips | v2 stable? | v1 oscillated? |
|-------|----------|----------|------------|----------------|
| 111857Z | fullcone-both | 1 | Yes (QUIC at 6s) | No |
| 111857Z | fullcone-tcp | 2 | Yes (TCP at 16s) | **Yes** |
| 122413Z | fullcone-tcp | 3 | Yes (TCP at 6s) | **Yes** |
| 123118Z | fullcone-both | 1 | Yes (QUIC at 6s) | No |
| 131939Z | fullcone-tcp | 2 | — | **Yes** |

**v1 oscillation rate: 60%** (3/5 runs). **v2 oscillation rate: 0%.**

TCP-only scenarios oscillate more (all 3 TCP runs oscillate vs 0/2
both-transport runs).

![v1/v2 Gap Comparison](../results/figures/10_v1_v2_gap_comparison.png)
*v1/v2 gap across three unreliable server ratios.*

---

## 7. Threshold Sensitivity (6 scenarios, 1 run each)

**Scenario file:** `testbed/scenarios/threshold-sensitivity.yaml`
**Traces:** `results/testbed/threshold-sensitivity/`

Testing `ActivationThresh` behavior with 3 servers.

| Scenario | Threshold | NAT | Result | Expected |
|----------|-----------|-----|--------|----------|
| thresh4-none-tcp-3 | 4 | none | REACHABLE | Expected no convergence — **surprised** |
| thresh4-none-quic-3 | 4 | none | REACHABLE | Expected no convergence — **surprised** |
| thresh2-none-tcp-3 | 2 | none | REACHABLE | Expected |
| thresh2-none-quic-3 | 2 | none | REACHABLE | Expected |
| thresh1-symmetric-tcp-3 | 1 | symmetric | UNREACHABLE | **Key finding** — v2 runs! |
| thresh1-symmetric-quic-3 | 1 | symmetric | UNREACHABLE | **Key finding** — v2 runs! |

**Observations:**
- No-NAT nodes converge regardless of threshold (listen address is directly public)
- Threshold only affects observed address promotion for NATted nodes
- **Symmetric NAT silence is fixable:** with thresh=1, nodes get correct UNREACHABLE
- Tradeoff: lower threshold = less confidence in observed address stability

---

## 8. Cross-Implementation

**Client implementations tested:** go-libp2p, rust-libp2p, js-libp2p
**Server:** go-libp2p only (3 servers)

### Initial results (before Rust timing fix)

| Client | AutoNAT result | Issue |
|--------|----------------|-------|
| go-libp2p | REACHABLE | Correct |
| rust-libp2p | UNREACHABLE (all TCP) | Ephemeral ports — timing bug |
| js-libp2p | Peer update events | Proxy, not direct autonat |

### Rust client after timing fix (wait for listeners)

**Port reuse enabled (default):**

| NAT Type | Transport | Candidate | Result | Matches go? |
|----------|-----------|-----------|--------|-------------|
| no-NAT | both | tcp/4001, udp/4001 | TCP+QUIC REACHABLE | Yes |
| full-cone | tcp | tcp/4001 | REACHABLE | Yes |
| full-cone | both | udp/4001 | QUIC REACHABLE | Yes |
| addr-restricted | tcp | tcp/4001 | REACHABLE (FP) | Yes (ADF) |
| port-restricted | tcp | tcp/4001 | UNREACHABLE | Yes |
| port-restricted | both | udp/4001 | UNREACHABLE | Yes |
| symmetric | both | udp/random | UNREACHABLE | Yes* |

\* go-libp2p produces NO SIGNAL for symmetric (threshold blocks);
rust-libp2p produces UNREACHABLE (no threshold filtering). Both are
correct — rust is actually more informative.

**Port reuse disabled (`--no-port-reuse` / `PortUse::New`):**

| NAT Type | Transport | Candidate | Result |
|----------|-----------|-----------|--------|
| no-NAT | both | tcp/4001, udp/4001 | TCP+QUIC REACHABLE |

When port reuse is explicitly disabled, identify's `_address_translation`
correctly replaces the ephemeral port with the listen port. AutoNAT v2
produces correct results.

**Conclusion:** After fixing the startup timing, rust-libp2p matches
go-libp2p's correctness across all NAT types. The remaining difference
is the lack of an `ObservedAddrManager` safety net for cases where port
reuse silently fails.

---

## Aggregate Metrics

| Metric | Value | Source |
|--------|-------|--------|
| Total runs | 178 | All scenario files |
| FNR (non-symmetric) | **0%** | Baseline + latency + loss + rust (fixed) |
| FPR (non-ADF) | **0%** | Baseline + latency + loss |
| ADF FPR | **100%** | 120 runs |
| Baseline TTC (TCP) | ~6,000ms | Baseline matrix |
| Baseline TTC (QUIC) | ~6,000-11,000ms | Baseline matrix |
| Probes to converge | 3 | = targetConfidence |
| TTU add forward | ~30s | Toggle scenarios |
| TTU remove forward | ~69s | Toggle scenarios |
| v1 oscillation rate | 60% (3/5) | v1/v2 gap |
| v2 oscillation rate | 0% | v1/v2 gap |
| QUIC TTC increase at 10% loss | +1% | Packet loss |
| TCP TTC increase at 10% loss | +147% | Packet loss |

---

## Convergence Heatmaps

![TCP Heatmap](../results/figures/08_convergence_heatmap_tcp.png)
*Convergence time: NAT type × condition (TCP).*

![QUIC Heatmap](../results/figures/08_convergence_heatmap_quic.png)
*Convergence time: NAT type × condition (QUIC).*

![FNR/FPR Summary](../results/figures/07_fnr_fpr_summary.png)
*False negative and false positive rates across all conditions.*

---

## v1 vs v2 Performance Analysis

This section interprets the v1/v2 gap results (§6 above) and the
convergence data from the baseline, latency, and packet-loss scenarios.

### v1/v2 Stability Comparison

Both v1 and v2 select probe servers from the **same pool** of connected
peers. The difference is in failure handling:

- **v1:** All non-success results (timeouts, resets, refusals) erode
  confidence. 4 consecutive non-success probes flip Public → Unknown.
  The DHT treats Unknown the same as Private (both trigger client mode),
  so **timeouts from honest-but-unreliable servers disrupt the DHT —
  no malicious peers needed.**
- **v2:** Server failures are **discarded entirely**. Only explicit
  `E_DIAL_ERROR` (NAT blocked the dial-back) counts. Server
  unreliability cannot cause state flips.

With 5/7 unreliable servers in the testbed, **60% of v1 runs oscillate;
0% of v2 runs do.**

### Trace Timelines (v1-v2-gap scenarios)

**Run 2 — clearest oscillation (`v1v2-gap-fullcone-tcp`):**

```
  3,026ms   v1  PUBLIC         ← initial v1 determination
  6,018ms   v2  reachable=1    ← v2 confirms /ip4/73.0.0.2/tcp/4001
                                  (stays stable for entire 600s window)
108,027ms   v1  PRIVATE        ← v1 FLIPPED (unreliable server timeout)
183,027ms   v1  PUBLIC         ← v1 flipped back (reliable server selected)
```

**Run 1 — v1 slow convergence:**

```
 16,025ms   v2  reachable=1    ← v2 confirms reachable
 18,024ms   v1  PRIVATE        ← v1 starts PRIVATE (unreliable servers dominate)
 95,025ms   v1  PUBLIC         ← v1 reaches public 77s later
```

**Run 3 — v1 stuck for 3 minutes:**

```
 18,028ms   v1  PRIVATE        ← v1 starts private
185,030ms   v1  PUBLIC         ← v1 takes 3+ minutes to reach public
```

### All v1-v2-gap Runs Summary

| Trace | Scenario | v1 flips | v2 stable? | v1 oscillated? |
|-------|----------|----------|------------|----------------|
| 111857Z | fullcone-both | 1 (→PUBLIC) | Yes (QUIC reachable at 6s) | No (single transition) |
| 111857Z | fullcone-tcp | 2 (→PRIVATE→PUBLIC) | Yes (TCP reachable at 16s) | **Yes** |
| 122413Z | fullcone-tcp | 3 (→PUBLIC→PRIVATE→PUBLIC) | Yes (TCP reachable at 6s) | **Yes** |
| 123118Z | fullcone-both | 1 (→PUBLIC) | Yes (QUIC reachable at 6s) | No (single transition) |
| 131939Z | fullcone-tcp | 2 (→PRIVATE→PUBLIC) | — | **Yes** |

3 out of 5 runs show v1 oscillation. Note: the testbed uses iptables to
block dial-backs on unreliable servers, producing `E_DIAL_ERROR` (not
timeouts). In production, unreliable servers more commonly produce
timeouts (e.g., peers behind their own NAT), which flip to Unknown
rather than Private — but the DHT impact is the same.

### Convergence Speed

**v2 Time-to-Confidence:**

| Condition | TCP | QUIC |
|-----------|-----|------|
| Baseline (no degradation) | ~6,000ms | ~6,000ms |
| 200ms added latency | ~18,600ms (+210%) | ~12,600ms (+110%) |
| 500ms added latency | ~32,000ms (+432%) | ~20,000ms (+233%) |
| 1% packet loss | ~6,000ms (+0%) | ~6,000ms (+0%) |
| 5% packet loss | ~7,000ms (+17%) | ~6,200ms (+4%) |
| 10% packet loss | ~14,900ms (+147%) | ~6,100ms (+1%) |

**v1 Time-to-Confidence:** highly variable because timeouts erode
confidence — best case ~3s, worst case >185s, oscillation case never
settles.

**Transport resilience:** Both TCP and QUIC maintain 0% FNR/FPR under
all tested loss levels. Initial single-run data suggested a QUIC
advantage, but follow-up testing showed this was a statistical artifact
from insufficient runs — neither transport shows a consistent
convergence advantage.

### Re-probe Tradeoff

v2's stability comes at the cost of slower reaction to genuine changes:

| | v1 | v2 (high confidence) |
|---|---|---|
| Re-probe interval | 15 min (configurable) | 1 hour primary / 3 hours secondary (configurable) |
| Flips on server unreliability | Yes (4 timeouts sufficient) | No (only E_DIAL_ERROR counts) |
| Detects genuine NAT change | Within ~15 min | Within ~1 hour |
| Event-driven re-probe triggers | None | None |

### DHT Impact

In go-libp2p, the DHT in `ModeAuto` subscribes to v1's
`EvtLocalReachabilityChanged`. It does not subscribe to v2's events.
When v1 oscillates, each flip triggers a DHT server↔client mode
switch, causing routing table updates across connected peers.

**Production observation (Kubo on live IPFS network, 2026-03-10):**
1. v2 confirms 2 addresses reachable at ~5s
2. v1 reports PUBLIC at ~8s
3. v1 decays to Unknown at ~45s (timeouts from unreliable peers)
4. v2 addresses remain reachable throughout
5. DHT switches to client mode — despite confirmed v2 reachability
