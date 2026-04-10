# Cross-Implementation Comparison: AutoNAT v2 in go-libp2p, rust-libp2p, and js-libp2p

This document collects the cross-implementation evidence that backs the
AutoNAT v2 final report. It is intentionally separate from the main
findings (F1–F5) because it is a survey, not a finding with a single
fix — the differences across implementations are the *symptom*; the
root cause is that the AutoNAT v2 spec standardizes only the wire
protocol, not the surrounding behaviors (activation thresholds,
address selection, event surface, port translation, DHT wiring).

For the proposed fix to that root cause, see
[final-report.md → Recommendations](final-report.md#recommendations).

---

## Summary

The AutoNAT v2 protocol works correctly when the implementation is
right (go-libp2p: 0% FNR/FPR in testbed). However, each implementation
has distinct limitations in the surrounding infrastructure — address
management, event model, platform constraints, and production readiness.
These differences mean that real-world reachability behavior varies
significantly depending on which libp2p implementation a node runs.

---

## Findings Matrix

How each main-report finding manifests in each implementation:

| Issue | go-libp2p | rust-libp2p | js-libp2p |
|-------|-----------|-------------|-----------|
| **Inconsistent global vs per-address reachability** (F1) | **Yes** — DHT/AutoRelay consume v1's `EvtLocalReachabilityChanged` and are blind to v2. Both protocols use the same server pool, but v1 counts timeouts as evidence (causing oscillation in 60% of runs with unreliable servers — no malicious peers needed) while v2 discards them entirely. | Not affected today — DHT consumes `ExternalAddrConfirmed` (v2-fed), but no documented reduction if both v1 and v2 ran | Not affected today — DHT consumes `self:peer:update` (v2-fed), but Helia uses v1 only and no documented reduction either |
| **UDP black hole blocks QUIC** (F2) | **Yes** — `dialerHost` shares counter, blocks fresh servers | Not affected — no black hole detector | Not affected — no black hole detector |
| **ADF false positive** (F3) | **Yes** — 100% FPR | **Yes** — 100% FPR | **Yes** — 100% FPR |
| **Symmetric NAT missing signal** (F4) | **Yes** — activation threshold blocks v2; `getNATType()` detects but unwired | Not affected — no threshold, produces UNREACHABLE | **Yes** — TCP excluded, QUIC fails silently, no events emitted |
| **TCP port reuse failure** (F5) | Not affected — `ObservedAddrManager` corrects ports | **Yes** — silent fallback to ephemeral port, probes wrong address | Not applicable — TCP observed addrs dropped entirely |
| **TCP observed addr exclusion** | Not affected — port reuse works | Not affected — port reuse works | **Yes** — Node.js lacks `SO_REUSEPORT`; all TCP observations dropped in Identify ([#2620](https://github.com/libp2p/js-libp2p/issues/2620)) |
| **UPnP → AutoNAT → DHT chain** | Works but unstable (v1 oscillation) | **Best architecture** — v2 feeds DHT directly, no oscillation | UPnP library fails on some routers; browser-first design |
| **Reachability events for consumers** | `EvtHostReachableAddrsChanged` (v2), `EvtLocalReachabilityChanged` (v1) | Per-probe `Event` struct | **None** — no events emitted |
| **Bootstrap connectivity (IPFS DHT)** | 3/4 peers reachable | 1/4 peers (needs `rsa` feature, no WSS, QUIC cert issues) | Not tested |

---

## Feature Comparison

Key architectural differences. For full details see the per-implementation
docs: [go-libp2p](go-libp2p-autonat-implementation.md),
[rust-libp2p](rust-libp2p-autonat-implementation.md),
[js-libp2p](js-libp2p-autonat-implementation.md).

| Feature | go-libp2p | rust-libp2p | js-libp2p |
|---------|-----------|-------------|-----------|
| **v2 → DHT wiring** | No (DHT reads v1 only) | Yes (`ExternalAddrConfirmed` from v2 via swarm) | Yes (`self:peer:update`) |
| **Reachability events** | `EvtHostReachableAddrsChanged` (v2), `EvtLocalReachabilityChanged` (v1) | Per-probe `Event` struct | **None** |
| **Confidence system** | Sliding window of 5, targetConfidence=3 | Single probe, no accumulation | Fixed thresholds (4/8), monotonic counters + TTL |
| **Failure handling** | v1: timeouts erode confidence; v2: discarded | Errors ignored | Failures increment counter toward threshold |
| **Black hole detection** | Yes (causes F2) | No | No |
| **Dial-back identity** | Separate `dialerHost` | Same swarm | Same identity |

---

## Local UPnP Test Results (Home Router, 2026-03-27)

Cross-implementation testing on a real residential router (port-restricted
NAT, UPnP enabled) with 3 runs per implementation:

| Metric | go-libp2p | rust-libp2p | js-libp2p |
|--------|-----------|-------------|-----------|
| UPnP port mapping | OK | OK | **FAIL** (`Service not found`) |
| UPnP library | `go-nat` | `igd-next` | `@achingbrain/nat-port-mapper` |
| Time to first reachable (median) | 14.3s | 5.7s | N/A (timeout) |
| TCP reachable | Yes (3/3) | Yes (3/3) | No |
| QUIC reachable | No (router bug) | No (router bug) | No |
| v2 → DHT mode | Unstable (v1 oscillation) | Stable Server mode | N/A |

The same router, same NAT, same UPnP — three different outcomes.
rust-libp2p has the cleanest architecture (v2 feeds DHT directly, no
oscillation), but go-libp2p is the only one proven in production.
js-libp2p's UPnP failure is expected given its browser-first design.

Full analysis: [upnp-nat-detection.md](upnp-nat-detection.md#cross-implementation-local-upnp-tests-2026-03-27)

---

## Platform Limitations

| Limitation | Cause | Impact |
|-----------|-------|--------|
| js-libp2p: no TCP reachability via Identify | Node.js `net.createConnection()` lacks `SO_REUSEPORT` | TCP address discovery depends entirely on UPnP or manual config |
| js-libp2p: UPnP incompatible with some routers | `@achingbrain/nat-port-mapper` service discovery failure | No external address learned; node stays unreachable |
| js-libp2p: browser-first design | No raw TCP/UDP sockets in browsers | UPnP, NAT traversal, port reuse are structurally irrelevant for the primary use case |
| rust-libp2p: IPFS bootstrap fragility | Missing `rsa` feature, no WSS transport, QUIC cert validation | Delays peer discovery; AutoNAT v2 has fewer probers |

---

## v2 Availability vs. Production Deployment

AutoNAT v2 is **implemented in all three libp2p libraries** but
**deployed in production by only two Go projects:**

| Implementation | v2 available? | How to enable | v2 in production? |
|---|---|---|---|
| go-libp2p | Yes | Explicit: `EnableAutoNATv2()` | **Kubo + Pactus** |
| rust-libp2p | Yes (compiled by default with `autonat` feature) | Wire `v2::client::Behaviour` + `v2::server::Behaviour` into swarm | **No** — projects use v1 re-export |
| js-libp2p | Yes (separate `@libp2p/autonat-v2` package) | Import and add to services | **No** — Helia uses v1 |

Rust projects that enable `autonat` (Forest, Pathfinder, Ceramic) get
v2 compiled in (`default = ["v1", "v2"]` in `libp2p-autonat`), but
none wire v2 behaviours into their swarm. The crate's `pub use v1::*`
re-export means v1 is what projects get by default. Note: even though
v1 is compiled in, rust-libp2p's DHT does **not** consume v1's events
— it consumes `ExternalAddrConfirmed` from v2 via swarm broadcasts,
which is why the F1 wiring gap does not affect rust-libp2p.

| Project | Language | AutoNAT status | v2 deployed? |
|---------|----------|---------------|--------------|
| **Kubo** | Go | v1 + v2 (both active) | **Yes** — 0% FNR/FPR |
| **Pactus** | Go | v2 (explicit `EnableAutoNATv2()`) | **Yes** |
| **Lotus/Forest/Venus** | Go/Rust | v1 only | No |
| **Helia** | JS | v1 only (`@libp2p/autonat`) | No |
| **Substrate** | Rust | Disabled entirely | No |
| **Avail** | Rust | **Disabled** (v1.13.2) | No |
| **Lighthouse** | Rust | No autonat (UPnP only, sigp fork) | No |

See [libp2p-autonat-ecosystem.md](libp2p-autonat-ecosystem.md) for the
full survey of ~25 projects.

---

## Related documents

- [final-report.md](final-report.md) — main findings and recommendations
- [rust-libp2p-autonat-implementation.md](rust-libp2p-autonat-implementation.md)
- [js-libp2p-autonat-implementation.md](js-libp2p-autonat-implementation.md)
- [go-libp2p-autonat-implementation.md](go-libp2p-autonat-implementation.md)
- [v1-v2-analysis.md](v1-v2-analysis.md) — state transitions, wiring gap, fix options
- [upnp-nat-detection.md](upnp-nat-detection.md) — UPnP cross-implementation tests
- [libp2p-autonat-ecosystem.md](libp2p-autonat-ecosystem.md) — full ecosystem survey
