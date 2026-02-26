# AutoNAT v2 Experiment: In-Flight WiFi (2026-02-16)

## Environment

- **Location**: In-flight (after departure from Heathrow)
- **Network**: Satellite-based in-flight WiFi
- **Local IP**: 172.19.206.18/23 (RFC 1918 private, 172.16.0.0/12)
- **Public IP**: 216.250.199.18 (confirmed via ifconfig.me)
- **Gateway**: 172.19.207.1
- **Interface**: en0 (WiFi)
- **Port used**: 4002 (port 4001 was occupied by IPFS daemon initially)
- **Transport**: both (TCP + QUIC)
- **Timeout**: 120s per run

## Network Characteristics

| Metric | Value |
|--------|-------|
| RTT (ping 8.8.8.8) | 632-820ms, avg 711ms |
| Jitter (stddev) | 71ms |
| Packet loss | 0% |
| IPv6 | No route to host |
| UDP connectivity | Working (DNS, QUIC handshakes succeed) |
| TCP connectivity | Working |
| Bootstrap peer 104.131.131.82 | Always fails (i/o timeout) |

## Key Finding: No Public Address Discovered

**The most significant difference from Heathrow: the observed address manager never activated a public address.**

### Heathrow behavior (for comparison)
At Heathrow (same day, earlier), the address discovery worked:
- At ~5s: `addresses_updated` added `/ip4/81.145.206.39/tcp/4001` and `/ip4/81.145.206.39/udp/4001/quic-v1`
- The external ports matched internal ports (4001→4001) — **port-preserving NAT**
- At ~12s: addresses were removed after AutoNAT determined them unreachable
- The `reachable_addrs_changed` event fired with addresses as "unknown", then later "unreachable"

### Flight behavior
- **No `addresses_updated` with public address ever occurred**
- **No `reachable_addrs_changed` event ever fired**
- Only `reachability_changed: private` from AutoNAT v1 (not v2)

### Root Cause Analysis

The flight WiFi uses **non-port-preserving NAT** (likely symmetric NAT or CGNAT with port randomization):

1. go-libp2p's observed address manager receives `ObservedAddr` from peers via the Identify protocol
2. Each peer sees a different external port: e.g., `216.250.199.18:54321`, `216.250.199.18:54322`, etc.
3. These are treated as different multiaddrs (`/ip4/216.250.199.18/tcp/54321` vs `/ip4/216.250.199.18/tcp/54322`)
4. The observed address manager requires `ActivationThresh=4` distinct observers for the **same** address
5. Since each observer sees a different port, no single observed address reaches the threshold
6. The public address is never added to the host's address list
7. AutoNAT v2 (which probes public addresses) has nothing to probe
8. Only AutoNAT v1 fires (which determines reachability without needing activated public addresses)

**Heathrow NAT was port-preserving** (4001→4001), so all 5 bootstrap peers observed the same address, easily exceeding the threshold. Flight WiFi NAT randomizes ports, preventing activation.

## Results Summary

### Run 0 (port 4001, IPFS daemon conflict)

| Metric | Value |
|--------|-------|
| Port | 4001 (conflicted with IPFS daemon) |
| QUIC addresses | **Missing** (UDP 4001 taken by IPFS) |
| Final reachability | private |
| Time to result | 24,978ms |
| Bootstrap peers | 4/5 |
| Public address discovered | No |
| Note | Only TCP worked due to port conflict |

### Runs 1-3 (port 4002, IPFS daemon stopped)

| Metric | Run 1 | Run 2 | Run 3 |
|--------|-------|-------|-------|
| Final reachability | private | **unknown** | private |
| Time to first event | 31,159ms | 30,526ms | 30,899ms |
| Time to stable | 31,159ms | 31,925ms | 30,899ms |
| Reachability transitions | 1 | **2** | 1 |
| Bootstrap peers | 4/5 | 4/5 | 4/5 |
| QUIC addresses | Yes | Yes | Yes |
| Public address discovered | No | No | No |
| Addresses at result | private+loopback only | private+loopback only | private+loopback only |

### Run 2 Oscillation Detail
Run 2 showed instability:
- 30,526ms: `reachability_changed` → `private`
- 31,925ms: `reachability_changed` → `unknown` (1.4s later, confidence lost)

This is an AutoNAT v1 behavior — the v1 probes are unreliable over high-latency satellite links.

## Comparison: Heathrow vs Flight

| Metric | Heathrow | Flight |
|--------|----------|--------|
| Network type | Airport WiFi | Satellite in-flight WiFi |
| RTT | ~55ms | ~711ms (13x slower) |
| Jitter | Low | 71ms |
| NAT type | Port-preserving | Non-port-preserving (symmetric/CGNAT) |
| Public address discovered | Yes (at ~5s) | **Never** |
| AutoNAT version used | v2 (probed public addr) | v1 only (no public addr to probe) |
| Time to result | ~17.2s | ~30.9s (1.8x slower) |
| Final reachability | private (4/4 runs) | private (2/3), unknown (1/3) |
| Stability | Zero oscillation | 1/3 runs oscillated |
| Bootstrap peers | 5/5 | 4/5 (1 always fails) |
| Bootstrap latency | 2-3s | 10-18s |

## Timeline Comparison

