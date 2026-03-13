# AutoNAT v2 Measurement Results

**Date:** 2026-03-13
**Testbed:** Docker-based lab, 7 AutoNAT v2 servers, configurable NAT (iptables)
**Implementation:** go-libp2p v0.47.0
**Transports:** TCP and QUIC

---

## 1. Convergence Time: v1 vs v2

![v1 vs v2 convergence](../results/figures/01_convergence.png)

Comparing AutoNAT v1 (`reachability_changed`) and v2 (`reachable_addrs_changed`) first-event timing across TCP and QUIC:

| NAT Type | v1 (TCP) | v2 (TCP) | v1 (QUIC) | v2 (QUIC) | Verdict |
|----------|----------|----------|-----------|-----------|---------|
| None (public IP) | 3.0s | 6.0s | 3.0s | 6.0s | Reachable |
| Full-cone | 3.0s | 6.0s | 3.0s | 6.0s | Reachable |
| Address-restricted | 3.0s | 6.0s | 3.0s | 6.0s | Reachable (false positive) |
| Port-restricted | 18.0s | 6.0s | 18.0s | 11.0s | Unreachable |
| Symmetric | 18.0s | *no event* | 18.0s | *no event* | Unreachable |

**Key findings:**
- **v1 is faster for reachable nodes** (3s vs 6s) because v1 uses a simpler binary check while v2 needs to probe individual addresses and accumulate confidence.
- **v2 is 3x faster for unreachable nodes** behind port-restricted NAT (6s vs 18s on TCP). v1 needs its full `retryInterval` to conclude.
- **Symmetric NAT is a blind spot for v2** — no public address is ever discovered, so v2 never initiates probing. Only v1 provides a verdict.
- QUIC v2 is slower than TCP v2 for port-restricted (11s vs 6s), likely due to UDP-specific retry/timeout behavior.
- The 3s floor for reachable nodes comes from AutoNAT v2's `bootDelay` before probing starts.
- Address-restricted NAT converges at 3s with a **reachable** verdict — this is a confirmed false positive (see Detection Correctness below).

---

## 2. Detection Accuracy

### Per-NAT correctness

![Detection correctness](../results/figures/05_detection_correctness.png)

Ground truth: nodes behind address-restricted, port-restricted, and symmetric NAT are **not** reachable by arbitrary peers. Only none and full-cone are truly reachable.

| NAT Type | TCP | QUIC | Correct? |
|----------|-----|------|----------|
| None | Reachable | Reachable | Yes |
| Full-cone | Reachable | Reachable | Yes |
| Address-restricted | Reachable | Reachable | **No — false positive** |
| Port-restricted | Unreachable | Unreachable | Yes |
| Symmetric | Unreachable | Unreachable | Yes |

**The address-restricted false positive is the most significant finding.** Address-restricted cone NAT (EIM + ADF) is the default behavior of Linux iptables `MASQUERADE` — meaning it is the most common NAT type on home routers. AutoNAT v2's dial-back succeeds because the server dials back from the same IP (which passes ADF filtering) but a different port. The protocol cannot distinguish full-cone from address-restricted.

### FNR/FPR across conditions (v1 vs v2)

![FNR/FPR summary](../results/figures/07_fnr_fpr_summary.png)

The figure compares v1 and v2 FNR/FPR across all tested conditions, including the v1/v2 gap scenario (full-cone NAT with 3 unreliable out of 7 servers).

**False Positive Rate (right panel):**
- **33% for both v1 and v2**, constant across all conditions. This is entirely due to the address-restricted false positive shown above (1 of 3 unreachable NAT types incorrectly detected as reachable). The FPR is structural, not probabilistic — it does not vary with network conditions because the root cause is the protocol design (same-IP dial-back).

**False Negative Rate (left panel):**
- **0% for both v1 and v2** under baseline and all degraded conditions (reliable servers only).
- **v1 FNR = 25% in the v1/v2 gap scenario** — when 3 of 7 servers are unreliable, v1 initially classifies a reachable full-cone node as "private" in 1 out of 4 runs. This is v1's sliding window oscillation problem: with mixed reliable/unreliable peers, v1 randomly samples and can get an unlucky majority.
- **v2 FNR = 0% in all scenarios including the gap scenario** — v2's per-address confidence system is resilient to unreliable servers because it accumulates confirmations from the 4 reliable servers and reaches `targetConfidence=3` independently of the unreliable ones.

