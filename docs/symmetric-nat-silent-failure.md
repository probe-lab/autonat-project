# Finding 4: Symmetric NAT Silent Failure — Deep Dive

Under symmetric NAT (ADPM), all three libp2p implementations fail to
produce a timely reachability signal. The root causes differ across
implementations: go-libp2p's activation threshold prevents AutoNAT v2
from running, js-libp2p either excludes addresses entirely (TCP) or
probes them silently (QUIC), and rust-libp2p is the only one that
correctly produces UNREACHABLE.

**Key context:** Nodes behind symmetric NAT are **definitively
unreachable** — the practical outcome is the same whether the signal is
"unknown" or "unreachable." There is no false positive. The impact of
this finding is operational (missing relay activation, no observability,
futile hole punching), not functional. See [Real-World Impact](#real-world-impact)
for details.

This document traces the exact code paths in each implementation.

---

## Background: Why Symmetric NAT Is Different

Under symmetric NAT (Address- and Port-Dependent Mapping), each
outbound connection is assigned a different external port:

```
Client → Peer A  →  NAT maps to 203.0.113.50:54321
Client → Peer B  →  NAT maps to 203.0.113.50:54322
Client → Peer C  →  NAT maps to 203.0.113.50:54323
```

Each mapping only accepts return traffic from the specific destination
it was created for. A dial-back from an arbitrary peer to any of these
ports will be dropped by the NAT.

Under cone NAT (Endpoint-Independent Mapping), all connections share the
same external port:

```
Client → Peer A  →  NAT maps to 203.0.113.50:4001
Client → Peer B  →  same: 203.0.113.50:4001
Client → Peer C  →  same: 203.0.113.50:4001
```

This distinction is the root of the problem: AutoNAT v2 relies on a
third party dialing back to an observed address, which structurally
cannot work for symmetric NAT ephemeral ports.

---

## go-libp2p: Threshold Blocks, Detection Exists but Unwired

### The activation threshold gate

go-libp2p's `ObservedAddrManager` requires an address to be reported by
at least `ActivationThresh=4` independent observers (distinct IPs for
IPv4, distinct `/56` prefixes for IPv6) before it becomes a candidate
for AutoNAT v2 probing.

Under symmetric NAT, each peer observes a different `ip:port`. No
single address ever accumulates 4 observations. Result: **AutoNAT v2
never runs**, and the node stays in "unknown" permanently.

Under cone NAT, every peer observes the same `ip:port`, so the
threshold is reached after 4 connections and AutoNAT v2 probes begin.

### `getNATType()` detects symmetric NAT correctly

The `ObservedAddrManager` has a `getNATType()` function that classifies
NAT mapping behavior by comparing observed ports across peers. It runs
on a 60-second ticker and requires ≥9 total observations before
classifying.

The algorithm groups observed addresses by local address and counts how
many peers observed each external address. If the top addresses account
for >50% of observations, the NAT is `EndpointIndependent` (cone). If
observations are dispersed across many different ports, it's
`EndpointDependent` (symmetric).

When the classification changes, it emits `EvtNATDeviceTypeChanged`
with the detected `NATDeviceType` and `TransportProtocol`.

**Testbed confirmed:** With 7 servers and 60s observation, `getNATType()`
correctly classifies cone NAT as `EndpointIndependent`.

### Nobody subscribes to `EvtNATDeviceTypeChanged`

The event is emitted but has **zero subscribers** anywhere in the
go-libp2p ecosystem:

- **go-libp2p** — DHT, AutoRelay, holepunch, AutoNAT: none subscribe
- **Kubo** — zero references to the event
- **go-libp2p-kad-dht** — zero references

The event type is defined in `core/event/nattype.go` and emitted in
`p2p/host/observedaddrs/manager.go`. The only other references are in
test files (`identify/metrics_test.go`, `eventbus/basic_metrics_test.go`).

This means go-libp2p correctly detects symmetric NAT at ~60s but does
nothing with that information. The detection is a wiring gap, not a
detection gap.

### Proposed fix

Wire `EvtNATDeviceTypeChanged{EndpointDependent}` into either:

1. **Lowering `ActivationThresh`** — Allow AutoNAT v2 to run with
   threshold=1, which testbed confirms produces correct UNREACHABLE.
   The security tradeoff is small: observer-IP deduplication means a
   single attacker IP can only contribute 1 observation regardless of
   sybil count.

2. **Emitting UNREACHABLE directly** — When `EndpointDependent` is
   detected, emit an UNREACHABLE signal without waiting for AutoNAT v2.

See [#89](https://github.com/probe-lab/autonat-project/issues/89).

### UPnP note

UPnP-mapped addresses bypass `ObservedAddrManager` entirely — they
enter the reachability tracker via `appendNATAddrs()`, skipping the
activation threshold. However, **this is not relevant for real-world
symmetric NAT:**

- **CGNAT** (the most common source of symmetric NAT) is operated by
  ISPs. Users have no administrative access, no UPnP, and no ability
  to configure port forwarding.
- **Mobile carrier NAT** — same situation, carrier-controlled.
- **Enterprise/corporate NAT** — admin-controlled, UPnP typically
  disabled.

The UPnP bypass was confirmed on a **port-restricted (cone) NAT** home
router, not a symmetric NAT device. Consumer routers that support UPnP
almost always use EIM (cone NAT), not ADPM (symmetric). The scenario
where UPnP and symmetric NAT coexist on the same device is a
theoretical edge case with no known real-world deployment.

---

## rust-libp2p: Works Correctly (No Threshold)

rust-libp2p has **no equivalent** to go-libp2p's `getNATType()` or
`EvtNATDeviceTypeChanged`. There is no NAT type classification at all.

It also has **no activation threshold**. A single
`NewExternalAddrCandidate` event from Identify immediately makes an
address a probe candidate. The AutoNAT v2 client tests it, the
dial-back fails (symmetric NAT drops inbound from unknown peers), and
the result is correctly reported as UNREACHABLE.

rust-libp2p gets the right answer without NAT type detection — because
it just probes everything and lets the network decide.

### Comparison with go-libp2p

| Aspect | go-libp2p | rust-libp2p |
|--------|-----------|-------------|
| NAT type detection | `getNATType()` at ~60s | None |
| Activation threshold | 4 observers required | None (single report) |
| Symmetric NAT result | NO SIGNAL | **UNREACHABLE** (correct) |
| Detection mechanism | Event emitted, nothing subscribes | Probes run, dial-back fails |

---

## js-libp2p: Two Distinct Failure Modes

js-libp2p has no activation threshold like go-libp2p and no NAT type
detection. The failure under symmetric NAT comes from two different
mechanisms depending on transport.

### TCP: Total exclusion in Identify (Node.js platform limitation)

In `packages/protocol-identify/src/identify.ts`, the
`maybeAddObservedAddress()` method unconditionally drops all TCP
observed addresses:

```typescript
if (TCP.exactMatch(cleanObservedAddr)) {
  // TODO: because socket dials can't use the same local port as the TCP
  // listener, many unique observed addresses are reported so ignore all
  // TCP addresses until https://github.com/libp2p/js-libp2p/issues/2620
  // is resolved
  return
}
```

> **Important:** This is a **Node.js platform limitation**, not a
> symmetric NAT issue. Node.js TCP sockets lack `SO_REUSEPORT` support,
> so every outbound TCP dial uses an ephemeral source port — producing
> many unique observed addresses regardless of NAT type. The js-libp2p
> team disabled TCP observed addresses entirely as a workaround. This
> affects all NAT types equally, but makes the symmetric NAT situation
> worse because there is no alternative path (UPnP can help for cone
> NATs but not symmetric). See
> [js-libp2p-autonat-implementation.md § Known Issue #7](js-libp2p-autonat-implementation.md#7-tcp-observed-address-exclusion-nodejs-platform-limitation)
> for the full analysis.

### QUIC: Addresses enter but verification is structurally impossible

QUIC observed addresses pass the Identify filter and reach the address
manager. The pipeline:

1. **`addObservedAddr()`** — Each unique `ip:port` passes the Cuckoo
   filter (they're all distinct strings). Under symmetric NAT, each
   connection produces a new address.

2. **`observed.add()`** — Addresses are stored with `verified: false`
   and `expires: 0`. However, there is a hard capacity limit of
   `maxObservedAddresses=10`. The first 10 unique addresses fill the
   buffer; the rest are silently dropped.

3. **`getUnverifiedMultiaddrs()`** — AutoNAT v2 pulls these as
   candidates. Since `expires: 0 < Date.now()` is always true, they
   pass the expiry filter.

4. **Dial-back fails** — AutoNAT v2 asks a peer to dial back to (e.g.)
   `203.0.113.50:54321`. The symmetric NAT drops the inbound packet
   (the mapping only accepts traffic from the original destination).
   The dial always fails.

5. **After 8 failures, removal** — `REQUIRED_FAILED_DIALS=8` triggers
   `unconfirmAddress()` → `addressManager.removeObservedAddr()`. The
   address is deleted.

6. **Cuckoo filter prevents retries** — The address string is recorded
   in the AutoNAT client's `addressFilter`, preventing the same address
   from being tested again.

7. **No events emitted** — js-libp2p's AutoNAT v2 emits no reachability
   events to consumers. The dial-back failures are tracked internally
   but never surface as an UNREACHABLE signal. The application sees
   nothing.

### Why cone NAT succeeds in js-libp2p

Under cone NAT with QUIC, every peer observes the same `ip:port`:

- Only 1 slot is consumed in the observed address buffer (not 10)
- The Cuckoo filter in `addObservedAddr()` deduplicates repeat
  observations, which is fine — one entry is enough
- AutoNAT v2 dial-back succeeds (cone NAT forwards inbound from any
  source)
- After `REQUIRED_SUCCESSFUL_DIALS=4` confirmations from different `/8`
  network segments, the address is confirmed
- `confirmObservedAddr()` marks it `verified: true` with a TTL
- The peer store update triggers `self:peer:update`, which the DHT
  listens to for mode switching

### Proposed fix

1. **Emit reachability events** — Surface AutoNAT v2 probe results
   (including failures) to consumers, so applications and the DHT can
   act on UNREACHABLE determinations.
2. **Resolve the TCP exclusion** — Address
   [js-libp2p#2620](https://github.com/libp2p/js-libp2p/issues/2620)
   to allow TCP observed addresses into the pipeline.

---

## Cross-Implementation Summary

| | go-libp2p | rust-libp2p | js-libp2p (TCP) | js-libp2p (QUIC) |
|-|-----------|-------------|-----------------|------------------|
| **Addresses enter pipeline?** | No (threshold blocks) | Yes (no threshold) | No (excluded in Identify — affects all NAT types) | Yes (passes filters) |
| **AutoNAT v2 runs?** | No | Yes | No | Yes |
| **Dial-back result** | N/A | Fails → UNREACHABLE | N/A | Fails → removed silently |
| **NAT type detected?** | Yes (`EndpointDependent` at ~60s) | No | No | No |
| **Detection acted on?** | No (no subscribers) | N/A | N/A | N/A |
| **Application signal** | NO SIGNAL | UNREACHABLE (correct) | NO SIGNAL | NO SIGNAL |
| **Root cause** | Activation threshold + unwired event | — | TCP exclusion in Identify | Structural verification failure + no events |

---

## Real-World Impact

Nodes behind symmetric NAT are **definitively unreachable** by
definition. ADPM means each outbound connection gets a unique external
port that only accepts return traffic from the specific destination —
no arbitrary peer can dial in. In the real world, symmetric NAT is
almost exclusively CGNAT (ISP-operated) or mobile carrier NAT, where
users have no administrative access to configure UPnP or port
forwarding.

This means the practical outcome — the node cannot serve DHT queries
and cannot accept direct inbound connections — is the same whether
the implementation reports "unknown" or "unreachable." **There is no
false positive:** the system never incorrectly claims a symmetric NAT
node is reachable.

The impact of the missing signal is operational, not functional:

1. **Missing relay activation.** In go-libp2p, AutoRelay subscribes to
   `EvtLocalReachabilityChanged` and activates on `Private`. The
   `Unknown` state does not trigger relay reservation, so the node may
   lack a relay fallback path entirely — degrading connectivity for
   peers that could otherwise reach it through a relay. This also
   prevents DCUtR (hole punching), which depends on relay connections
   being established first.

2. **No observability.** Operators cannot distinguish "AutoNAT still
   converging" from "definitively behind symmetric NAT." There is no
   metric or event to diagnose the situation. This is particularly
   problematic for long-running nodes where "still converging" is
   clearly wrong but indistinguishable from the symmetric NAT case.

---

## Testbed Evidence

### Baseline (default threshold)

| NAT type | Transport | Result | Probes |
|----------|-----------|--------|--------|
| symmetric | TCP | NO SIGNAL | 0 |
| symmetric | QUIC | NO SIGNAL | 0 |

### Threshold sensitivity

| Threshold | NAT type | Result |
|-----------|----------|--------|
| 4 (default) | symmetric | NO SIGNAL |
| 2 | symmetric | NO SIGNAL |
| 1 | symmetric | **UNREACHABLE** (correct) |

With `ActivationThresh=1`, go-libp2p correctly determines UNREACHABLE
for symmetric NAT. This confirms the threshold is the gate, not a
protocol-level limitation.

### Under latency and packet loss

All symmetric NAT scenarios produce NO SIGNAL regardless of network
conditions:

| Condition | Result |
|-----------|--------|
| 200ms latency | NO SIGNAL |
| 500ms latency | NO SIGNAL |
| 1% packet loss | NO SIGNAL |
| 5% packet loss | NO SIGNAL |
| 10% packet loss | NO SIGNAL |

### Port forwarding toggle detection

| NAT type | Forward added | Forward removed |
|----------|---------------|-----------------|
| port-restricted | Detected (~30s) | Detected (~69s) |
| symmetric | NOT detected (180s) | NOT detected (180s) |

Symmetric NAT nodes cannot detect reachability changes from port
forwarding because AutoNAT v2 never runs (go-libp2p) or the probes
fail before toggling occurs.

### UPnP (local home router — port-restricted, NOT symmetric)

The local UPnP tests were run on a **port-restricted (cone) NAT** home
router, not a symmetric NAT device:

| Config | v2 result | Time |
|--------|-----------|------|
| UPnP enabled, QUIC bound | REACHABLE | ~22s |
| UPnP disabled | UNREACHABLE | — |

This confirms UPnP-mapped addresses bypass the `ObservedAddrManager`
threshold in go-libp2p. However, this is **not applicable to real-world
symmetric NAT** — CGNAT and mobile carrier devices do not support UPnP.
See [UPnP note](#upnp-note) above.

---

## References

- [Final Report — Finding #4](final-report.md#finding-4-symmetric-nat-silent-failure)
- [go-libp2p AutoNAT Implementation](go-libp2p-autonat-implementation.md)
- [js-libp2p AutoNAT Implementation](js-libp2p-autonat-implementation.md)
- [rust-libp2p AutoNAT Implementation](rust-libp2p-autonat-implementation.md)
- [UPnP and NAT Detection](upnp-nat-detection.md)
- [Measurement Results](measurement-results.md)
- [GitHub issue #89](https://github.com/probe-lab/autonat-project/issues/89)
- [js-libp2p#2620 — TCP observed address exclusion](https://github.com/libp2p/js-libp2p/issues/2620)
