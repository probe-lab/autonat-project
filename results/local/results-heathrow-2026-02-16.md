# AutoNAT v2 Local Experiment: Heathrow Airport WiFi

**Date:** 2026-02-16 09:47–09:52 UTC
**Location:** Heathrow Airport, London
**Network:** Airport WiFi (BT/Openreach)
**Runs:** 4 (1 initial + 3 multi-run batch)
**Transport:** TCP + QUIC (both)
**go-libp2p:** v0.47.0

## Summary

| Metric | Run 0 | Run 1 | Run 2 | Run 3 | Mean | Std Dev |
|--------|-------|-------|-------|-------|------|---------|
| Final reachability | private | private | private | private | — | — |
| Reachability transitions | 1 | 1 | 1 | 1 | 1.0 | 0.0 |
| Time to result (ms) | 17202 | 17028 | 17128 | 17221 | 17145 | 84 |
| Time to addr discovery (ms) | 5007 | 5011 | 5007 | 5008 | 5008 | 2 |
| Time to unreachable (ms) | 11440 | 12888 | 13336 | 11215 | 12220 | 1032 |
| Bootstrap peers connected | 5/5 | 5/5 | 5/5 | 5/5 | 5.0 | 0.0 |
| Time to all bootstrap (ms) | 3573 | 2739 | 1668 | 2102 | 2521 | 697 |
| Public addresses discovered | 2 | 2 | 2 | 2 | 2.0 | 0.0 |

**Result: 4/4 runs = private. Zero oscillation. Zero false positives.**

## Network Environment

- **Private IP:** `10.22.91.36` (RFC 1918 — airport WiFi DHCP)
- **Public IP:** `81.145.206.39` (BT/Openreach — Heathrow NAT gateway)
- **NAT type:** Unknown (not measured), but behavior is consistent with port-restricted or symmetric
- **Listen addresses:** `/ip4/10.22.91.36/tcp/4001`, `/ip4/10.22.91.36/udp/4001/quic-v1`

## Phase Timeline (typical run)

```
 0s     Node starts, listens on private IP
 |
 ~0.4s  First bootstrap peer connected
 |
 ~2.5s  All 5 bootstrap peers connected, DHT running
 |
 5.0s   Identify observes public IP (81.145.206.39)
 |      → 2 public addresses added: TCP + QUIC
 |      → AutoNAT starts probing (status: unknown)
 |
 ~12s   Both addresses marked UNREACHABLE
 |      → Public addresses removed from advertised set
 |
 ~17s   Confidence threshold reached → PRIVATE
 |      (single transition, no oscillation)
 |
 ~32s   SIGTERM sent (15s after stable)
```

## Bootstrap Peer Latencies

Connection time from node start to successful libp2p connection (includes DNS, TCP/QUIC handshake, Noise/TLS, muxer negotiation).

| Peer (short ID) | Run 0 | Run 1 | Run 2 | Run 3 | Mean | Min | Max |
|------------------|-------|-------|-------|-------|------|-----|-----|
| Qm..ezGAJN | 1288ms | 458ms | 439ms | 352ms | 634ms | 352ms | 1288ms |
| Qm..19uLTa | 2076ms | 936ms | 687ms | 610ms | 1077ms | 610ms | 2076ms |
| Qm..nj75Nb | 2253ms | 1382ms | 773ms | 738ms | 1287ms | 738ms | 2253ms |
| Qm..YW3dwt | 2762ms | 1878ms | 1219ms | 1236ms | 1774ms | 1219ms | 2762ms |
| Qm..QLuvuJ | 3573ms | 2739ms | 1668ms | 2102ms | 2521ms | 1668ms | 3573ms |

Run 0 was the cold start (no cached connections). Runs 1-3 were faster, likely
due to DNS caching and warmed-up network paths. Connection order is consistent
across all runs (same peer always fastest).

## Address Discovery via Identify

In all 4 runs, the public IP was discovered at **exactly ~5000ms** after start.
This is not network latency — it's go-libp2p's internal `EvtLocalAddressesUpdated`
timer that batches Identify observations. The node learns its public IP from
bootstrap peers via Identify's `ObservedAddr`, but only pushes the update on a
5-second cadence.

| Run | Public IP discovered at | Addresses |
|-----|------------------------|-----------|
| 0 | 5007ms | `/ip4/81.145.206.39/tcp/4001`, `/ip4/81.145.206.39/udp/4001/quic-v1` |
| 1 | 5011ms | same |
| 2 | 5007ms | same |
| 3 | 5008ms | same |

Both TCP and QUIC public addresses were discovered simultaneously. The node
correctly filtered its private IP (`10.22.91.36`) — only the NAT's external IP
was submitted for AutoNAT probing.

## AutoNAT Probing Phase

After address discovery (~5s), AutoNAT v2 starts probing the public addresses
with remote servers discovered via DHT. The probing phase measures:

