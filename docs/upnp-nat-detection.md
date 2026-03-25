# UPnP and AutoNAT v2 Reachability Detection

## Overview

This document explains how UPnP (Universal Plug and Play) port mappings interact
with AutoNAT v2 reachability detection, including how port remapping affects
address probing and when detection can fail.

## Background: What AutoNAT v2 Probes

AutoNAT v2 tests individual addresses for reachability. The client announces a
set of addresses to an AutoNAT server; the server picks one and attempts to dial
back to it. If the dial-back succeeds, that address is confirmed reachable.

The addresses the client announces come from two sources:

1. **Directly observed addresses** — IP:port pairs that AutoNAT servers see
   the client come from when it connects to them (post-NAT address)
2. **NATPortMap addresses** — external addresses learned via UPnP/NAT-PMP,
   added by `libp2p.NATPortMap()`

Both sources are included in the AutoNAT v2 dial request. The server probes
whichever address it selects.

---

## Case 1: No UPnP — EIM Router (observed in testing)

A router using **Endpoint-Independent Mapping (EIM)** assigns the same external
port for outbound traffic to all destinations. This makes QUIC/UDP reachable
without any explicit port forwarding.

**Observed example:**

```
Local:   192.168.1.38:4001
Public:  79.153.193.239 (router's external IP)
Router maps: 4001 → 33611 (consistent for all destinations)
```

AutoNAT v2 results:

| Address | Result | Reason |
|---------|--------|--------|
| `/ip4/79.153.193.239/tcp/4001` | UNREACHABLE | No inbound TCP mapping |
| `/ip4/79.153.193.239/tcp/33611` | UNREACHABLE | TCP conntrack blocks new inbound |
| `/ip4/79.153.193.239/udp/4001/quic-v1` | UNREACHABLE | Original port not mapped inbound |
| `/ip4/79.153.193.239/udp/33611/quic-v1` | **REACHABLE** | EIM: same port 33611 accepts inbound |

Why QUIC works but TCP doesn't:
- **QUIC/UDP**: EIM means any source can send UDP to `79.153.193.239:33611` and
  it reaches the client at `192.168.1.38:4001`. The AutoNAT server's dial-back
  to port 33611 succeeds.
- **TCP**: The NAT's connection tracking for TCP only allows inbound from the
  exact IP:port the client contacted. A new inbound TCP connection from a
  different source port is dropped, even if port 33611 is used.

**AutoNAT v1 vs v2 here:** v1 reports `private` (conservative). v2 is more
precise — it correctly identifies QUIC port 33611 as reachable.

---

## Case 2: UPnP with Same External Port (4001 → 4001)

`libp2p.NATPortMap()` requests an explicit DNAT rule from the router:

```
UPnP mapping: external 4001 → internal 192.168.1.38:4001 (TCP + UDP)
```

The client announces `/ip4/79.153.193.239/tcp/4001` and
`/ip4/79.153.193.239/udp/4001/quic-v1`.

AutoNAT v2 probes these addresses. The router's UPnP DNAT rule forwards inbound
traffic on port 4001 to the client.

**Result: REACHABLE for both TCP and QUIC** ✓

---

## Case 3: UPnP with Different External Port (4001 → 33611)

Some routers assign a different external port for the UPnP mapping (e.g. because
port 4001 is already in use externally):

```
UPnP mapping: external 33611 → internal 192.168.1.38:4001 (TCP + UDP)
```

`NATPortMap()` learns about external port 33611 and updates the host's announced
addresses to `/ip4/79.153.193.239/tcp/33611` and
`/ip4/79.153.193.239/udp/33611/quic-v1`.

AutoNAT v2 probes port 33611. The router's UPnP DNAT rule forwards the
inbound traffic to the client.

**Result: REACHABLE for both TCP and QUIC** ✓

The port number itself does not matter — `NATPortMap()` handles announcing the
correct external port, and AutoNAT v2 probes exactly that port.

---

## Issue 3: AutoNAT v1 Does Not Recover After UPnP Port Remapping

### Observed behaviour

In a testbed run with UPnP enabled and port remapping (4001 → 57558):

