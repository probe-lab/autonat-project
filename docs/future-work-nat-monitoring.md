# Future Work: NAT Monitoring Service

**GitHub Issue:** [#79](https://github.com/probe-lab/autonat-project/issues/79)

---

## Motivation

Our testbed findings are based on controlled Docker environments with
known NAT types. Several findings depend on real-world prevalence data
that we don't have:

| Finding | What we need to know | Impact if common |
|---------|---------------------|-----------------|
| ADF false positive | How common is address-restricted NAT? | Protocol design flaw if >5% of nodes |
| Symmetric NAT silent failure | What % of nodes are behind symmetric NAT? | Large blind spot in autonat v2 |
| v1 oscillation | What fraction of DHT peers are unreliable for dial-back? | Determines real-world oscillation frequency |
| AutoNAT v2 adoption | How many peers support `/libp2p/autonat/2/dial-request`? | Affects convergence speed |
| UDP black hole | What % of networks block UDP? | Determines QUIC dial-back failure rate |

Without real-world measurements, we can only say "this is a problem in
theory" — not "this affects N% of the network."

---

## Proposed Approach: Three Tiers

### Tier 1: Query Existing Nebula Data (no new infrastructure)

The [Nebula crawler](https://github.com/dennis-tra/nebula) already stores
per-peer protocol lists and agent versions. These queries require no code
changes:

| Query | SQL sketch | Answers |
|-------|-----------|---------|
| AutoNAT v2 adoption | `WHERE protocols @> '/libp2p/autonat/2/dial-request'` | What % of peers can serve as v2 servers? |
| go-libp2p version distribution | `WHERE agent_version LIKE 'kubo%'`, parse version | What % run v0.42.0+ (v2 as primary)? |
| v1-only vs v2-capable vs both | Cross-reference protocol sets | Network transition state |
| TCP vs QUIC address patterns | Parse `listen_maddrs` | Typical address count → probe load |
| Relay-dependent peers | `WHERE is_relay = true` on addresses | Size of restrictive-NAT population |
| Cloud vs residential | `is_cloud` / `asn` segmentation | NAT analysis excluding servers |

**Effort:** Days. Just SQL queries on existing ProbeLab infrastructure.

**Limitation:** Nebula only sees DHT server-mode nodes. Peers behind
restrictive NAT (DHT clients) are invisible. This gives a biased view
weighted toward reachable nodes.

### Tier 2: Extend Nebula with Multi-Vantage Classification (moderate)

Run Nebula from 2-3 VPS nodes on different providers (different ASNs/IPs).
Compare observed external ports for the same peer across vantage points:

```
Vantage A sees peer X at 73.1.2.3:5001
Vantage B sees peer X at 73.1.2.3:5001  → same port → EIM (cone NAT)
Vantage B sees peer X at 73.1.2.3:8472  → different port → ADPM (symmetric)
```

Then test filtering by having an uncontacted vantage point probe the
known mapping:

```
Vantage C (never contacted by peer X) dials 73.1.2.3:5001
  Success → EIF (full cone)
  Failure → ADF or APDF
```

To distinguish ADF from APDF: vantage A (already contacted) dials from
a different port:

```
Vantage A (port 9999, not the original) dials 73.1.2.3:5001
  Success → ADF (address-dependent, any port from known IP)
  Failure → APDF (port-dependent, only exact IP:port)
```

**Effort:** Weeks. Needs 2-3 VPS, Nebula modifications to coordinate
cross-vantage classification, storage for multi-vantage observations.

**Value:** Directly answers "how common is ADF?" — the key unknown for
the false positive finding.

### Tier 3: Instrumented AutoNAT Server (best signal, most effort)

Run dedicated AutoNAT v2 servers and log every incoming request. This
captures the exact population that needs NAT traversal — NATted peers
that actively seek AutoNAT servers.

**Metrics collectible:**

| Metric | Method | Report finding validated |
|--------|--------|------------------------|
| NAT type per peer | Multi-vantage observed port comparison | ADF prevalence, symmetric prevalence |
| Amplification trigger rate | Log `DialDataRequest` events | Real-world byte overhead |
| v1 vs v2 request ratio | Count protocol streams | v2 adoption |
| Dial-back success by transport | Log outcomes (TCP vs QUIC) | UDP black hole real-world impact |
| Addresses per request | Log address count | Probe load model |
| Probe interval | Log timestamps per peer | Validates throttle/refresh assumptions |
| Refused dial rate | Count `E_DIAL_REFUSED` | Rate limiting impact |

**Setup:**
1. 2-3 VPS on different providers (Hetzner, Vultr, OVH)
2. Each runs Kubo with DHT server + AutoNAT server + relay + OTel export
3. Log every incoming AutoNAT request with full metadata
4. Cross-reference peer observations across vantage points
5. Run for 2-4 weeks for stable sample (accounting for ~40% hourly churn)

**Expected yield:** 50-100k unique peers with NAT type classification.

**Effort:** Months (including analysis). Needs VPS budget, custom
instrumentation, data pipeline.

**Value:** Definitive answers for all open prevalence questions. Could
be published as a standalone measurement paper.

---

## Relationship to Existing ProbeLab Infrastructure

ProbeLab already operates:

- **Nebula** — DHT crawler with rich per-peer data (protocols, versions,
  addresses, errors). Running continuously on the IPFS Amino DHT.
- **Parsec** — DHT performance measurement infrastructure
- **Probelab website** — dashboards for network metrics

The proposed monitoring service builds on this existing infrastructure.
Tier 1 requires no new code. Tier 2 extends Nebula. Tier 3 adds a new
instrumented node type.

### Integration with Nebula

Nebula's database schema already supports most of what Tier 1 needs.
The key extension for Tier 2 is correlating observations of the same
peer from multiple vantage points — Nebula currently operates from a
single vantage point per crawl.

For Tier 3, the instrumented AutoNAT server is a separate component
that feeds data into the same analysis pipeline. It complements Nebula
by capturing the NATted population that crawlers can't see.

---

## Priority and Sequencing

| Tier | Effort | Blocking question answered | Recommendation |
|------|--------|---------------------------|----------------|
| **1** | Days | v2 adoption rate, address patterns | **Do first** — immediate value, no infrastructure needed |
| **2** | Weeks | ADF prevalence, NAT type distribution | **Do second** — answers the key ADF question |
| **3** | Months | All metrics, publication-quality data | **Longer-term** — full monitoring service |

Tier 1 should be done immediately as part of the final report — even
rough numbers for v2 adoption rate would contextualize the findings
significantly. Tiers 2 and 3 are follow-up projects.

---

## Key Questions to Answer

In priority order:

1. **What % of IPFS peers support AutoNAT v2?** If <10%, convergence
   is much slower than testbed's 6s (need 3 v2 servers, but only 1 in
   10 peers supports it).

2. **How common is address-restricted (ADF) NAT?** If <5%, the false
   positive is an edge case. If >20%, it's a protocol design problem
   requiring a fix.

3. **What % of networks block UDP?** Determines real-world impact of
   the black hole detector issue on QUIC dial-back.

4. **How often does v1 oscillate on the live network?** The testbed
   shows 3/5 runs oscillate with 71% unreliable servers — but what's
   the real fraction of unreliable peers on IPFS?

5. **What's the real amplification overhead?** Testbed shows 0% (same
   subnet). Real-world NATted peers would trigger 30-100KB per probe.

---

## References

- [NAT Classification Crawl Idea](nat-classification-crawl-idea.md) — detailed technical design
- [Nebula crawler](https://github.com/dennis-tra/nebula) — ProbeLab's DHT crawling tool
- [ADF False Positive](adf-false-positive.md) — the finding that motivates ADF prevalence measurement
- [UDP Black Hole Detector](udp-black-hole-detector.md) — UDP blocking prevalence question
- Trautwein et al., "NAT Hole Punching in the Wild" (2022) — prior ProbeLab measurement work
- Issue #79