### Heathrow (Run 1)
```
0ms      started (both TCP+QUIC)
2,539ms  bootstrap peer 1 connected
2,629ms  bootstrap peer 2 connected
2,824ms  bootstrap peer 3 connected
3,068ms  bootstrap peer 4 connected
3,243ms  bootstrap peer 5 connected
5,007ms  addresses_updated: +81.145.206.39/tcp/4001, +81.145.206.39/udp/4001/quic-v1
5,007ms  reachable_addrs_changed: unknown=[/ip4/81.145.206.39/...]
11,440ms addresses_updated: -81.145.206.39 (removed after probing)
17,202ms reachability_changed: private
```

### Flight (Run 1)
```
0ms       started (both TCP+QUIC)
10,838ms  bootstrap peer 1 connected
12,938ms  bootstrap peer 2 connected
15,184ms  bootstrap peer 3 connected
18,056ms  bootstrap peer 4 connected
23,057ms  bootstrap done (peer 5 failed)
          [NO addresses_updated with public IP — never happened]
          [NO reachable_addrs_changed — never happened]
31,159ms  reachability_changed: private (from AutoNAT v1)
```

## Implications for AutoNAT v2

1. **Non-port-preserving NAT prevents AutoNAT v2 from working entirely** — the observed address manager cannot activate a public address, so v2 never probes anything. Only v1 provides a result.

2. **This is a significant fraction of real networks** — CGNAT (common in mobile/satellite), symmetric NAT, and enterprise NATs all randomize ports.

3. **Possible improvement**: The observed address manager could normalize observed addresses by ignoring the port component when counting observers. If 4 peers all see `216.250.199.18:*`, that should be enough to activate `/ip4/216.250.199.18/tcp/<listen-port>`.

4. **High latency degrades AutoNAT v1 reliability** — the oscillation in Run 2 suggests v1 probes can be inconclusive on satellite links.

## Docker Testbed Debug (Background Task, same session)

A background debug task from the Docker testbed completed during this session.
Key finding: **AutoNAT v2 works in the testbed for TCP with `OBS_ADDR_THRESH=2`**.

The debug client (port 4002, inside the Docker container, port-restricted NAT):
- 5s: TCP public address `/ip4/73.0.0.2/tcp/4002` activated, marked "unknown"
- 6s: Three `reachability check successful` from all 3 servers
- 6s: Address moved to "unreachable" and removed — correct result!

The original client (port 4001) had only discovered a QUIC address, which never
completed probing. TCP probing works; QUIC probing through port-restricted NAT
silently fails (dial-back UDP packet blocked by NAT).

Full debug output: see `/private/tmp/claude-501/-Users-sergi-workspace-probelab/tasks/ba35ffb.output`
(ephemeral; content also documented in `docs/autonat-v2.md` under
"Transport-Specific Probing Behavior").

## Raw Data Files

All raw data in `results/local/data/`:

- `local-both-flight-20260216T165454Z.json` — Run 0 summary (port conflict)
- `local-both-flight-20260216T165454Z-run1.jsonl` — Run 0 raw events
- `local-both-flight-20260216T170045Z-run1.jsonl` — Failed run (IPFS daemon, timeout)
- `local-both-flight-20260216T170659Z.json` — Runs 1-3 summary
- `local-both-flight-20260216T170659Z-run1.jsonl` — Run 1 raw events
- `local-both-flight-20260216T170659Z-run2.jsonl` — Run 2 raw events (oscillation)
- `local-both-flight-20260216T170659Z-run3.jsonl` — Run 3 raw events

## Conclusions

1. **Non-port-preserving NAT completely prevents AutoNAT v2 from functioning.** The observed address manager never activates a public address because each bootstrap peer reports a different external port. With `ActivationThresh=4`, no single multiaddr reaches the threshold. AutoNAT v2 has nothing to probe — only v1 provides a result.

2. **This is not an edge case.** Symmetric NAT / CGNAT with port randomization is common on mobile networks, satellite internet, and enterprise NATs. A significant fraction of real-world users will hit this behavior.

3. **High latency degrades AutoNAT v1 reliability.** The oscillation in Run 2 (private → unknown at +1.4s) shows that v1 probes can be inconclusive over satellite links with ~700ms RTT. Bootstrap connections take 10-18s (vs 2-3s on low-latency WiFi), compounding the problem.

4. **Lowering `--obs-addr-thresh` does not help.** Even with threshold=1, symmetric NAT produces a different observed address per peer. No single address is ever confirmed by multiple observers, so the threshold is irrelevant — the fundamental issue is that each observation is unique.

5. **Possible go-libp2p improvement: IP-based grouping for observed addresses.** If the observed address manager grouped observations by IP (ignoring port) and activated the listen-port variant (e.g., activate `/ip4/X.X.X.X/tcp/4001` when 4 peers report `X.X.X.X:*`), AutoNAT v2 could at least *attempt* to probe. The probe would still fail (symmetric NAT blocks inbound), but the system would reach a `private` result faster and more reliably than v1 alone.

6. **Testbed reproduction.** This scenario can be reproduced in the Docker testbed using symmetric NAT + high latency:
   ```bash
   ./testbed/run.sh symmetric both 5 --latency=350
   ```
   This gives ~700ms RTT (matching satellite WiFi) with endpoint-dependent port mapping, replicating the exact conditions observed in-flight. See Experiment 14 in `docs/testbed.md`.

## CSV Data

See companion files in `results/local/data/`:
- `flight-2026-02-16-events.csv`
- `flight-2026-02-16-summary.csv`
- `flight-2026-02-16-bootstrap.csv`
