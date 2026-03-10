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

| Scenario | TCP | QUIC | Notes |
|----------|-----|------|-------|
| No UPnP, EIM router | ✗ | ✓ | QUIC works due to UDP EIM |
| No UPnP, APDF router | ✗ | ✗ | Port-restricted, no inbound |
| UPnP, same port | ✓ | ✓ | Full reachability |
| UPnP, different port | ✓ | ✓ | NATPortMap announces correct port |
| UPnP, router ignores | ✗ | ✗ | No mapping created |
| CGNAT | ✗ | ✗ | Filtered by IsPublicAddr() |

## Testbed Reproduction

The testbed supports UPnP emulation via `miniupnpd` on the router container.
Set `upnp: true` in a scenario to enable it:

```yaml
scenarios:
  - name: port-restricted-upnp
    nat_type: port-restricted
    transport: both
    server_count: 7
    upnp: true
    assertions:
      - type: has_event
        event: reachable_addrs_changed
        filter: {not_empty: reachable}
        message: "Node should be reachable via UPnP mapping"
```

Run with:
```bash
./testbed/run.sh testbed/scenarios/your-scenario.yaml
```
