# AutoNAT v2: Protocol Analysis and Confirmed Issues

**Date:** 2026-02-23
**Protocol:** AutoNAT v2 (`/libp2p/autonat/2/dial-request`, `/libp2p/autonat/2/dial-back`)
**Spec:** https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md (r2, 2023-04-15)
**Implementation:** go-libp2p v0.47.0 (`p2p/protocol/autonatv2/`)
**Testbed:** Docker-based lab with configurable NAT types (iptables)

---

## Table of Contents

1. [Glossary](#glossary)
2. [Executive Summary](#executive-summary)
3. [Background: NAT Types and Traversal](#background-nat-types-and-traversal)
4. [Protocol Walkthrough](#protocol-walkthrough)
5. [Issue 1: Address-Restricted NAT False Positive](#issue-1-address-restricted-nat-false-positive)
6. [Issue 2: QUIC Dial-Back Failure on Fresh Servers](#issue-2-quic-dial-back-failure-on-fresh-servers)
7. [Additional Confirmed Issues](#additional-confirmed-issues)
8. [Appendix A: Testbed Architecture](#appendix-a-testbed-architecture)
9. [Appendix B: Test Results](#appendix-b-test-results)

---

## Glossary

| Acronym | Full Name | Description |
|---------|-----------|-------------|
| **NAT** | Network Address Translation | Router technique that maps private IP addresses to public ones, allowing multiple devices to share a single public IP |
| **AutoNAT** | Automatic NAT Detection | libp2p protocol for determining whether a node's addresses are publicly reachable |
| **STUN** | Session Traversal Utilities for NAT | IETF protocol (RFC 5389/8489) for discovering a node's external IP:port mapping and NAT type |
| **TURN** | Traversal Using Relays around NAT | IETF protocol (RFC 5766/8656) that relays traffic through a server when direct connection is impossible |
| **ICE** | Interactive Connectivity Establishment | IETF framework (RFC 8445) that combines STUN, TURN, and direct checks to find the best connection path |
| **DCUtR** | Direct Connection Upgrade through Relay | libp2p protocol for decentralized hole punching, coordinated via a relay peer |
| **QUIC** | QUIC (not an acronym) | UDP-based transport protocol (RFC 9000) providing multiplexed, encrypted connections |
| **UDP** | User Datagram Protocol | Connectionless transport protocol; used by QUIC |
| **TCP** | Transmission Control Protocol | Connection-oriented transport protocol; used by libp2p for reliable streams |
| **IP** | Internet Protocol | Network layer protocol providing addressing (IPv4/IPv6) |
| **DNAT** | Destination NAT | NAT rule that rewrites the destination address of incoming packets (used to forward traffic to internal hosts) |
| **SNAT** | Source NAT | NAT rule that rewrites the source address of outgoing packets (used to map internal IPs to a public IP) |
| **EIM** | Endpoint-Independent Mapping | NAT mapping behavior where the same external port is used regardless of destination (RFC 4787). Also called "static mapping" |
| **ADPM** | Address- and Port-Dependent Mapping | NAT mapping behavior where a different external port is assigned for each unique destination IP:port. Also called "symmetric" or "dynamic mapping" |
| **EIF** | Endpoint-Independent Filtering | NAT filtering that allows inbound traffic from any source to a mapped port (full-cone) |
| **ADF** | Address-Dependent Filtering | NAT filtering that allows inbound traffic only from IPs the internal host has previously contacted (address-restricted cone) |
| **APDF** | Address- and Port-Dependent Filtering | NAT filtering that allows inbound traffic only from the exact IP:port the internal host has previously contacted (port-restricted cone) |
| **CGNAT** | Carrier-Grade NAT | Large-scale NAT deployed by ISPs, often symmetric, sharing a single public IP among many subscribers (RFC 6598, 100.64.0.0/10) |
| **RTT** | Round-Trip Time | Time for a packet to travel to a destination and back; a measure of network latency |
| **TLS** | Transport Layer Security | Cryptographic protocol for securing communications; used in libp2p's connection handshake |
| **UPnP** | Universal Plug and Play | Protocol that allows devices to request port mappings from a router automatically |
| **NAT-PMP** | NAT Port Mapping Protocol | Apple-developed protocol for requesting port mappings from a router (predecessor to PCP) |
| **PCP** | Port Control Protocol | IETF protocol (RFC 6887) for requesting port mappings from a router |
| **IPFS** | InterPlanetary File System | Decentralized storage network built on libp2p |
| **DHT** | Distributed Hash Table | Decentralized key-value store used for peer discovery in libp2p (Kademlia-based) |
| **RFC** | Request for Comments | IETF standards documents |

---

## Executive Summary

We identified two issues in the AutoNAT v2 protocol through a combination of
real-world field experiments (3 network environments, 10 runs) and Docker
testbed validation (5 NAT types, native Linux VM):

1. **Address-restricted NAT false positive (CONFIRMED):** Nodes behind
   address-restricted cone NAT — the most common home router NAT type — are
   incorrectly classified as publicly reachable. This is a protocol design gap:
   the spec requires dial-back from a different peer ID but not a different IP.

2. **QUIC dial-back failure on fresh servers (CONFIRMED & WORKED AROUND):**
   The server's `dialerHost` shares its `UDPBlackHoleSuccessCounter` with
   the main host. On fresh servers with zero UDP history, the counter enters
   `Blocked` state, causing `CanDial()` to return false for all QUIC
   addresses. This is a limitation of how go-libp2p's
   [black hole detector](udp-black-hole-detector.md) interacts with
   AutoNAT v2 on freshly started servers — not a bug in the detector itself.
   Long-running nodes (Kubo) are unaffected. Testbed workaround: disable
   the detector on the main host so the `dialerHost` inherits no counter.

Additionally, two previously known issues were confirmed in both field and
testbed environments:

3. **Symmetric NAT bypasses v2 entirely (CONFIRMED):** No public address is
   ever activated, so v2 never runs.

4. **v1 confidence oscillation (CONFIRMED):** AutoNAT v1's sliding window
   oscillates ~33% of the time, independent of network latency.

---

## Background: NAT, Mapping, Filtering, and Traversal

### What NAT Does

A NAT router sits between a private network and the public internet. Devices
on the private network have private IP addresses (e.g., 192.168.1.x) that
are not routable on the internet. When a device sends a packet to the
internet, the NAT router:

1. **Replaces** the source address (private IP:port) with its own public
   IP and a chosen external port
2. **Records** this mapping in a translation table
3. **Forwards** the modified packet to the destination

When a response comes back to the public IP:external port, the router looks
up the mapping, rewrites the destination back to the private IP:port, and
forwards it to the device.

```
Private network                NAT Router                  Internet
192.168.1.10:4001  ──→  203.0.113.1:50000  ──→  Server at 1.2.3.4:443
                         │
                   Translation table:
                   192.168.1.10:4001 ↔ 203.0.113.1:50000 → 1.2.3.4:443
                         │
192.168.1.10:4001  ←──  203.0.113.1:50000  ←──  1.2.3.4:443 (response)
```

The critical question is: **what happens when an unsolicited packet arrives
at the public IP?** The answer depends on the NAT type.

For the full NAT mapping/filtering taxonomy (EIM, ADPM, EIF, ADF, APDF) and NAT type classification, see [autonat-v2.md](autonat-v2.md#nat-types-quick-reference).

### How NAT Filtering Affects AutoNAT v2 Dial-Back

When the AutoNAT server's `dialerHost` dials back to the client, the
connection arrives at the client's NAT from the **server's IP** but a
**different port** (ephemeral, not the server's listen port). The NAT's
filtering decision determines whether the dial-back reaches the client:

```
Client behind NAT contacted Server at 1.2.3.4:5000
NAT mapping: client:4001 → 203.0.113.1:50000

Server's dialerHost dials back from 1.2.3.4:random_port to 203.0.113.1:50000

Full-cone (EIF):       "Any source allowed"                → PASS
Addr-restricted (ADF): "Is 1.2.3.4 trusted? YES"          → PASS ← Issue #1
Port-restricted (APDF):"Is 1.2.3.4:random trusted? NO"    → BLOCK (correct)
Symmetric (APDF):      N/A — v2 never reaches this stage
```

The protocol cannot distinguish full-cone from address-restricted because the
dial-back always comes from a trusted IP. Full-cone is correct (truly
reachable), but address-restricted is a false positive (only reachable from
the server's IP).

### Port Forwarding, DMZ, and UPnP

Devices can bypass NAT restrictions by explicitly configuring the router to
forward inbound traffic. These mechanisms create **full-cone-equivalent**
behavior for specific ports:

**Manual Port Forwarding:**
The router administrator creates a static rule: "forward all traffic arriving
at public port X to private device Y on port Z."

```
Router rule: 203.0.113.1:4001 → 192.168.1.10:4001

Any host on the internet → 203.0.113.1:4001 → forwarded to 192.168.1.10:4001
```

The device is truly publicly reachable on that port. No outbound traffic is
needed to create the mapping — it's permanent.

**DMZ (Demilitarized Zone):**
The router forwards ALL inbound traffic (all ports) to a single designated
device. Equivalent to making that device directly reachable on the router's
public IP for every port. Simple but exposes the device to all internet
traffic.

**UPnP (Universal Plug and Play) / NAT-PMP / PCP:**
The device programmatically asks the router to create a port forwarding rule.
No manual configuration needed — the device sends a request to the router
saying "please forward public port 4001 to my private address on port 4001."

```
Device → Router: "Map external port 4001 to me"
Router: Creates forwarding rule (like manual port forwarding)
Device is now reachable from the internet on that port
```

libp2p supports UPnP via the `go-libp2p` `NATManager`. When enabled, the
node automatically requests port mappings from the router. If the router
supports UPnP and it's enabled, this effectively creates full-cone behavior
for the mapped ports — making the node genuinely publicly reachable.

**Impact on AutoNAT v2:** When port forwarding, DMZ, or UPnP is active, the
node IS truly publicly reachable. AutoNAT v2 correctly reports "reachable"
in these cases. The false positive (Issue #1) only affects nodes behind
address-restricted NAT **without** any port forwarding configured.

| Configuration | NAT behavior | AutoNAT v2 result | Correct? |
|--------------|-------------|-------------------|----------|
| No port forwarding, addr-restricted | ADF | Reachable | **No (Issue #1)** |
| Manual port forwarding | EIF (for that port) | Reachable | Yes |
| DMZ | EIF (all ports) | Reachable | Yes |
| UPnP/NAT-PMP mapping | EIF (for that port) | Reachable | Yes |
| No port forwarding, port-restricted | APDF | Unreachable | Yes |
| No port forwarding, symmetric | ADPM+APDF | v2 never runs | N/A |

### NAT Traversal in libp2p vs Traditional Protocols

| Step | Traditional (WebRTC/VoIP) | libp2p |
|------|--------------------------|--------|
| 1. Discover external address | **STUN** binding request | **Identify** protocol (peers report ObservedAddr) |
| 2. Classify NAT / test reachability | **STUN** behavior tests (RFC 5780) | **AutoNAT v1/v2** |
| 3. Establish direct connection | **ICE** (try direct, STUN, TURN) | **DCUtR** (hole punching via relay) |
| 4. Fallback relay | **TURN** server | **Circuit Relay v2** |

The key difference at step 2: STUN tests reachability from **multiple IP
addresses** (same IP different port, different IP same port), which allows it
to classify the NAT type and distinguish full-cone from address-restricted.
AutoNAT v2 only tests reachability from the **same IP** the client already
contacted, making these two NAT types indistinguishable.

### Related Work

**IETF Draft: Using QUIC to Traverse NATs**
([draft-seemann-quic-nat-traversal-02](https://datatracker.ietf.org/doc/html/draft-seemann-quic-nat-traversal)),
authored by Marten Seemann (Protocol Labs) and Eric Kinnear (Apple). Proposes
two approaches for QUIC-native NAT traversal:

1. **ICE + QUIC:** Uses standard ICE (RFC 8445) with STUN on the same UDP
   socket as QUIC. After ICE completes, the client initiates a QUIC handshake
   using the nominated address pair. The preceding ICE connectivity checks
   establish the necessary NAT bindings.

2. **QUIC NAT Extension:** A QUIC-native approach using new frame types
   (`ADD_ADDRESS`, `PUNCH_ME_NOW`, `REMOVE_ADDRESS`) to coordinate hole
   punching without ICE. The server sends address candidates, the client
   pairs them and triggers path validation via QUIC's built-in mechanisms.

References: RFC 9000 (QUIC), RFC 8445 (ICE), RFC 5389 (STUN),
RFC 9287 (QUIC bit greasing), RFC 8838 (Trickle ICE).

**Decentralized Hole Punching**
([Seemann, Inden, Vyzovitis — DINPS 2022](https://research.protocol.ai/publications/decentralized-hole-punching/)),
presented at the 5th International Workshop on Distributed Infrastructure
for the Common Good. Introduces the DCUtR protocol for hole punching without
centralized infrastructure. The approach "leverages protocols similar to STUN
(RFC 8489), TURN (RFC 8566) and ICE (RFC 8445), without the need for any
centralized infrastructure." A single bootstrap node suffices for peer
discovery.

**ProbeLab Hole Punching Measurement Campaign**
([probe-lab/network-measurements, Dec 2022](https://github.com/probe-lab/network-measurements/blob/main/results/rfm15-nat-hole-punching.md)),
measuring DCUtR performance across real networks with volunteer-deployed
vantage points:

- Overall hole punching success rate: **~70%**
- TCP and QUIC showed **roughly equivalent success rates**
- When both available, QUIC won the race ~81% of the time
- Success was largely independent of relay RTT or location
- Most successes occurred on the **first attempt**
- Four networks showed <5% success (likely symmetric NAT)
- VPN peers experienced reduced effectiveness due to additional NAT layers

**Challenging Tribal Knowledge — Large Scale Measurement Campaign on
Decentralized NAT Traversal**
([2025, arXiv:2510.27500](https://arxiv.org/html/2510.27500v1)),
analyzing 4.4M+ traversal attempts from 85K+ networks across 167 countries:

- Confirmed **~70% ± 7.1% baseline** hole punching success rate
- Refuted long-held assumption of UDP superiority — TCP and QUIC achieve
  statistically indistinguishable rates
- 97.6% of successes on first attempt
- Proposed improvements: birthday paradox multi-port opening (+12.5% for
  mixed NAT), role alternation on retry, refined RTT calculation
- Cited historical data: ~11% of peers behind symmetric NAT (Halkes et al.,
  2011), with CGNAT proliferation increasing EDM usage (40% of CGNAT
  deployments use EDM per 2016 data)

**NAT Classification in the libp2p Ecosystem**

The libp2p specifications and research use a simplified NAT model:

| libp2p model | RFC 4787 NAT types included |
|-------------|---------------------------|
| "Easy NAT" (cone, EIM) | Full-cone (EIF), address-restricted (ADF), port-restricted (APDF) |
| "Hard NAT" (symmetric, ADPM) | Symmetric (ADPM + APDF) |

This binary classification aligns with the hole punching use case: EIM NATs
allow hole punching (peers agree on the external address), while ADPM NATs
do not (each peer sees a different external port).

**Relevant RFCs**

| RFC | Title | Relevance |
|-----|-------|-----------|
| [RFC 4787](https://datatracker.ietf.org/doc/html/rfc4787) | NAT Behavioral Requirements for Unicast UDP | Defines EIM/ADPM mapping and EIF/ADF/APDF filtering taxonomy |
| [RFC 5780](https://datatracker.ietf.org/doc/html/rfc5780) | NAT Behavior Discovery Using STUN | Defines tests to classify NAT type (change IP, change port) |
| [RFC 5128](https://datatracker.ietf.org/doc/html/rfc5128) | State of P2P Communication Across NATs | Surveys NAT traversal techniques and success rates by NAT type |
| [RFC 8445](https://datatracker.ietf.org/doc/html/rfc8445) | ICE: Interactive Connectivity Establishment | Framework for NAT traversal combining STUN, TURN, and direct checks |
| [RFC 8489](https://datatracker.ietf.org/doc/html/rfc8489) | STUN: Session Traversal Utilities for NAT | Discovers external address mappings |
| [RFC 8656](https://datatracker.ietf.org/doc/html/rfc8656) | TURN: Traversal Using Relays around NAT | Relays traffic when direct connection is impossible |
| [RFC 9000](https://datatracker.ietf.org/doc/html/rfc9000) | QUIC: A UDP-Based Multiplexed and Secure Transport | Transport protocol used by libp2p |

---

## Protocol Walkthrough

For a step-by-step walkthrough of the AutoNAT v2 protocol (address activation,
server selection, DialRequest, amplification prevention, dial-back, nonce
verification, confidence accumulation), see
[autonat-v2.md](autonat-v2.md). For go-libp2p implementation details
(constants, structs, confidence system), see
[go-libp2p-autonat-implementation.md](go-libp2p-autonat-implementation.md).

---

## Issue 1: Address-Restricted NAT False Positive

### Classification

- **Category:** Protocol design gap
- **Severity:** High — affects the most common NAT type in home routers
- **Impact:** Nodes incorrectly advertise as publicly reachable; peers that
  attempt direct connections fail, causing delays before relay/hole-punch
  fallback

### The Spec Gap

The protocol tests: **"can the server reach the client?"** But it equates this
with **"can anyone reach the client?"** For address-restricted NAT, these are
not the same thing.

Three specific passages in the spec contribute to this gap:

**1. Dial-back uses same IP (Stage 4):**

> "The server dials the selected address, opens a stream with Protocol ID
> `/libp2p/autonat/2/dial-back`"

The spec describes a single server performing both the request handling and
the dial-back. The dial-back necessarily originates from the server's IP —
the same IP the client already contacted.

**2. Different peer ID, not different IP (Stage 4):**

> "Clients SHOULD only rely on the nonce and not on the peerID for verifying
> the dial back as the server is free to use a separate peerID for the dial
> backs."

The spec explicitly allows a separate peer ID, which the implementation uses
via `dialerHost`. But it says nothing about using a separate IP. The purpose
of the different peer ID is to ensure a fresh connection handshake — not to
test reachability from an untrusted network location.

**3. Success means "publicly reachable" (Stage 6):**

> `E_DIAL_ERROR`: "indicating the selected address is **not publicly
> reachable**"

The inverse of `E_DIAL_ERROR` is `OK`, which implicitly means "publicly
reachable." But the test only proves reachability from the server's IP.

### The False Positive Flow

```
         Client (behind addr-restricted NAT)   Server (1.2.3.4)
              │                                       │
  Stage 2:    │── connect to 1.2.3.4 ───────────────→│
              │   NAT creates mapping                 │  main host
              │   NAT remembers: "1.2.3.4 is trusted" │  (peer ID: A)
              │                                       │
  Stage 4:    │←── dial-back from 1.2.3.4 ───────────│  dialerHost
              │    NAT: "is 1.2.3.4 trusted?"         │  (peer ID: B)
              │    YES → allow connection              │  SAME IP
              │                                       │
  Stage 6:    │←── DialResponse { dialStatus: OK } ──│
              │                                       │
              │    Client concludes: "I am public!"    │
              │                                       │
  Later:      │←── peer at 5.6.7.8 tries to connect   │
              │    NAT: "is 5.6.7.8 trusted?"          │
              │    NO → DROP                           │
              │    Connection fails despite "public"   │
```

### NAT Type Behavior Matrix

| NAT Type | Mapping | Filtering | Dial-back from same IP | v2 result | Correct? |
|----------|---------|-----------|----------------------|-----------|----------|
| Full-cone | EIM | EIF | Allowed (any source) | Reachable | **Yes** — truly reachable from anyone |
| Address-restricted | EIM | ADF | Allowed (trusted IP) | Reachable | **No** — only reachable from contacted IPs |
| Port-restricted | EIM | APDF | Blocked (different port) | Unreachable | **Yes** |
| Symmetric | ADPM | APDF | N/A (v2 never runs) | — | N/A |

### Testbed Evidence

Docker Compose testbed with iptables router, 5 AutoNAT servers, 1 client.
All runs on native Linux VM (Docker Desktop macOS has a separate DNAT issue).

| NAT Type | Command | Result |
|----------|---------|--------|
| `full-cone` | `NAT_TYPE=full-cone TRANSPORT=tcp ./testbed/run.sh testbed/scenarios/matrix.yaml` | **Reachable ~3s** |
| `address-restricted` | `NAT_TYPE=address-restricted TRANSPORT=tcp ./testbed/run.sh ...` | **Reachable ~3s** (false positive) |
| `port-restricted` | `NAT_TYPE=port-restricted TRANSPORT=tcp ./testbed/run.sh ...` | Unreachable ~3s |
| `symmetric` | `NAT_TYPE=symmetric TRANSPORT=tcp ./testbed/run.sh ...` | v2 never fires (120s) |

Address-restricted shows identical behavior to full-cone despite the node
being unreachable from arbitrary IPs.

**NAT rule verification** (`verify-nat.sh address-restricted`):
- Server 1 (previously contacted IP), different port → **reaches client**
- Server 2 (never contacted IP) → **blocked**

This confirms the iptables rules correctly implement ADF and the false
positive is a protocol-level issue, not a testbed artifact.

### Real-World Prevalence

**Important caveat:** We initially assumed address-restricted NAT (ADF) is
"the most common" home router type. This appears to be incorrect. Testing of
consumer devices (Cisco-Linksys, ipTIME, telecom-provided APs) found they
all use **EIM + APDF** (port-restricted cone), not EIM + ADF
([source](https://www.netmanias.com/en/post/techdocs/6062/nat-network-protocol/nat-behavioral-requirements-as-defined-by-the-ietf-rfc-4787-part-2-filtering-behavior)).
Linux-based routers (OpenWrt, DD-WRT, most OEM firmware) also default to
APDF because netfilter's conntrack tracks the full 5-tuple.

[RFC 4787](https://datatracker.ietf.org/doc/html/rfc4787) recommends ADF as
the minimum filtering behavior but permits APDF. In practice, most tested
devices implement the stricter APDF.

| NAT Type | Where found | Prevalence | Affected? |
|----------|-------------|------------|-----------|
| Port-restricted (APDF) | Most home routers (Linux/conntrack) | **Very common** | No (correctly detected) |
| Address-restricted (ADF) | Some older/non-Linux routers, some firewalls | **Unknown — needs measurement** | **Yes — false positive** |
| Symmetric (ADPM+APDF) | Mobile carriers, satellite, CGNAT | Common | No (v2 skipped entirely) |
| Full-cone (EIF) | Routers with DMZ/UPnP/port forwarding | Uncommon (requires config) | No (correctly reachable) |

**The protocol gap is real**, but its real-world impact depends on how many
networks use ADF vs APDF filtering. Comprehensive NAT type distribution data
is lacking in the literature — existing studies test small numbers of specific
devices. A large-scale measurement study (e.g., via the IPFS network) would
be needed to quantify the actual prevalence of ADF-based NATs.

### Comparison with STUN

STUN (RFC 5780) avoids this problem by testing from **multiple IP addresses**.
The STUN server performs three tests:

| Test | Source | Detects |
|------|--------|---------|
| 1. Same IP, same port | Server's primary addr | Baseline mapping |
| 2. Same IP, **different port** | Server's primary IP, alt port | Port filtering (APDF vs ADF) |
| 3. **Different IP**, same port | Server's secondary IP | Address filtering (ADF vs EIF) |

Test 3 is what AutoNAT v2 lacks. Through address-restricted NAT, test 3 would
fail (client hasn't contacted the secondary IP), correctly revealing the NAT's
filtering behavior.

### Suggested Fixes

**Option A: Multi-IP dial-back.** Servers with multiple public IPs use a
different IP for dial-back (analogous to STUN test 3). The spec would add:

> "Servers SHOULD use a different IP address for dial-back than the one the
> client connected to. Servers with only one IP SHOULD indicate this
> limitation in the response."

**Option B: Coordinated dial-back.** Server A receives the DialRequest and
asks Server B (different IP) to perform the dial-back. More complex but
doesn't require multi-homed servers.

**Option C: Client-side heuristic.** Clients treat "reachable" results with
lower confidence when the dial-back came from the same IP they connected to.
Defense-in-depth measure that doesn't require spec changes.

---

## Issue 2: QUIC Dial-Back Failure on Fresh Servers

### Classification

- **Category:** Testbed limitation (go-libp2p black hole detector interaction)
- **Severity:** High — affects all freshly started AutoNAT v2 servers
- **Status:** CONFIRMED and WORKED AROUND in probe-lab fork; upstream fix pending

### Summary

The server's `dialerHost` (internal host used for dial-back connections)
shares its `UDPBlackHoleSuccessCounter` with the main host. On fresh servers
with zero UDP connection history, the counter enters `Blocked` state, causing
`CanDial()` to return false for all QUIC/UDP addresses. The server responds
`E_DIAL_REFUSED` for every QUIC dial-back request, and QUIC addresses stay
"unknown" indefinitely on the client.

Long-running nodes (e.g., Kubo in production) are unaffected because their
counters accumulate enough successful UDP connections to reach `Allowed` state.

For the full analysis — what the black hole detector is, why the upstream v2
approach doesn't work on fresh servers, the testbed workaround, and the
proper upstream fix — see
[UDP Black Hole Detector and AutoNAT v2](udp-black-hole-detector.md).

### Evidence

**Before workaround (5 servers, all transports):**

| NAT Type | Transport | v2 QUIC |
|----------|-----------|---------|
| none | quic | **unknown (120s)** |
| none | both | **unknown (30s+)** |
| full-cone | both | **unknown (30s+)** |
| port-restricted | both | **unknown (30s+)** |

Debug output from `filterKnownUndialables`:
```
dialable=[] errs=[{/ip4/1.2.3.4/udp/4001/quic-v1 dial refused because of black hole}]
```

**After workaround (7 servers):**

| NAT Type | Transport | v2 TCP | v2 QUIC | Time |
|----------|-----------|--------|---------|------|
| none | both | reachable | **reachable** | ~6s |
| none | quic | — | **reachable** | ~6s |
| symmetric | both | — | — (v1: private) | ~18s |
| symmetric | quic | — | — (v1: private) | ~15s |

### Investigation Trail

1. QUIC v2 stayed "unknown" with 5 servers → added 7 servers → still unknown
2. Compared testbed (fails) vs public IPFS servers (works) — key insight
3. Added `replace` directive to use `probe-lab/go-libp2p` fork → still unknown
4. Debug logging in `server.go`: `canDial=false` for QUIC
5. Added listen addresses for `dialerHost` → `canDial` still false
6. Exposed `filterKnownUndialables` via debug method → found `"black hole"` error
7. Traced to shared `UDPBlackHoleSuccessCounter` in `Blocked` state
8. Workaround: nil counter on main host → `dialerHost` inherits nil → QUIC works

### Recommendations

1. **go-libp2p fix:** The `dialerHost` should not share black hole counters
   with the main host. The probe-lab fork (`v0.47.0-autonat_otel`) already
   applies this workaround locally. See
   [upstream fix](udp-black-hole-detector.md#proper-upstream-fix) for the
   proposed upstream fix.

2. **Server count:** With `targetConfidence=3` and 2 primary addresses
   (TCP + QUIC), at least 6 servers are needed (3 per address). Use 7 for
   margin.

3. **Applications:** Enable both `EnableAutoNATv2()` and `EnableNATService()`:
   ```go
   opts := []libp2p.Option{
       libp2p.EnableAutoNATv2(),
       libp2p.EnableNATService(),
       libp2p.NATPortMap(),
   }
   ```

---

## Additional Confirmed Issues

### Issue #17: Symmetric NAT Bypasses v2 Entirely

- **Stage affected:** Stage 0 (Address activation)
- **Category:** Protocol design / implementation coupling
- **Status:** CONFIRMED — in-flight satellite WiFi (2026-02-16) and testbed

Symmetric NAT (ADPM) assigns a different external port per destination.
go-libp2p's `ObservedAddrManager` requires `ActivationThresh=4` identical
observations to activate a public address. With symmetric NAT, every peer
sees a different external port, so no single address reaches the threshold.
AutoNAT v2 has zero addresses to probe and never runs.

**Impact:** On symmetric NAT (mobile carriers, satellite, CGNAT), the node's
only reachability signal comes from AutoNAT v1, which oscillates ~33% of the
time (Issue #8). In the worst case, the node has no reliable reachability
information at all.

### Issue #8: v1 Confidence Window Oscillation

- **Stage affected:** Stage 7 equivalent in v1
- **Category:** Implementation
- **Status:** CONFIRMED — in-flight satellite WiFi and hotel WiFi

AutoNAT v1's sliding window (5 probes, `minConfidence=2`) oscillates between
`private` and `unknown` states. Observed at both 711ms RTT (satellite) and
6ms RTT (hotel WiFi), confirming the oscillation is not latency-dependent.
Oscillation rate: ~33% of runs in both environments.

### Issue #18: TCP Port Blocking Creates QUIC-Only Discovery

- **Stage affected:** Stage 0 (Bootstrap + Identify)
- **Category:** Infrastructure
- **Status:** CONFIRMED — hotel WiFi (2026-02-19)

Managed networks (hotels, airports, corporate) often block outbound TCP on
non-standard ports (e.g., 4001) while allowing UDP. go-libp2p prefers QUIC
for bootstrap, so connections succeed, but no TCP observed addresses are
reported via Identify. Only QUIC public address is discovered and probed.

**Empirical data** (hotel WiFi, 3 runs): outbound TCP to port 4001 blocked
(`nc -z` failed). 5/5 bootstrap peers connected over QUIC (0.2-2.5s). QUIC
address activated with remapped port (`/ip4/63.211.255.232/udp/29538/quic-v1`).
No TCP address ever appeared.

### Issue #16a: Docker Desktop macOS DNAT Bug

- **Stage affected:** Stage 4 (Dial-back connection)
- **Category:** Testbed infrastructure
- **Status:** CONFIRMED — root cause identified via tcpdump (2026-02-21)

Docker Desktop macOS rewrites source IPs to the bridge gateway (10.0.1.1) when
packets cross Docker networks through the router's DNAT. The client's SYN-ACK
goes to 10.0.1.1 which has no conntrack entry, causing RST. This is NOT a
libp2p issue — plain TCP through DNAT fails the same way. **Workaround:** run
the testbed on native Linux. Port-restricted and symmetric results remain valid
on macOS (they fail regardless of DNAT).

---

## Protocol Flow → Issue Mapping

Each stage of the AutoNAT v2 reachability flow has specific failure modes:

```
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 1: Bootstrap + Identify                                      │
│  Issues: #18 TCP port blocking (CONFIRMED, hotel)                   │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 2: Observed Address Activation (ActivationThresh=4)          │
│  Issues: #17 Symmetric NAT prevents activation (CONFIRMED, flight)  │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 3: AutoNAT v2 Probing (DialRequest → DialBack → verify)     │
│  Issues: #1 Address-restricted false positive (CONFIRMED, testbed)  │
│          #2 QUIC dial-back failure (CONFIRMED & FIXED)              │
│          #3 Rate limiting exhaustion (Untested)                     │
│          #4 Timeout-induced failures (Untested)                     │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 4: Confidence Accumulation                                   │
│  Issues: #8 v1 oscillation (CONFIRMED, flight + hotel)              │
│          #9 NAT mapping timeout (Untested)                          │
├─────────────────────────────────────────────────────────────────────┤
│  STAGE 5: Result                                                    │
│  v2 emits EvtHostReachableAddrsChanged (per-address)               │
│  v1 emits EvtLocalReachabilityChanged (host-level)                 │
│  WORST CASE (symmetric NAT): v2 never runs → v1 oscillates →       │
│  node has NO reliable reachability information                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Issue Categories

1. **False positives**: Node behind NAT incorrectly classified as public
2. **False negatives**: Publicly reachable node incorrectly classified as private
3. **Flakiness**: Detection oscillates between public and private
4. **Blind spots**: AutoNAT v2 never runs, falling back to less reliable v1

---

## Hypothesized Issues (Untested)

### #3: Rate Limiting Exhaustion

**Category:** Infrastructure / implementation. When many clients probe the same
servers, rate limiting (60 RPM global, 12 RPM per peer) causes
`E_REQUEST_REJECTED`. If rejections dominate, convergence is delayed or never
happens.

### #4: Timeout-Induced Failures

**Category:** Implementation. Under high latency or packet loss, the dial-back
may exceed timeouts (15s stream, 10s dial-back, 5s nonce exchange), causing
false `E_DIAL_ERROR`.

### #5: Insufficient AutoNAT Servers

**Category:** Infrastructure. Nodes with few connected v2-capable peers may not
find enough servers for `minConfidence=2`. Each server is throttled for 2
minutes after use.

### #9: NAT Mapping Timeout Racing

**Category:** Protocol design. NAT mappings have finite lifetimes (30s-300s for
UDP). If mappings expire between probes, results oscillate.

### #10: Server Selection Randomness

**Category:** Implementation. Different servers may give different results due
to varying latencies or capabilities. Random selection means mixed signals in
the confidence window.

### Protocol Design Concerns

**Single address per request (#11):** The server dials exactly one address per
request. Verifying N addresses requires N separate requests, scaling convergence
time linearly.

**Trust in negative reports (#12):** A malicious server could falsely claim
`E_DIAL_ERROR`. Nonce verification only protects against false positives.

**Amplification cost as barrier (#13):** Cross-IP testing incurs 30-100KB dial
data cost, slowing convergence for multi-interface nodes.

---

## Real-World Experiments → Testbed Mapping

Three field experiments, each mapped to a testbed scenario:

| Field Experiment | Date | NAT Type | Testbed Equivalent | Key Finding |
|------------------|------|----------|-------------------|-------------|
| Heathrow airport WiFi | 2026-02-16 | Port-restricted (EIM, port-preserving) | `NAT_TYPE=port-restricted` | v2 correctly unreachable at ~12s |
| In-flight satellite WiFi | 2026-02-16 | Symmetric (ADPM) | `NAT_TYPE=symmetric LATENCY_MS=350` | v2 completely bypassed, v1 only |
| Hotel WiFi | 2026-02-19 | Port-restricted (EIM, port-remapping) + TCP blocked | `NAT_TYPE=port-restricted` + TCP filtering | v2 QUIC-only, correctly unreachable |

### Testbed Reproduction Results

**Experiment #13: Hotel WiFi** (2026-02-25) — port-restricted + TCP port 4001
blocked + port remap 4001→29538:

| Metric | Field (hotel) | Testbed |
|--------|--------------|---------|
| QUIC address activation | ~5s | ~5s |
| v2 QUIC unreachable | ~11s | ~11s |
| v1 private | ~17s | ~18s |
| TCP addresses | Never discovered | Never discovered |

Testbed accurately reproduces hotel WiFi behavior.

**Experiment #14: Flight WiFi** (2026-02-25) — symmetric NAT + 350ms one-way
delay:

| Metric | Field (flight) | Testbed |
|--------|---------------|---------|
| First connection | 10-18s | 5.1s |
| v1 private | ~31s | ~21s |
| v2 fired | No | No |
| Public address activated | No | No |

Key behavior reproduced: symmetric NAT prevents address activation and v2
never fires. Lower absolute latency because local server discovery is faster
than real IPFS bootstrap.

**Experiments #9/#10: Packet Loss / High Latency** — NOT YET VALID. The
`tc netem` rules are applied on the router, but `none` NAT type places the
client directly on `public-net` bypassing the router. Must re-run with
`full-cone` NAT type.

---

## Issue Summary

| Issue | Stage | Status | Category | Impact |
|-------|-------|--------|----------|--------|
| #1 Addr-restricted false positive | 3 (Probing) | **CONFIRMED** | Protocol design | Wrong "public" result |
| #2 QUIC dial-back black hole | 3 (Probing) | **WORKED AROUND** | Testbed limitation | Fresh servers can't dial QUIC |
| #17 Symmetric blocks v2 | 2 (Activation) | **CONFIRMED** | Protocol design | v2 completely bypassed |
| #8 v1 oscillation | 4 (Confidence) | **CONFIRMED** | Implementation | Unreliable result (~33%) |
| #18 TCP port blocking | 1 (Bootstrap) | **CONFIRMED** | Infrastructure | QUIC-only discovery |
| #16a Docker macOS DNAT | 3 (Probing) | **CONFIRMED** | Infrastructure | Testbed limitation |
| #3 Rate limiting | 3 (Probing) | Untested | Infrastructure | Delayed convergence |
| #4 Timeout failures | 3 (Probing) | Untested | Implementation | False negatives |
| #5 Insufficient servers | 3 (Probing) | Untested | Infrastructure | Delayed convergence |
| #9 Mapping timeout | 4 (Confidence) | Untested | Protocol design | Intermittent results |
| #10 Server selection | 4 (Confidence) | Untested | Implementation | Mixed signals |

---

## Appendix A: Testbed Architecture

```
                    public-net (73.0.0.0/24)
                    ┌────────────────────────────────────────┐
                    │                                        │
              ┌─────┴─────┐  ┌─────────┐       ┌─────────┐  │
              │  Router    │  │Server 1 │  ...  │Server 5 │  │
              │ 73.0.0.2   │  │73.0.0.10│       │73.0.0.14│  │
              │ (iptables) │  └─────────┘       └─────────┘  │
              └─────┬─────┘                                  │
                    │                                        │
                    │  private-net (10.0.1.0/24)             │
                    ├────────────────────┐                   │
                    │                    │                   │
              ┌─────┴─────┐        ┌────┴────┐              │
              │  Router    │        │ Client  │              │
              │ 10.0.1.2   │        │10.0.1.10│              │
              └───────────┘        └─────────┘              │
                                                            │
                    ┌─────────┐  (no-NAT mode)              │
                    │ Client  ├─────────────────────────────┘
                    │73.0.0.100  (directly on public-net)
                    └─────────┘
```

**NAT types implemented via iptables on the router:**

| NAT Type | iptables Rules | Mapping | Filtering |
|----------|---------------|---------|-----------|
| `full-cone` | SNAT (outbound) + DNAT (all inbound to client) | EIM | EIF |
| `address-restricted` | MASQUERADE + xt_recent (track contacted IPs) + DNAT | EIM | ADF |
| `port-restricted` | MASQUERADE + conntrack ESTABLISHED | EIM | APDF |
| `symmetric` | MASQUERADE --random (randomize source port per dest) | ADPM | APDF |

**Additional router features:**
- `TCP_BLOCK_PORT`: Blocks outbound TCP on a specified port (reproduces hotel WiFi)
- `PORT_REMAP`: Forces consistent port remapping via SNAT (reproduces hotel WiFi's 4001→29538)

---

## Appendix B: Test Results

### Linux VM Testbed (2026-02-23, before QUIC fix)

| File | NAT Type | Transport | Result | Time |
|------|----------|-----------|--------|------|
| `full-cone-tcp-5-20260223T131733Z.json` | full-cone | TCP | Reachable | ~3s |
| `address-restricted-tcp-5-20260223T131941Z.json` | address-restricted | TCP | Reachable (false positive) | ~3s |
| `port-restricted-tcp-5-20260223T132354Z.json` | port-restricted | TCP | Unreachable | ~3s |
| `symmetric-tcp-5-20260223T132519Z.json` | symmetric | TCP | v2 never fires | 120s |
| `none-quic-5-20260223T132840Z.json` | none | QUIC | Unknown | 120s |

### Linux VM Testbed (2026-02-24, with EnableNATService — TCP-only)

| File | NAT Type | Transport | v1 | v2 TCP | Time |
|------|----------|-----------|-----|--------|------|
| `none-tcp-5-20260224T143203Z.json` | none | TCP | public ~3s | reachable | ~6s |
| `full-cone-tcp-5-20260224T143407Z.json` | full-cone | TCP | public ~3s | reachable | ~6s |
| `address-restricted-tcp-5-20260224T143611Z.json` | addr-restricted | TCP | public ~3s | reachable (false positive) | ~6s |
| `port-restricted-tcp-5-20260224T143815Z.json` | port-restricted | TCP | private ~18s | unreachable | ~6s |
| `symmetric-tcp-5-20260224T144036Z.json` | symmetric | TCP | private ~18s | never fires | — |

### Linux VM Testbed (2026-02-24, with EnableNATService — QUIC-only)

| File | NAT Type | Transport | v1 | v2 QUIC | Time |
|------|----------|-----------|-----|---------|------|
| `none-quic-5-20260224T143305Z.json` | none | QUIC | public ~3s | **unknown** | 120s |
| `full-cone-quic-5-20260224T143509Z.json` | full-cone | QUIC | public ~3s | **unknown** | 120s |
| `address-restricted-quic-5-20260224T143713Z.json` | addr-restricted | QUIC | public ~3s | **unknown** | 120s |
| `port-restricted-quic-5-20260224T143919Z.json` | port-restricted | QUIC | private ~18s | **unknown** | 120s |
| `symmetric-quic-5-20260224T144152Z.json` | symmetric | QUIC | private ~18s | never fires | — |

### Linux VM Testbed (2026-02-24, with EnableNATService — both transports)

| File | NAT Type | Transport | v1 | v2 TCP | v2 QUIC |
|------|----------|-----------|-----|--------|---------|
| `none-both-5-20260224T154559Z.json` | none | both | public ~3s | reachable ~6s | **unknown** |
| `full-cone-both-5-20260224T154701Z.json` | full-cone | both | public ~3s | — | **unknown** |
| `address-restricted-both-5-20260224T154804Z.json` | addr-restricted | both | public ~3s | — | **unknown** |
| `port-restricted-both-5-20260224T154906Z.json` | port-restricted | both | private ~18s | — | **unknown** |
| `symmetric-both-5-20260224T155023Z.json` | symmetric | both | private ~18s | never fires | never fires |

Note: In NAT modes (full-cone, addr-restricted, port-restricted), only the
QUIC address is tracked by v2 (the NAT-mapped address). TCP address is not
separately tracked in these cases because both transports share the router's
public IP mapping.

### Linux VM Testbed (2026-02-24, after QUIC fix — full matrix, 7 servers)

All tests pass (10/10). QUIC v2 per-address reachability now works correctly.

| File | NAT Type | Transport | v1 | v2 | Time |
|------|----------|-----------|-----|-----|------|
| `none-tcp-7-20260224T222648Z.json` | none | TCP | public | reachable | ~6s |
| `none-quic-7-20260224T222751Z.json` | none | QUIC | public | **reachable** | ~6s |
| `full-cone-tcp-7-20260224T222854Z.json` | full-cone | TCP | public | reachable | ~3s |
| `full-cone-quic-7-20260224T222956Z.json` | full-cone | QUIC | public | **reachable** | ~3s |
| `address-restricted-tcp-7-20260224T223059Z.json` | addr-restricted | TCP | public | reachable (false positive) | ~3s |
| `address-restricted-quic-7-20260224T223202Z.json` | addr-restricted | QUIC | public | **reachable** (false positive) | ~3s |
| `port-restricted-tcp-7-20260224T223305Z.json` | port-restricted | TCP | private | unreachable | ~3s |
| `port-restricted-quic-7-20260224T223410Z.json` | port-restricted | QUIC | private | **unreachable** | ~6s |
| `symmetric-tcp-7-20260224T223519Z.json` | symmetric | TCP | private | never fires | ~18s |
| `symmetric-quic-7-20260224T223637Z.json` | symmetric | QUIC | private | never fires | ~15s |

Both transports combined (5/5 passed):

| File | NAT Type | Transport | v1 | v2 TCP | v2 QUIC | Time |
|------|----------|-----------|-----|--------|---------|------|
| `none-both-7-20260224T224149Z.json` | none | both | public | reachable | **reachable** | ~6s |
| `full-cone-both-7-20260224T224252Z.json` | full-cone | both | public | — | **reachable** | ~3s |
| `address-restricted-both-7-20260224T224355Z.json` | addr-restricted | both | public | — | **reachable** (FP) | ~3s |
| `port-restricted-both-7-20260224T224458Z.json` | port-restricted | both | private | — | **unreachable** | ~6s |
| `symmetric-both-7-20260224T224607Z.json` | symmetric | both | private | never fires | never fires | ~18s |

Key changes from pre-fix results:
- QUIC now resolves correctly across all NAT types (was "unknown" in all cases)
- `none/both/7` shows `reachable=2` — both TCP and QUIC resolve in a single run
- Address-restricted false positive (Issue #1) reproduces identically for QUIC
- Port-restricted correctly detected as unreachable for QUIC
- Symmetric NAT still bypasses v2 entirely (expected — Issue #17)

### Hotel WiFi Reproduction (2026-02-25, 7 servers)

Testbed simulation of hotel WiFi conditions: port-restricted NAT + TCP port
4001 blocked + port remap 4001→29538.

| File | Setup | v2 QUIC | v1 | Time |
|------|-------|---------|-----|------|
| `port-restricted-both-7-20260225T075406Z.json` | port-restricted + tcp-block + remap | unreachable | private | ~18s |

- QUIC address activated at ~5s with remapped port: `/ip4/73.0.0.2/udp/29538/quic-v1`
- v2 marked unreachable at ~11s (port-restricted blocks dial-back)
- No TCP addresses discovered (outbound TCP blocked)
- Matches field data (hotel WiFi 2026-02-19) within ±1s on all metrics

### Flight WiFi Reproduction (2026-02-25, 7 servers)

Testbed simulation of in-flight satellite WiFi: symmetric NAT + 350ms one-way
delay (700ms RTT).

| File | Setup | v2 | v1 | Bootstrap |
|------|-------|-----|-----|-----------|
| `symmetric-both-7-20260225T075515Z.json` | symmetric + 350ms latency | never fired | private ~21s | 5-16s |

- First connection at 5.1s (vs 3s baseline — latency adds ~2s per handshake)
- No public address activated (symmetric NAT prevents activation)
- v2 never ran (no addresses to probe)
- Confirms field observation: symmetric NAT + high latency = v1-only

### Packet Loss / High Latency (2026-02-25 — NOT YET VALID)

Experiments #9 (packet loss 1/5/10%) and #10 (latency 200/500ms) were run with
`none` NAT type. The `tc netem` rules are applied on the **router container**,
but `none` NAT places the client directly on `public-net` without a router.
The degradation was never applied — all 10 runs produced baseline results
(reachable ~6s). These experiments need to be re-run using `full-cone` NAT type
so traffic traverses the router.

### macOS Docker Desktop Testbed (2026-02-21)

| File | NAT Type | Transport | Result | Notes |
|------|----------|-----------|--------|-------|
| `none-both-5-20260221T175717Z.json` | none | TCP | Reachable ~6s | QUIC stayed unknown |
| `port-restricted-tcp-5-20260221T222924Z.json` | port-restricted | TCP | Unreachable ~6s | Matches Heathrow |
| `symmetric-tcp-5-20260221T224118Z.json` | symmetric | TCP | v2 never fires | Matches flight |
| `address-restricted-tcp-5-20260221T225416Z.json` | address-restricted | TCP | Unreachable ~11s | INVALID (macOS DNAT bug) |
| `full-cone-tcp-5-20260221T231037Z.json` | full-cone | TCP | Unreachable ~11s | INVALID (macOS DNAT bug) |

Note: macOS results for full-cone and address-restricted are invalid due to
Docker Desktop macOS rewriting source IPs during cross-network DNAT (see
Issue #16a above). Linux VM results supersede these.

### Field Experiments (2026-02-16 to 2026-02-19)

| Location | Date | NAT Type | v2 Result | v1 Result | Runs |
|----------|------|----------|-----------|-----------|------|
| Heathrow airport | 2026-02-16 | Port-restricted (EIM, port-preserving) | Unreachable ~12s | Private ~17s | 4 |
| In-flight satellite | 2026-02-16 | Symmetric (ADPM) | Never fired | Private ~31s (1/3 oscillated) | 3 |
| Hotel WiFi | 2026-02-19 | Port-restricted (EIM, port-remapping, TCP blocked) | QUIC unreachable ~11s | Private (1/3 oscillated) | 3 |
