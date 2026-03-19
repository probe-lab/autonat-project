# Testbed Analysis Summary

Generated from existing traces on 2026-03-18.

---

## Full Matrix Baseline (7 servers, no degradation)

| Scenario | Result | TTC (ms) | FNR/FPR | Probes |
|----------|--------|----------|---------|--------|
| none-tcp-7 | reachable | 6,022 | 0% FNR | 3 |
| none-quic-7 | reachable | 6,019 | 0% FNR | 3 |
| full-cone-tcp-7 | reachable | 6,022 | 0% FNR | 3 |
| full-cone-quic-7 | reachable | 6,021 | 0% FNR | 3 |
| addr-restricted-tcp-7 | reachable | 6,021 | 0% FNR | 3 |
| addr-restricted-quic-7 | reachable | 6,021 | 0% FNR | 3 |
| port-restricted-tcp-7 | unreachable | 6,013 | 0% FPR | 3 |
| port-restricted-quic-7 | unreachable | 11,015 | 0% FPR | 4 |
| symmetric-tcp-7 | **NO SIGNAL** | — | N/A | 0 |
| symmetric-quic-7 | **NO SIGNAL** | — | N/A | 0 |

**Key observations:**
- 0% FNR and 0% FPR across all non-symmetric NAT types
- TTC consistently ~6s for TCP, ~6-11s for QUIC
- Port-restricted QUIC takes longer (11s) due to additional probe round
- **Symmetric NAT produces no reachability signal** (#80) — autonat v2 never runs because no address reaches the observed address activation threshold
- Address-restricted reports **reachable** (true for dial-back from servers that already connected, but potentially false positive for unsolicited inbound — see #36)

---

## High Latency Impact (7 servers)

| Scenario | TTC (ms) | vs baseline |
|----------|----------|-------------|
| full-cone-tcp-7 (baseline) | 6,022 | — |
| full-cone-tcp-7-lat200 | 18,670 | +210% |
| full-cone-tcp-7-lat500 | 32,023 | +432% |
| full-cone-quic-7 (baseline) | 6,021 | — |
| full-cone-quic-7-lat200 | 12,617 | +110% |
| full-cone-quic-7-lat500 | 20,022 | +233% |
| port-restricted-tcp-7 (baseline) | 6,013 | — |
| port-restricted-tcp-7-lat200 | 16,874 | +181% |
| port-restricted-tcp-7-lat500 | 27,519 | +358% |
| addr-restricted-tcp-7-lat200 | 18,421 | +206% |
| addr-restricted-tcp-7-lat500 | 32,021 | +432% |
| addr-restricted-quic-7-lat200 | 12,623 | +110% |
| addr-restricted-quic-7-lat500 | 20,020 | +232% |

**Key observations:**
- 0% FNR and 0% FPR even at 500ms latency — correctness unaffected
- QUIC is more latency-resilient than TCP (~110% increase at 200ms vs ~210% for TCP)
- At 500ms (1s RTT), TCP TTC reaches 32s — still within AutoNAT v2's 15s stream timeout because each probe round's RTT cost is added to the base ~6s
- Symmetric NAT still produces NO SIGNAL at all latency levels

---

## Packet Loss Impact (7 servers)

| Scenario | TTC (ms) | vs baseline |
|----------|----------|-------------|
| full-cone-tcp-7 (baseline) | 6,022 | — |
| full-cone-tcp-7-loss1 | 6,018 | +0% |
| full-cone-tcp-7-loss5 | 7,036 | +17% |
| full-cone-tcp-7-loss10 | 14,877 | +147% |
| full-cone-quic-7 (baseline) | 6,021 | — |
| full-cone-quic-7-loss1 | 6,025 | +0% |
| full-cone-quic-7-loss5 | 6,245 | +4% |
| full-cone-quic-7-loss10 | 6,089 | +1% |
| addr-restricted-tcp-7-loss10 | 8,501 | +41% |
| port-restricted-tcp-7-loss10 | 19,430 | +223% |
| port-restricted-quic-7-loss10 | 11,088 | +1% |

**Key observations:**
- 0% FNR and 0% FPR even at 10% packet loss — correctness unaffected
- **QUIC is dramatically more loss-resilient than TCP** — 10% loss adds +147% to TCP TTC but only +1% to QUIC TTC
- This is expected: QUIC's built-in retransmission handles packet loss at the transport layer, while TCP retransmission adds visible latency
- 1% packet loss has negligible impact on both transports

---

## v1 vs v2 Oscillation (full-cone, 2 reliable + 5 unreliable servers)

Best trace: `v1-v2-gap-20260313T122413Z/v1v2-gap-fullcone-tcp.json`

| Time (ms) | Protocol | Event |
|-----------|----------|-------|
| 3,026 | v1 | **PUBLIC** |
| 5,010 | v2 | reachable=[] (initial empty) |
| 6,018 | v2 | reachable=["/ip4/73.0.0.2/tcp/4001"] — **stable** |
| 108,027 | v1 | **PRIVATE** ← flipped! |
| 183,027 | v1 | **PUBLIC** ← flipped back! |

**v2 stayed reachable the entire observation window. v1 oscillated.**

All 4 v1-v2-gap runs (across 2 scenarios):

| Trace | v1 flips | v2 changes | v1 oscillated? |
|-------|----------|------------|----------------|
| v1v2-gap-fullcone-both (run 1) | 1 | 2 | No (single transition) |
| v1v2-gap-fullcone-tcp (run 1) | 2 | 2 | Yes (private→public) |
| v1v2-gap-fullcone-tcp (run 2) | 3 | 2 | **Yes (public→private→public)** |
| v1v2-gap-fullcone-both (run 2) | 1 | 2 | No (single transition) |
| v1v2-gap-fullcone-tcp (run 3) | 2 | 1 | Yes (public→private) |

TCP scenarios show v1 oscillation; QUIC/both scenarios are more stable (fewer unreliable server interactions).

---

## Time-to-Update (port forwarding toggle)

Scenario: `ttu-port-restricted-tcp` (port-restricted NAT, TCP transport)

| Phase | Action | Detected | TTU |
|-------|--------|----------|-----|
| 1 | Add port forward | Yes | **30s** |
| 2 | Remove port forward | Yes | **69s** |

Removal takes longer because the existing confirmed reachability must expire before re-probing detects the change.

Only 1 of 5 toggle scenarios has been run — remaining 4 need execution (#76).

---

## Aggregate Metrics

| Metric | Value | Notes |
|--------|-------|-------|
| Total traces analyzed | 52 | 10 matrix + 16 high-latency + 24 packet-loss + 2 TTU |
| FNR (all non-symmetric) | **0%** | 0 false negatives across 50 runs |
| FPR (all runs) | **0%** | 0 false positives across 50 runs |
| Baseline TTC (TCP) | ~6,000ms | Consistent across NAT types |
| Baseline TTC (QUIC) | ~6,000-11,000ms | Higher for port-restricted |
| Probes to converge | 3 (typical) | Matches targetConfidence=3 |
| Symmetric NAT | **No signal** | AutoNAT v2 never runs (#80) |
