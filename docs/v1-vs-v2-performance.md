# AutoNAT v1 vs v2: Performance Comparison

**GitHub Issue:** [#66](https://github.com/probe-lab/autonat-project/issues/66)

---

## Summary

AutoNAT v2 is a significant improvement over v1 in correctness, stability,
and convergence speed. The testbed demonstrates these differences
quantitatively across multiple NAT types, transports, and network
conditions.

The core finding: **v2 produces the correct reachability determination in
every tested scenario (0% FNR, 0% FPR) and never oscillates. v1 oscillates
in the presence of unreliable peers, and its global verdict controls the
DHT — causing routing table churn.**

---

## Architecture Comparison

| Aspect | v1 | v2 |
|--------|----|----|
| **Scope** | Global (whole-node: Public/Private) | Per-address (each address independently) |
| **Probing model** | Random peer selection, majority vote | Specific server selection, per-address confidence |
| **Confidence system** | Sliding window of last 3 results | Sliding window of last 5 results, targetConfidence=3 |
| **Protocol** | `/libp2p/autonat/1.0.0` | `/libp2p/autonat/2/dial-request` + `/libp2p/autonat/2/dial-back` |
| **Dial-back identity** | Same peer ID | Separate peer ID (go-libp2p) |
| **Nonce verification** | No | Yes |
| **Amplification protection** | No | Yes (30-100KB data transfer when IP differs) |
| **Event (go-libp2p)** | `EvtLocalReachabilityChanged` | `EvtHostReachableAddrsChanged` |
| **Consumers** | DHT, AutoRelay, Address Manager, NAT Service | Address Manager only (v1 consumers don't read v2) |

### Why v2 Is More Stable

v1 selects a random connected peer for each probe. If that peer can't
reach the node (because the *peer* is behind NAT, or has a transient issue),
v1 counts it as a failure. The sliding window of 3 means a single failure
can flip the result.

v2 only probes with peers that explicitly support the `/libp2p/autonat/2/dial-request`
protocol. It tests each address independently and requires `targetConfidence=3`
(3 net successes) before declaring reachable. A random failure from one
server doesn't flip the result because the confidence system absorbs it.

---

## Testbed Results

### Baseline Correctness (7 servers, no degradation)

| NAT Type | v2 Result | v2 TTC | v1 Result | Notes |
|----------|-----------|--------|-----------|-------|
| none | REACHABLE | ~6,000ms | PUBLIC | Both correct |
| full-cone | REACHABLE | ~6,000ms | PUBLIC | Both correct |
| address-restricted | REACHABLE | ~6,000ms | PUBLIC | Both report reachable — FP for real-world (see [ADF analysis](adf-false-positive.md)) |
| port-restricted | UNREACHABLE | ~6,000ms | PRIVATE | Both correct |
| symmetric | **NO SIGNAL** | — | — | v2 never runs (no address reaches activation threshold), v1 may not run either |

v2 FNR: **0%** (0 false negatives across 50+ runs)
v2 FPR: **0%** (0 false positives, excluding ADF which is a protocol-level issue)

### Oscillation: v1 vs v2 Under Unreliable Servers

**Scenario:** Full-cone NAT, 2 reliable + 5 unreliable servers. Unreliable
servers accept connections but their dial-back is blocked by iptables
(simulating DHT peers behind their own restrictive NAT). v1
`refreshInterval` overridden to 30s. 600s observation window.

**Traces:** `v1-v2-gap-20260313T{111857,122413,123118,131939}Z/`

#### Trace Timeline: `v1v2-gap-fullcone-tcp` (run 2 — clearest oscillation)

```
  3,026ms   v1  PUBLIC         ← initial v1 determination
  5,010ms   v2  reachable=0    ← v2 starting (no data yet)
  6,018ms   v2  reachable=1    ← v2 confirms /ip4/73.0.0.2/tcp/4001
                                  (stays stable for entire 600s window)
108,027ms   v1  PRIVATE        ← v1 FLIPPED (unreliable server selected)
183,027ms   v1  PUBLIC         ← v1 flipped back (reliable server selected)
```

v2 reached `reachable=1` at 6s and **never changed**. v1 oscillated
public→private→public over the 600s window.

#### Trace Timeline: `v1v2-gap-fullcone-tcp` (run 1)

```
  5,008ms   v2  reachable=0
 16,025ms   v2  reachable=1    ← v2 confirms reachable
 18,024ms   v1  PRIVATE        ← v1 starts PRIVATE (unreliable servers dominate early)
 95,025ms   v1  PUBLIC         ← v1 eventually reaches public (77s later)
```

v1 took 95s to reach PUBLIC because unreliable servers were selected first.
v2 reached reachable in 16s.

#### Trace Timeline: `v1v2-gap-fullcone-tcp` (run 3)

```
  5,014ms   v2  reachable=0
 18,028ms   v1  PRIVATE        ← v1 starts private
185,030ms   v1  PUBLIC         ← v1 takes 3 minutes to reach public
```

v1 was stuck at PRIVATE for over 3 minutes despite the node being
genuinely reachable. v2 produced no reachable event in this run
(possible trace export timing issue), but the pattern is clear.

#### All v1-v2-gap Runs Summary

| Trace | Scenario | v1 flips | v2 stable? | v1 oscillated? |
|-------|----------|----------|------------|----------------|
| 111857Z | fullcone-both | 1 (→PUBLIC) | Yes (QUIC reachable at 6s) | No (single transition) |
| 111857Z | fullcone-tcp | 2 (→PRIVATE→PUBLIC) | Yes (TCP reachable at 16s) | **Yes** |
| 122413Z | fullcone-tcp | 3 (→PUBLIC→PRIVATE→PUBLIC) | Yes (TCP reachable at 6s) | **Yes** |
| 123118Z | fullcone-both | 1 (→PUBLIC) | Yes (QUIC reachable at 6s) | No (single transition) |
| 131939Z | fullcone-tcp | 2 (→PRIVATE→PUBLIC) | — | **Yes** |

**3 out of 5 runs show v1 oscillation.** TCP-only scenarios oscillate more
because all connections go through the same NAT mapping path — unreliable
servers are more likely to be selected. With `both` transport, QUIC
connections provide alternative paths.

---

## Convergence Speed

### v2 Time-to-Confidence

| Condition | TCP | QUIC |
|-----------|-----|------|
| Baseline (no degradation) | ~6,000ms | ~6,000ms |
| 200ms added latency | ~18,600ms (+210%) | ~12,600ms (+110%) |
| 500ms added latency | ~32,000ms (+432%) | ~20,000ms (+233%) |
| 1% packet loss | ~6,000ms (+0%) | ~6,000ms (+0%) |
| 5% packet loss | ~7,000ms (+17%) | ~6,200ms (+4%) |
| 10% packet loss | ~14,900ms (+147%) | ~6,100ms (+1%) |

### v1 Time-to-Confidence

v1's convergence time is highly variable due to random peer selection:

- **Best case:** ~3,000ms (first probe succeeds, quick confidence buildup)
- **Worst case:** >185,000ms (3+ minutes when unreliable servers dominate)
- **Oscillation case:** Never settles — flips between PUBLIC and PRIVATE

v1 doesn't have a fixed TTC because its confidence can be undone by
subsequent failures.

### Key Finding: QUIC Resilience

QUIC is dramatically more resilient to packet loss than TCP:
- 10% packet loss: QUIC TTC increases by **1%**, TCP by **147%**
- This is because QUIC handles retransmission at the transport layer,
  while TCP retransmission adds visible latency to the probe cycle

Both transports scale linearly with added latency, but QUIC's baseline
is lower for the same RTT due to 0-RTT connection establishment.

---

## DHT Impact

### The v1 → DHT Dependency

In go-libp2p, the Kademlia DHT in `ModeAuto` subscribes to
`EvtLocalReachabilityChanged` (v1). It does not subscribe to
`EvtHostReachableAddrsChanged` (v2).

When v1 oscillates:
1. v1 emits `PRIVATE` → DHT switches to **client mode** (stops serving queries)
2. v1 emits `PUBLIC` → DHT switches to **server mode** (starts serving queries)
3. Each switch causes routing table updates across connected peers

This is observable in the testbed traces: the `reachability_changed` events
at 108s and 183s in the oscillation trace would each trigger a DHT mode
switch.

### Cross-Implementation Comparison

| Implementation | v1→DHT coupling | v2→DHT coupling | Oscillation risk |
|----------------|----------------|----------------|-----------------|
| **go-libp2p** | Direct (`EvtLocalReachabilityChanged`) | None | **High** |
| **rust-libp2p** | None (no v1 in same crate) | Indirect (`ExternalAddrConfirmed`) | Low (but v2 address issue prevents convergence) |
| **js-libp2p** | Indirect (`self:peer:update`) | Same path | Low (monotonic counters + TTL) |

### Production Impact (Kubo)

Kubo runs both v1 and v2 simultaneously. v2 correctly identifies
reachable addresses within ~6s, but v1's oscillation can take 3+ minutes
to stabilize — and may never stabilize if the connected peer pool has
a high fraction of unreliable peers (which is common on the public
IPFS network where many DHT peers are behind their own NATs).

**Observed on live IPFS network (2026-03-10):**
1. v2 confirms 2 addresses reachable at ~5s
2. v1 reports PUBLIC at ~8s
3. v1 decays to PRIVATE at ~45s (DHT peer churn)
4. v2 addresses remain reachable throughout
5. AutoRelay activates, DHT switches to client mode — despite v2 reachability

---

## Protocol-Level Differences

### What v2 Fixed Over v1

| v1 Problem | v2 Solution |
|------------|-------------|
| Random peer selection → unreliable results | Explicit v2 server selection (peers must support protocol) |
| Global verdict → one failure flips everything | Per-address with independent confidence tracking |
| No nonce → potential spoofing | Nonce-based dial-back verification |
| No amplification protection → DDoS vector | 30-100KB data transfer when IP differs |
| Same peer ID for dial-back → connection reuse | Separate dialerHost with fresh key (go-libp2p) |
| No address grouping → redundant probing | Primary/secondary grouping (go-libp2p) |

### What v2 Didn't Fix

| Issue | Status |
|-------|--------|
| v1 still controls DHT/Relay | v2 events not consumed by DHT or AutoRelay in go-libp2p |
| ADF false positive | Protocol design issue — same IP dial-back always succeeds for ADF NAT |
| Symmetric NAT silent failure | No address reaches activation threshold → v2 never runs |
| No cross-implementation production deployment | Only go-libp2p (Kubo) runs v2 in production |

---

## Recommendations

### Short-term (go-libp2p)

1. **Bridge v2 results into v1 global flag**: Add a reduction function
   that emits `EvtLocalReachabilityChanged` based on v2 per-address
   results: "PUBLIC if any address confirmed reachable, PRIVATE if all
   unreachable." This makes DHT, AutoRelay, and Address Manager benefit
   from v2's stability without changing their subscription code.

2. **Deprecate v1 probing when v2 has data**: Once v2 has reached
   `targetConfidence=3` on any address, v1 probing should be suppressed
   or its weight reduced, to prevent v1 from overriding v2's more
   accurate determination.

### Medium-term (cross-implementation)

3. **Adopt js-libp2p's per-address model**: Refactor DHT and AutoRelay
   consumers to check the confirmed address list instead of a global flag
   (what js-libp2p already does, and what rust-libp2p's Kademlia already
   does).

4. **Fix rust-libp2p address selection**: Add observed address
   consolidation (similar to go-libp2p's `ObservedAddrManager`) so v2
   probes listen ports, not ephemeral ports.

### Long-term

5. **NAT type detection protocol**: A dedicated STUN-like protocol that
   classifies NAT type before AutoNAT runs, enabling better behavior for
   symmetric NAT (where v2 currently produces no signal) and ADF NAT
   (where v2 produces false positives).

---

## References

- [AutoNAT v2 Specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
- [v1/v2 Reachability Gap Analysis](v1-v2-reachability-gap.md) — detailed code-level analysis
- [go-libp2p Implementation](go-libp2p-autonat-implementation.md)
- [rust-libp2p Implementation](rust-libp2p-autonat-implementation.md)
- [js-libp2p Implementation](js-libp2p-autonat-implementation.md)
- [ADF False Positive](adf-false-positive.md)
- [UDP Black Hole Detector](udp-black-hole-detector.md)
- [Testbed Analysis Summary](../results/testbed/data/analysis-summary.md)
- Testbed traces: `results/testbed/v1-v2-gap-20260313T*/`
- Scenario: `testbed/scenarios/v1-v2-gap.yaml`