```
15:12:55  Connected to AutoNAT v2 server
15:13:00  v2: probing /tcp/4001 and /udp/4001 (unknown)
15:13:11  v2: port 4001 UNREACHABLE (no inbound mapping)
15:13:12  v1: REACHABILITY CHANGED: private
15:13:14  v2: port 57558 REACHABLE (UPnP mapping active)
           — v1 never fires "public" again
```

**v2 summary:**

| elapsed | reachable | unreachable | unknown |
|---------|-----------|-------------|---------|
| 5015ms | — | — | tcp/4001, udp/4001 |
| 16276ms | — | tcp/4001, udp/4001 | tcp/57558, udp/57558 |
| 19155ms | tcp/57558, udp/57558 | tcp/4001, udp/4001 | — |

`final_reachability: public`, `time_to_first_reachable: 19155ms`

### Root cause

AutoNAT v1 and v2 are independent subsystems that do not share state.

When port 4001 failed to respond (at 16276ms), v1 fired `REACHABILITY CHANGED: private`. Once private, v1 must collect 3+ independent dial-back confirmations on the new port (57558) before transitioning back to `public`. In a short run that converges in ~20s, v1 never has time to gather those confirmations.

More importantly, **v1 has no mechanism to consume v2 results**. Even if the run lasted hours, v1's confidence cycle operates on a different schedule and peers chosen by v1 may or may not attempt port 57558.

### Consequences

When v1 fires `private` after the initial port 4001 failure:

- **NATService (v1 server)** removes itself from the protocol list — stops helping other peers test reachability
- **AutoRelay** may activate and acquire circuit relay addresses, adding overhead
- **DHT auto-server mode** may revert to client mode, reducing routing table participation

All of this happens even though v2 has confirmed the node is fully reachable via port 57558.

### Comparison with non-UPnP run

| Scenario | v1 result | v2 result | v1 fires "public"? |
|----------|-----------|-----------|-------------------|
| EIM, no UPnP (port 4001 direct) | public → private (decay) | public | Yes, early (~3s) |
| UPnP, port remapped (4001→57558) | private (never recovers) | public | No |

In the EIM case, v1 fires `public` at ~3s because port 4001 is directly reachable and a connected peer can immediately confirm. With UPnP remapping, port 4001 is unreachable so v1 fires `private` and stays there.

### Upstream gap