**Key takeaway:** v2 is strictly more reliable than v1 for false negatives under real-world conditions where not all peers are reliable. v1's whole-node binary approach is vulnerable to oscillation when the peer pool is mixed.

---

## 3. Impact of Network Latency

![Latency impact](../results/figures/03_latency_impact.png)

Convergence time under added one-way latency (RTT = 2x):

| NAT Type | Baseline | +200ms | +500ms | Degradation |
|----------|----------|--------|--------|-------------|
| Full-cone (TCP) | 3.0s | 8.0s | 13.0s | +10s at 500ms |
| Address-restricted (TCP) | 3.0s | 7.0s | 12.0s | +9s at 500ms |
| Port-restricted (TCP) | 6.0s | 17.0s | 25.0s | +19s at 500ms |
| Symmetric (TCP) | 18.0s | 21.0s | 25.0s | +7s at 500ms |

**Key findings:**
- Latency has a **proportionally larger impact on reachable nodes** than unreachable ones. Full-cone TCP goes from 3s to 13s (4.3x) while symmetric goes from 18s to 25s (1.4x).
- Port-restricted TCP is the most affected: 6s → 25s (4.2x). Each failed probe attempt adds the full RTT penalty.
- QUIC shows similar patterns but with slightly different absolute values.
- At 500ms added latency (1s RTT), all NAT types converge within 25s — still well under AutoNAT's default timeout, so no probe failures from timeouts at this latency.

---

## 4. Impact of Packet Loss

![Packet loss impact](../results/figures/04_packet_loss_impact.png)

Convergence time under packet loss:

| NAT Type | Baseline | 1% loss | 5% loss | 10% loss |
|----------|----------|---------|---------|----------|
| Full-cone (TCP) | 3.0s | 3.0s | 3.4s | 3.0s |
| Address-restricted (TCP) | 3.0s | 4.0s | 4.0s | 6.5s |
| Port-restricted (TCP) | 6.0s | 6.0s | 6.0s | 19.4s |
| Symmetric (TCP) | 18.0s | 18.0s | 18.0s | 19.0s |

**Key findings:**
- **Packet loss up to 5% has minimal impact** on convergence time across all NAT types and transports.
- At **10% loss, TCP port-restricted degrades sharply** (6s → 19.4s), approaching symmetric NAT levels. This is because each failed dial-back probe must complete its full timeout before the next attempt.
- QUIC is **more resilient to packet loss** than TCP — the QUIC panel shows relatively flat lines even at 10% loss, likely due to QUIC's built-in loss recovery at the transport layer.
- Symmetric NAT is barely affected by packet loss because its 18s convergence time is dominated by the v1 probe interval, not network round-trips.

---

## 5. Convergence Heatmaps

### TCP
![TCP heatmap](../results/figures/08_convergence_heatmap_tcp.png)

### QUIC
![QUIC heatmap](../results/figures/08_convergence_heatmap_quic.png)

The heatmaps show convergence time across all NAT types and network conditions (packet loss + latency). Key patterns:

- **Top-left (light):** Reachable NATs under good conditions — 3s convergence.
- **Bottom-right (dark):** Unreachable NATs under degraded conditions — up to 25s.
- **Latency is the dominant factor** for degradation. The rightmost columns (200ms, 500ms latency) show the steepest color shifts.
- **TCP is more sensitive** to degradation than QUIC overall. The TCP heatmap shows more color variation (wider dynamic range) than QUIC.

---

## 6. Time-to-Update: Dynamic Network Changes

![Time-to-update](../results/figures/06_time_to_update.png)

Measures how quickly AutoNAT detects a reachability change when port forwarding is toggled mid-session on a port-restricted NAT node:

| Phase | Action | Detection Time |
|-------|--------|---------------|
| 1 | Add port forwarding (unreachable → reachable) | 30s |
| 2 | Remove port forwarding (reachable → unreachable) | 69s |

