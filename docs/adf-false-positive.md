# Address-Restricted NAT (ADF) False Positive in AutoNAT v2

## Summary

AutoNAT v2 produces a **100% false positive rate** for nodes behind
address-restricted cone NAT (EIM + ADF). The node is reported as
publicly reachable when it is not reachable by arbitrary peers.

This is a protocol-level issue, not an implementation bug — the same
behavior occurs across any compliant AutoNAT v2 implementation.

---

## The Problem

### What is ADF?

Address-Dependent Filtering (ADF) is a NAT filtering behavior where
inbound traffic is allowed from **any port** of an IP address that the
internal host has previously contacted. In RFC 4787 terminology:

| Filtering type | Allows inbound from | Real-world reachability |
|---------------|--------------------|-----------------------|
| **EIF** (Full cone) | Any IP, any port | Truly reachable by anyone |
| **ADF** (Address-restricted) | Previously contacted IP, any port | Only reachable by servers you've talked to |
| **APDF** (Port-restricted) | Previously contacted IP:port only | Not reachable (dial-back uses different port) |

### Why AutoNAT v2 Gets It Wrong

AutoNAT v2's probe sequence:

1. Client connects to Server (TCP or QUIC)
2. Client sends DialRequest asking Server to probe its address
3. Server dials back from a **different port** (via `dialerHost`)
4. NAT evaluates: "Is this IP allowed?"

For ADF NAT, the answer is **yes** — the client already contacted the
server's IP in step 1. The NAT doesn't check the source port (unlike
APDF). The dial-back succeeds, and AutoNAT v2 reports REACHABLE.

But an arbitrary peer that the client has never contacted would be
**blocked** by the same NAT. The node is not truly publicly reachable.

### Diagram

```
Client (10.0.1.10) ──TCP──▶ Server (73.0.0.3:4001)
                              │
                    NAT adds 73.0.0.3 to "allowed IPs"
                              │
Server dialerHost (73.0.0.3:random) ──TCP──▶ Client's mapped addr
                              │
                    NAT checks: is 73.0.0.3 in allowed IPs? YES ✓
                              │
                    Dial-back succeeds → REACHABLE (false positive)

Random peer (73.0.0.99:any) ──TCP──▶ Client's mapped addr
                              │
                    NAT checks: is 73.0.0.99 in allowed IPs? NO ✗
                              │
                    Packet dropped → actually UNREACHABLE
```

---

## Testbed Evidence

### Scenario

File: `testbed/scenarios/adf-false-positive.yaml`

| Scenario | NAT type | Transport | Runs | Expected |
|----------|----------|-----------|------|----------|
| adf-tcp | address-restricted | tcp | 20 | Reachable (false positive) |
| adf-quic | address-restricted | quic | 20 | Reachable (false positive) |
| adf-both | address-restricted | both | 20 | Reachable (false positive) |
| apdf-tcp | port-restricted | tcp | 20 | Unreachable (correct) |
| apdf-quic | port-restricted | quic | 20 | Unreachable (correct) |
| apdf-both | port-restricted | both | 20 | Unreachable (correct) |

### Results (TCP, 20 runs each)

| NAT type | Reported reachable | Reported unreachable | FPR |
|----------|-------------------|---------------------|-----|
| **Address-restricted (ADF)** | **20/20** | 0/20 | **100%** |
| **Port-restricted (APDF)** | 0/20 | 20/20 | **0%** |

Every single ADF run reports the address as reachable. The false positive
rate is 100% — it is not intermittent or probabilistic. The protocol
design guarantees this outcome for ADF NATs because the probe always
comes from an IP the client has already contacted.

### Baseline Comparison (from full-matrix runs)

| NAT type | Result | TTC |
|----------|--------|-----|
| none | reachable | ~6s |
| full-cone | reachable | ~6s |
| **address-restricted** | **reachable (FP)** | **~6s** |
| port-restricted | unreachable | ~6s |
| symmetric | no signal | — |

The ADF false positive is indistinguishable from a correctly reachable
node — same TTC, same number of probes, same confidence level. There is
no way for the node to know it received a false positive.

---

## Real-World Prevalence of ADF

### How Common Is ADF NAT?

Based on available evidence, ADF is **rare in modern deployments**:

- **RFC 4787** (2007) recommends ADF as a compromise between security and
  transparency, but most vendors chose APDF (more secure) or EIF (more
  transparent) instead.

- **RFC 7857** (2016) updated recommendations further toward
  endpoint-independent filtering with protocol awareness, moving away
  from ADF.

- **Consumer routers**: Most (Netgear, ASUS, TP-Link) default to
  "secured" filtering which is APDF. "Open" mode is typically EIF, not
  ADF. ADF is rarely exposed as an option.