This is a known design gap in go-libp2p: v2 results do not feed back into the v1 global reachability flag. See [GitHub issue #60](https://github.com/probe-lab/autonat-project/issues/60) for tracking.

The correct long-term fix is for go-libp2p to derive `EvtLocalReachabilityChanged` from v2 per-address results, so that AutoRelay, NATService, and DHT use the more precise v2 signal.

---

## When Detection Can Fail

### 1. Timing race

AutoNAT v2 starts probing shortly after startup. If `NATPortMap()` has not yet
established the UPnP mapping and updated the announced addresses by the time the
first probe goes out, the probe uses the wrong address and fails.

The node re-probes periodically, so this results in a delayed correct verdict
rather than a permanent false negative. The `time_to_confidence` metric (P3.5)
captures this delay.

### 2. Router ignores UPnP requests

Many ISP-provided routers silently drop UPnP requests or have UPnP disabled by
default. `NATPortMap()` returns without error but no mapping is created. The
client announces its local address as if mapped, but inbound connections fail.

**Result: false negative** — the node is actually unreachable, and AutoNAT v2
correctly reports it as such (no false positive). But the node may keep
announcing an address it believes is mapped.

### 3. UPnP mapping expires mid-session

UPnP leases have a lifetime. If the mapping expires and is not renewed before
the next AutoNAT v2 probe cycle, the probe fails and the address transitions
from `reachable` to `unreachable`. This appears as a `time_to_update` event.

### 4. Double NAT / CGNAT

If the client is behind CGNAT (100.64.0.0/10) or a double NAT, `NATPortMap()`
may only reach the first NAT layer. The outer NAT (ISP's CGNAT) has no UPnP
and the external address is still unreachable.

`manet.IsPublicAddr()` filters CGNAT addresses as private, so AutoNAT v2 will
not probe them — correctly reporting the node as unreachable.

---

## Summary

| Scenario | TCP (v2) | QUIC (v2) | v1 global flag | Notes |
|----------|----------|-----------|---------------|-------|
| No UPnP, EIM router | ✗ | ✓ | public → private (decay) | QUIC reachable; v1 decays from DHT churn |
| No UPnP, APDF router | ✗ | ✗ | private | Port-restricted, no inbound (confirmed locally) |
| UPnP, same port | ✓ | ✓ | public | Full reachability, v1 agrees |
| UPnP, port remapped | ✓ | ✓ | **private → public (84s delay)** | v2 at ~22s; v1 needs >100s to catch up |
| UPnP, router ignores | ✗ | ✗ | private | No mapping created |
| CGNAT | ✗ | ✗ | private | Filtered by IsPublicAddr() |

## Local Test Results: Home Router (2026-03-24)

### Setup

- **Router:** Residential ISP router (port-restricted NAT)
- **Public IP:** 79.153.197.240 / 79.153.199.168 (dynamic, varies across sessions)
- **Private IP:** 192.168.1.38
- **Binary:** go-libp2p testbed node with `NATPortMap()` enabled, bootstrapping to IPFS DHT
- **Duration:** 80–300s per run
- **6 test runs** with varying UPnP/port-forwarding configurations

### Results Summary

| Trace | UPnP enabled | IPv4 QUIC bound | v2 result | v1 result |
|-------|-------------|-----------------|-----------|-----------|
| `go-upnp-debug` | Yes | Yes | **REACHABLE** (ports 27232, 57302, 57021, 38700) | PRIVATE (stuck) |
| `go-no-forwards` | Yes | Yes | **REACHABLE** (ports 35155, 17249, 51955, etc.) | PRIVATE → PUBLIC at 106s |
| `go-no-kubo` | No | Yes | UNREACHABLE (port 4001 only) | PRIVATE |
| `go-5min` | No | Yes | UNREACHABLE (port 4001 only) | PRIVATE |
| `go-upnp-working` | Yes | **No (port conflict)** | UNREACHABLE (all ephemeral) | PRIVATE |

### UPnP Working: `go-upnp-debug` (representative)

When UPnP is enabled and transports bind correctly, the NAT manager creates
explicit DNAT mappings on the router. These UPnP-mapped ports are reachable
from any source (unlike NAT-assigned ephemeral ports).

**Timeline:**

| Time | Event |
|------|-------|
| 5s | Identify observes `/tcp/4001` and `/udp/4001/quic-v1` (port reuse) |
| 15s | UPnP-mapped addresses appear: `/tcp/27232`, `/udp/27232/quic-v1` |
| 16s | v2: port 4001 UNREACHABLE (no UPnP mapping for listen port) |
| 17s | v1: PRIVATE (based on port 4001 failure) |
| ~22s | v2: port 27232 REACHABLE (both TCP and QUIC) |
| ~31s | v2: port 57302 also REACHABLE |
| ~38s | Accumulates 5 reachable addresses across TCP and QUIC |

**Probe results:**

| Address | Result | Source |
|---------|--------|--------|
| `/tcp/4001` | UNREACHABLE | Identify observation (listen port) |
| `/udp/4001/quic-v1` | UNREACHABLE | Identify observation (listen port) |
| `/tcp/27232` | **REACHABLE** | UPnP mapping |
| `/udp/27232/quic-v1` | **REACHABLE** | UPnP mapping |
| `/tcp/57302` | **REACHABLE** | UPnP mapping |
| `/udp/57302/quic-v1` | **REACHABLE** | UPnP mapping |

The router assigns **random external ports** for UPnP mappings (not the requested
port 4001). This is the port-remapping scenario from Case 3 above.

### UPnP Disabled: `go-no-kubo` (representative)

Without UPnP, only identify-observed addresses are available. With the home
router's port-restricted NAT, all addresses are UNREACHABLE:

- Port 4001 (listen port via port reuse): NAT blocks inbound from non-contacted peers
- No UPnP-mapped addresses available

### Port Conflict: `go-upnp-working` (anomalous)

In this run, IPv4 QUIC failed to bind (UDP port 4001 held by a previous process).
Only IPv6 QUIC was available (`/ip6/::1/udp/4001/quic-v1`). Without IPv4 listeners
for both TCP and QUIC, UPnP had limited transport surface. Additionally, identify-
observed ephemeral ports rotated every ~20s (33080 → 13165 → 25282 → ...) — all
UNREACHABLE due to port-restricted filtering.

### v1/v2 Gap with UPnP

The tests confirm the v1/v2 gap (Finding #1) in a real-world UPnP scenario:

| Trace | v2 first REACHABLE | v1 first PUBLIC | Gap |
|-------|-------------------|-----------------|-----|
| `go-upnp-debug` (80s run) | 22s | never | v1 stuck in PRIVATE |
| `go-no-forwards` (120s run) | 22s | 106s | **84s gap** |

v2 confirms reachability via UPnP-mapped ports at ~22s. v1 independently detects
reachability only after ~106s (if the run is long enough). In shorter runs, v1
stays PRIVATE — triggering unnecessary relay activation and DHT client mode even
though the node is reachable.

---

## Docker Testbed UPnP: Why It Doesn't Work

The testbed includes UPnP emulation via `miniupnpd` on the router container
(`upnp: true` in scenario YAML). However, UPnP scenarios **fail consistently**
(0/20 for port-restricted and symmetric NAT). Three independent root causes
prevent UPnP from working in the Docker testbed:

### 1. SSDP multicast not forwarded in Docker bridge networks

UPnP discovery uses SSDP — a multicast protocol where clients send M-SEARCH
to `239.255.255.250:1900`. Docker bridge networks **do not forward multicast
traffic** between containers. The client's SSDP discovery packets never reach
the router container's miniupnpd.

This is a fundamental Docker bridge limitation. Workarounds (macvlan, ipvlan
network drivers) would require significant testbed restructuring.

### 2. iptables-legacy vs nftables backend mismatch

The router container's NAT rules use `iptables` (nft backend, the default on
modern Alpine/Debian). miniupnpd creates its port forwarding rules using
`iptables-legacy` (the older backend). The two backends maintain **separate
rule tables** — miniupnpd's DNAT rules are invisible to the nft-based rules,
so port mappings created by miniupnpd have no effect on actual packet
forwarding.

Even when miniupnpd accepts a UPnP AddPortMapping SOAP request, the resulting
iptables-legacy rule doesn't interact with the nft NAT rules that control
actual traffic flow.

### 3. NAT-PMP unicast should work but doesn't

go-libp2p's `go-nat` library supports NAT-PMP (unicast to gateway, no
multicast needed). miniupnpd is configured with `enable_natpmp=yes`.
Direct UDP testing confirms the router responds to NAT-PMP requests on
port 5351. However, `go-nat`'s discovery sequence (SSDP first, then
NAT-PMP fallback) does not reliably reach the NAT-PMP path — possibly
due to timeout handling or the SSDP failure mode not triggering the
fallback correctly. This needs further investigation.

### Testbed Results

| Scenario | Result | Explanation |
|----------|--------|-------------|
| `upnp-port-restricted` | 0/20 FAIL | UPnP not discovered |
| `upnp-symmetric` | 0/20 FAIL | UPnP not discovered |
| `upnp-address-restricted` | 20/20 PASS | **False positive** — ADF allows dial-back from contacted IPs regardless of UPnP |

### Conclusion

UPnP cannot be reliably tested in the Docker testbed due to Docker bridge
networking limitations and iptables backend conflicts. **Local testing on a
real home router** (see [above](#local-test-results-home-router-2026-03-24))
provides the necessary evidence for UPnP behavior. Static port forwarding
(`port_forward: true`) works correctly in the testbed for all NAT types
(140/140 PASS) and tests the same AutoNAT detection path that UPnP uses
once a mapping exists.

See [#92](https://github.com/probe-lab/autonat-project/issues/92) for
tracking.