**Key findings:**
- Detecting a transition to **reachable takes ~30s**. This is dominated by the v2 probe interval — the node must wait for the next scheduled probe cycle after the forwarding rule is added.
- Detecting a transition to **unreachable takes ~69s** — more than 2x slower. The node must accumulate enough failed probes to override its previous confident "reachable" state.
- The asymmetry makes sense: gaining confidence requires fewer confirmations than losing it (hysteresis prevents oscillation from transient failures).
- Total time from network change to detection: **~100s for a full unreachable→reachable→unreachable cycle**.

---

## 7. v1/v2 Reachability Gap: Oscillation Under Unreliable Peers

![v1/v2 gap comparison](../results/figures/10_v1_v2_gap_comparison.png)

This experiment reproduces the v1/v2 reachability gap under controlled conditions. A client behind full-cone NAT (truly reachable) connects to a mix of reliable and unreliable AutoNAT servers. Unreliable servers accept connections but their dial-back is blocked by iptables — simulating DHT peers behind restrictive NAT that cannot complete the verification.

**Setup:**
- Full-cone NAT, TCP transport, 7 total servers
- v1 `refreshInterval` overridden to 30s (default is 15min) to capture oscillation within the observation window
- v2 `targetConfidence=3`, `backoffStartInterval=5s`
- Three scenarios: 71%, 29%, and 0% unreliable servers

**Results:**

| Unreliable Ratio | v1 Flips | v1 GAP Duration | v2 Status |
|------------------|----------|-----------------|-----------|
| 5/7 (71%) | 6 (3 private episodes) | 75s, 95s, 85s | Stable reachable |
| 2/7 (29%) | 3 (2 private episodes) | 95s, 65s | Stable reachable |
| 0/7 (0%) | 0 | — | Stable reachable |

**Why v1 oscillates:**
- v1 uses **whole-node reachability** with a sliding confidence window of 3. Each probe picks one random peer from all connected peers.
- When v1 picks an unreliable server, the dial-back fails, confidence decreases, and with enough failed probes the node flips to "private".
- On the next cycle, if v1 picks a reliable server, it flips back to "public". This creates oscillation proportional to the unreliable server ratio.
- With 71% unreliable servers, v1 spends ~40% of the observation window incorrectly reporting "private".

**Why v2 is unaffected:**
- v2 tests **each address independently** and accumulates per-address confidence. Once 3 different reliable servers confirm an address is reachable, it stays confirmed.
- Failed dial-backs from unreliable servers simply don't count — they don't subtract from existing confidence.
- Even with 71% unreliable servers, the 2 reliable servers are sufficient to reach `targetConfidence=3`.

**Implication:** In real-world deployments where DHT peers have mixed reachability (common on the IPFS network), v1's whole-node approach causes persistent oscillation while v2 provides stable, correct results. Applications consuming `EvtLocalReachabilityChanged` (v1) may see frequent state changes that trigger unnecessary relay setup/teardown, while `EvtHostReachableAddrsChanged` (v2) remains stable.

---


## Summary of Findings

| Finding | Severity | Root Cause |
|---------|----------|------------|
| Address-restricted NAT false positive (33% FPR) | **High** | Protocol design: same-IP dial-back passes ADF filtering |
| Symmetric NAT bypasses v2 entirely | Medium | No public address activated → v2 never probes |
| v1/v2 reachability gap | Medium | Subsystems consume v1 only; v2 results ignored |
| QUIC dial-back failure on fresh servers | Low | Black hole detector blocks UDP on cold start |
| v1 oscillation (~33% of cycles) | Low | Sliding window inherently unstable |

| Metric | v1 | v2 | Condition |
|--------|-----|-----|-----------|
| False Negative Rate | 0–25% | 0% | 25% with unreliable servers (v1 only) |
| False Positive Rate | 33% | 33% | Structural (address-restricted NAT) |
| Best-case convergence | 3s | Reachable, no degradation |
| Worst-case convergence | 25s | Unreachable, 500ms latency |
| Time-to-update (gain reachability) | ~30s | Port forwarding toggle |
| Time-to-update (lose reachability) | ~69s | Port forwarding toggle |