- **Tailscale's observation**: "In the wild we're overwhelmingly dealing
  only with IP-and-port endpoint-dependent firewalls" (APDF or symmetric).

- **No measurement study found** that quantifies ADF prevalence
  separately from APDF. The 2011 Halkes & Pouwelse study measured
  hole-punching success rates but didn't distinguish ADF from APDF
  filtering.

### Where ADF Might Still Exist

| Environment | Likelihood | Notes |
|-------------|-----------|-------|
| Modern home routers | Very low | Default to APDF ("secured") |
| Older firmware | Low | Some early NAT implementations used ADF |
| Enterprise firewalls | Low-medium | Custom rules may allow return traffic by IP |
| Some CGNAT deployments | Low | Operators may choose less restrictive filtering |
| Embedded/IoT gateways | Low | Simpler implementations may use ADF |
| Mobile carrier NAT | Unknown | Varies by operator, limited public data |

### Assessment

The ADF false positive is a **known protocol-level vulnerability** with
**likely low real-world impact** because:

1. Most deployed NATs use APDF (which correctly blocks dial-back)
2. ADF is not the default on any major consumer router brand
3. The RFC recommendations have moved away from ADF over time

However, this assessment is based on limited evidence — no comprehensive
measurement study of ADF prevalence exists. A NAT monitoring service
(see #79) could provide definitive data.

---

## Protocol-Level Analysis

### Why the Protocol Can't Distinguish ADF from Full Cone

From AutoNAT v2's perspective, ADF and full-cone NAT look identical:

| Behavior | Full cone (EIF) | Address-restricted (ADF) |
|----------|----------------|------------------------|
| Dial-back from contacted server | ✅ succeeds | ✅ succeeds |
| Dial-back from uncontacted server | ✅ succeeds | ❌ fails |

AutoNAT v2 only tests dial-back from servers the client has already
connected to (step 1 of the protocol requires an existing connection).
It never tests dial-back from an uncontacted IP, so it can't distinguish
EIF from ADF.

### Potential Protocol Fixes

1. **Multi-server verification with different IPs**: Require that the
   dial-back comes from a **different IP** than the one the client
   connected to. This would catch ADF (NAT would block the unknown IP).
   However, it requires servers with multiple IPs or cooperation between
   servers, which complicates the protocol.

2. **Third-party dial-back**: Have server A ask server B to perform the
   dial-back. Server B's IP was never contacted by the client, so ADF
   NAT would block it. This is closer to STUN's multi-server design but
   adds protocol complexity and trust requirements.

3. **Accept as known limitation**: Document that AutoNAT v2 cannot
   distinguish EIF from ADF, and note that ADF is rare. The false
   positive has limited practical impact because peers that connect to
   the node (and would thus be in the NAT's allowed list) are the ones
   most likely to need to reach it.

### Impact on DHT and Hole Punching

If AutoNAT v2 reports a node as reachable when it's behind ADF NAT:

- **DHT**: Node enters server mode and accepts queries. Peers that
  connect to it can reach it (their IP gets whitelisted). Peers that
  only have the node in their routing table but never connected may
  fail to reach it.

- **Hole punching**: Not triggered because the node believes it's
  reachable. This means peers behind restrictive NAT that need DCUtR
  to connect won't get relay-based fallback.

- **Practical impact**: Likely low. Most DHT interactions involve
  an initial connection (which whitelists the IP), and the node IS
  reachable by peers it has contacted. The gap is for unsolicited
  inbound from unknown peers.

---

## Comparison: How Each Implementation Handles ADF

| Implementation | ADF result | Notes |
|----------------|-----------|-------|
| **go-libp2p** | Reachable (FP) | Tested, confirmed 100% FPR |
| **rust-libp2p** | N/A | Ephemeral port issue prevents any address from being probed |
| **js-libp2p** | Expected reachable (FP) | Same protocol, same vulnerability |

The ADF false positive is inherent to the AutoNAT v2 protocol design,
not specific to any implementation.

---

## References

- [AutoNAT v2 Specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
- [RFC 4787: NAT Behavioral Requirements for Unicast UDP](https://www.rfc-editor.org/rfc/rfc4787) — REQ-8 filtering recommendations
- [RFC 7857: Updates to NAT Behavioral Requirements](https://www.rfc-editor.org/rfc/rfc7857)
- [Tailscale: How NAT Traversal Works](https://tailscale.com/blog/how-nat-traversal-works)
- [Halkes & Pouwelse: UDP NAT and Firewall Puncturing in the Wild (2011)](https://link.springer.com/chapter/10.1007/978-3-642-20798-3_1)
- [NAT Classification Crawl Idea](nat-classification-crawl-idea.md)
- Issue #36: Address-Restricted Cone NAT analysis
- Issue #57: Assess real-world prevalence of ADF NAT
- Issue #79: NAT monitoring service proposal
