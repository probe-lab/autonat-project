# AutoNAT v2 Experiment: Hotel WiFi (2026-02-19)

## Environment

- **Location**: Hotel (restricted connectivity)
- **Network**: Hotel WiFi (captive portal / managed network)
- **Local IP**: 172.20.2.236/19 (RFC 1918 private, 172.16.0.0/12)
- **Public IP**: 63.211.255.232 (confirmed via ifconfig.me)
- **Gateway**: 172.20.0.1
- **DNS**: 172.20.0.1 (hotel's own resolver)
- **Interface**: en0 (WiFi)
- **Port used**: 4001
- **Transport**: both (TCP + QUIC)
- **Timeout**: 120s per run

## Network Characteristics

| Metric | Value |
|--------|-------|
| RTT (ping 8.8.8.8) | 5-7ms, avg 6.2ms |
| Jitter (stddev) | 0.6ms |
| Packet loss | 0% |
| IPv6 | No route |
| UDP connectivity | Working (DNS, QUIC handshakes succeed) |
| TCP port 4001 (outbound) | **Blocked** |
| TCP port 443 (outbound) | Working (QUIC/TLS bootstraps succeed) |

## Key Finding: QUIC-Only Address Discovery + v1 Oscillation

### NAT behavior

The hotel NAT shows **endpoint-independent mapping with port remapping** for UDP:
- Internal port 4001 is mapped to external port **29538** (consistent across all peers)
- All 5 bootstrap peers see the same external address: `/ip4/63.211.255.232/udp/29538/quic-v1`
- This allows the observed address manager to activate the address (ActivationThresh=4 met)

### TCP blocked

Outbound TCP to port 4001 is blocked by the hotel firewall. Bootstrap connections
succeed because go-libp2p prefers QUIC (UDP). This means:
- No TCP connections to bootstrap peers → no TCP observed addresses via Identify
- No TCP public address is ever activated
- Only the QUIC public address is discovered and probed

### AutoNAT v2 behavior

AutoNAT v2 worked correctly for the QUIC address:
- **5s**: QUIC public address `/ip4/63.211.255.232/udp/29538/quic-v1` activated, marked "unknown"
- **11s**: Address probed and marked **unreachable**, then removed from address list

This is consistent across all 3 runs — AutoNAT v2 correctly identifies the QUIC
address as unreachable behind the hotel NAT.

### AutoNAT v1 oscillation (Run 1)

Run 1 showed significant v1 instability despite low latency (6ms RTT):

```
19,040ms  reachability_changed: private
22,761ms  reachability_changed: unknown  (+3.7s)
25,153ms  reachability_changed: private  (+2.4s)
32,912ms  reachability_changed: unknown  (+7.8s)
37,892ms  reachability_changed: private  (+5.0s)
41,210ms  reachability_changed: unknown  (+3.3s)
```

6 transitions total, ending as `unknown`. Runs 2 and 3 were stable with a single
`private` at 17s. This shows **v1 oscillation is not solely caused by high latency**
— it can happen on low-latency hotel networks too.

## Results Summary

| Metric | Run 1 | Run 2 | Run 3 |
|--------|-------|-------|-------|
| Final reachability | **unknown** | private | private |
| Time to first v1 event | 19,040ms | 17,158ms | 17,157ms |
| Time to stable | 41,210ms (unstable) | 17,158ms | 17,157ms |
| v1 transitions | **6** | 1 | 1 |
| Bootstrap peers | 5/5 | 5/5 | 5/5 |
| Bootstrap latency | 0.5-2.5s | 0.2-1.6s | 0.3-1.7s |
| QUIC public address discovered | Yes (5s) | Yes (5s) | Yes (5s) |
| TCP public address discovered | No | No | No |
| v2 QUIC result | unreachable (11s) | unreachable (11s) | unreachable (11s) |

## Timeline (Run 2 — stable, representative)

```
0ms      started (both TCP+QUIC, port 4001)
209ms    bootstrap peer 1 connected (QUIC)
436ms    bootstrap peer 2 connected
969ms    bootstrap peer 3 connected
1,505ms  bootstrap peer 4 connected
1,570ms  bootstrap peer 5 connected, bootstrap done
5,014ms  addresses_updated: +63.211.255.232/udp/29538/quic-v1
5,014ms  reachable_addrs_changed: unknown=[63.211.255.232/udp/29538/quic-v1]
11,217ms addresses_updated: -63.211.255.232/udp/29538/quic-v1 (removed)
11,217ms reachable_addrs_changed: unreachable=[63.211.255.232/udp/29538/quic-v1]
17,158ms reachability_changed: private (AutoNAT v1)
```

## Timeline (Run 1 — oscillating)

```
0ms       started
513ms     bootstrap peer 1 connected
910ms     bootstrap peer 2 connected
1,660ms   bootstrap peer 3 connected
2,392ms   bootstrap peer 4 connected
2,457ms   bootstrap peer 5 connected, bootstrap done
5,009ms   addresses_updated: +63.211.255.232/udp/29538/quic-v1 (activated)
5,009ms   reachable_addrs_changed: unknown=[63.211.255.232/udp/29538/quic-v1]
11,210ms  addresses_updated: -63.211.255.232/udp/29538/quic-v1 (unreachable, removed)
11,210ms  reachable_addrs_changed: unreachable=[63.211.255.232/udp/29538/quic-v1]
19,040ms  reachability_changed: private
22,761ms  reachability_changed: unknown   ← oscillation begins
25,153ms  reachability_changed: private
32,912ms  reachability_changed: unknown
37,892ms  reachability_changed: private
41,210ms  reachability_changed: unknown   ← ended here (stable_wait expired)
```

## Comparison: Heathrow vs Flight vs Hotel

| Metric | Heathrow | Flight | Hotel |
|--------|----------|--------|-------|
| Network type | Airport WiFi | Satellite WiFi | Hotel WiFi |
| RTT | ~55ms | ~711ms | ~6ms |
| NAT mapping | EIM, port-preserving | Symmetric (ADPM) | EIM, port-remapping |
| TCP outbound | Open | Open | **Blocked (port 4001)** |
| UDP outbound | Open | Open | Open |
| Public address (TCP) | Yes (4001→4001) | Never | Never (TCP blocked) |
| Public address (QUIC) | Yes (4001→4001) | Never | Yes (4001→29538) |
| AutoNAT v2 used | Yes (both transports) | No (no addr activated) | Yes (QUIC only) |
| v2 result | unreachable | N/A | unreachable |
| v1 result | private (stable) | private (1/3 oscillated) | private (1/3 oscillated) |
| v1 convergence | ~17s | ~31s | ~17s |
| Bootstrap peers | 5/5 | 4/5 | 5/5 |
| Bootstrap latency | 2-3s | 10-18s | 0.2-2.5s |

## Implications

1. **TCP port blocking is common on managed networks.** Hotels, airports, and
   corporate networks often restrict outbound TCP to standard ports (80, 443).
   This means TCP-only libp2p nodes may fail to bootstrap entirely. QUIC (UDP)
   provides resilience since UDP is typically less restricted.

2. **Port-remapping EIM NAT still allows v2 activation.** Unlike symmetric NAT
   (flight WiFi), where each peer sees a different external port, the hotel NAT
   uses the same external port for all destinations (just different from internal).
   This is enough for observed address activation (ActivationThresh met).

3. **v1 oscillation occurs even at low latency.** Run 1's 6-transition oscillation
   at 6ms RTT disproves the hypothesis that v1 instability is solely caused by
   high latency. The root cause likely involves the v1 confidence window being
   sensitive to probe timing or server selection, not just network conditions.

4. **QUIC-only v2 gives correct result.** Even with only QUIC address discovery,
   v2 correctly identifies the address as unreachable. The system works as
   designed — it just can't probe TCP addresses that were never discovered.

## Raw Data Files

All raw data in `results/local/data/`:

- `local-both-hotel-wifi-20260219T140800Z.json` — Summary (3 runs)
- `local-both-hotel-wifi-20260219T140800Z-run1.jsonl` — Run 1 raw events (oscillation)
- `local-both-hotel-wifi-20260219T140800Z-run2.jsonl` — Run 2 raw events (stable)
- `local-both-hotel-wifi-20260219T140800Z-run3.jsonl` — Run 3 raw events (stable)