| Metric | Run 0 | Run 1 | Run 2 | Run 3 |
|--------|-------|-------|-------|-------|
| Probe start (addr discovery) | 5007ms | 5011ms | 5007ms | 5008ms |
| Addrs marked unreachable | 11440ms | 12888ms | 13336ms | 11215ms |
| Probing duration | 6433ms | 7877ms | 8329ms | 6207ms |
| Reachability declared | 17202ms | 17028ms | 17128ms | 17221ms |
| Confidence delay | 5762ms | 4140ms | 3792ms | 6006ms |

- **Probing duration** (time from address discovery to unreachable): 6.2–8.3s.
  This includes the AutoNAT anti-thundering-herd random delay (up to 3s) plus
  the actual dial-back attempts by remote servers.
- **Confidence delay** (time from unreachable to final reachability): 3.8–6.0s.
  go-libp2p requires multiple consistent probe results before changing
  reachability status (confidence threshold).

## Reachability State Machine

All 4 runs followed the identical state sequence:

```
[start] → unknown → private
```

No intermediate states. No oscillation. No false positives. The transition
happened exactly once per run at ~17s.

| Transition | Run 0 | Run 1 | Run 2 | Run 3 |
|------------|-------|-------|-------|-------|
| unknown → private | 17202ms | 17028ms | 17128ms | 17221ms |

## Observations

1. **Highly consistent and correct.** All 4 runs converged to `private` with
   zero oscillation. Time to result was 17.0–17.2s (84ms std dev). This is a
   textbook true negative for a NATted host.

2. **The 5s address discovery delay is a fixed cost.** go-libp2p batches
   Identify observations on a 5-second timer. This means even if the node
   learns its public IP in <1s, AutoNAT probing doesn't start until ~5s.

3. **Bootstrap latency is the main variable.** Run 0 (cold start) took 3.6s to
   connect all 5 peers. Subsequent runs took 1.7–2.7s. On slower networks,
   this could push the total time to result past 20s.

4. **Both transports fail identically.** TCP and QUIC public addresses were
   both marked unreachable at the same time. The airport NAT blocks inbound
   connections on both protocols equally.

5. **No server rejections observed.** All 5 bootstrap peers connected
   successfully. Public AutoNAT servers responded to probe requests without
   rate-limiting or rejection (no `E_REQUEST_REJECTED` events in logs).

6. **Public IP is stable.** The same external IP (`81.145.206.39`) was observed
   in all 4 runs across ~5 minutes. Airport WiFi NAT maintains stable
   mappings at least over this timescale.

## Comparison with Expected Testbed Results

| Scenario | Expected result | Time to result | Oscillation |
|----------|----------------|----------------|-------------|
| **Testbed: port-restricted** | private | ~17s (estimated) | none |
| **Testbed: symmetric** | private | ~17s (estimated) | none |
| **Heathrow WiFi (this test)** | private | 17.1s (measured) | none |
| **Testbed: no-NAT** | public | ~17s (estimated) | none |
| **Testbed: address-restricted** | public? (hypothesis) | TBD | TBD |

The real-world result aligns with port-restricted or symmetric NAT behavior.
The time-to-result is expected to match the testbed once Docker experiments
are run, since the protocol timing is dominated by go-libp2p's internal
timers (5s address batch + ~7s probing + ~5s confidence), not network latency.

## Raw Data

### CSV (for analysis)

All CSV files include documentation headers (lines starting with `#`) describing
columns and methodology. Import with `comment='#'` in pandas or skip `#` lines.

| File | Rows | Description |
|------|------|-------------|
| `results/heathrow-2026-02-16-summary.csv` | 4 | Per-run summary metrics (reachability, timings, peer counts) |
| `results/heathrow-2026-02-16-bootstrap.csv` | 20 | Bootstrap peer connection latencies (5 peers x 4 runs) |
| `results/heathrow-2026-02-16-events.csv` | 60 | All events from all runs (full timeline) |

```python
# Example: load in pandas
import pandas as pd
summary = pd.read_csv('results/heathrow-2026-02-16-summary.csv', comment='#')
bootstrap = pd.read_csv('results/heathrow-2026-02-16-bootstrap.csv', comment='#')
events = pd.read_csv('results/heathrow-2026-02-16-events.csv', comment='#')
```

### JSON (raw logs)

| File | Description |
|------|-------------|
| `results/local-both-heathrow-20260216T094739Z-run1.jsonl` | Initial single run (raw JSON log) |
| `results/local-both-heathrow-20260216T094739Z.json` | Initial single run (summary) |
| `results/local-both-heathrow-20260216T094946Z-run1.jsonl` | Batch run 1 (raw JSON log) |
| `results/local-both-heathrow-20260216T094946Z-run2.jsonl` | Batch run 2 (raw JSON log) |
| `results/local-both-heathrow-20260216T094946Z-run3.jsonl` | Batch run 3 (raw JSON log) |
| `results/local-both-heathrow-20260216T094946Z.json` | Batch run summary (JSON) |
