# NAT Monitoring: Real-World Validation of Testbed Findings

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

## Goal

Classify every active libp2p peer on the IPFS network by:

1. **NAT protocol supported** — v1 only, v2 only, both, or neither
2. **NAT type** — full-cone (EIF), address-restricted (ADF),
   port-restricted (APDF), or symmetric (ADPM)

---

## Tier 1: Query Existing Nebula Data (this project)

The [Nebula crawler](https://github.com/dennis-tra/nebula) already stores
per-peer protocol lists and agent versions. These queries require no code
changes and can be done now to contextualize the report findings.

### Data Confirmed Available

Nebula's PostgreSQL schema stores all required fields (verified from
`db/models/pg/` in [probe-lab/nebula](https://github.com/probe-lab/nebula)):

| Table | Field | Content |
|-------|-------|---------|
| `protocols` | `protocol` | Full protocol string (e.g., `/libp2p/autonat/2/dial-request`) |
| `protocols_sets` | `protocol_ids` | Protocol set per visit |
| `agent_versions` | `agent_version` | Agent string (e.g., `kubo/0.32.0/go-libp2p/0.47.0`) |
| `multi_addresses` | `maddr` | Full multiaddr (parseable for TCP/QUIC transport) |
| `multi_addresses` | `is_relay`, `is_public` | Relay and reachability flags |
| `multi_addresses` | `is_cloud`, `asn` | Cloud provider and AS number |
| `multi_addresses` | `country`, `continent` | Geolocation |
| `visits` | `protocols_set_id`, `agent_version_id`, `multi_address_ids` | Per-visit linkage |

### Queries

| Query | SQL sketch | Answers |
|-------|-----------|---------|
| AutoNAT v2 adoption | `WHERE protocols @> '/libp2p/autonat/2/dial-request'` | What % of peers can serve as v2 servers? |
| go-libp2p version distribution | `WHERE agent_version LIKE 'kubo%'`, parse version | What % run v0.42.0+ (v2 as primary)? |
| Platform distribution | Parse `agent_version`: `kubo/*` → Go, `helia/*` → JS, `*substrate*` → Rust | Implementation breakdown |
| v1-only vs v2-capable vs both | Cross-reference protocol sets | Network transition state |
| TCP vs QUIC address patterns | Parse `maddr` for `/tcp/` vs `/udp/.../quic-v1` | Typical address count → probe load |
| Relay-dependent peers | `WHERE is_relay = true` on addresses | Size of restrictive-NAT population |
| Cloud vs residential | `is_cloud` / `asn` segmentation | NAT analysis excluding servers |

### Effort

Days. SQL queries on existing ProbeLab Nebula database.

### Limitation

Nebula only sees DHT server-mode nodes. Peers behind restrictive NAT
(DHT clients) are invisible. This gives a biased view weighted toward
reachable nodes. Tier 1 answers "what protocols do reachable nodes
support?" but not "what NAT type are unreachable nodes behind?"

### Deliverable

A data appendix for the final report with real v2 adoption numbers,
contextualizing the testbed findings.

---

## Tier 2: Full NAT Classification via ants-watch (future work)

### Approach

Use [ants-watch](https://github.com/probe-lab/ants-watch) to deploy
sybil nodes across the full DHT keyspace on 2-3 VPS with different
public IPs. Instead of crawling to nodes (which misses NATted peers),
the sybils wait for peers to connect during normal DHT operations
(lookups, puts, gets).

Every active peer on the network eventually contacts a sybil as part
of DHT routing. This captures the **entire active population** —
including DHT client-mode nodes behind restrictive NAT that are
invisible to crawlers.

### NAT Protocol Classification

When a peer connects to a sybil, the identify protocol exchanges the
full protocol list. This directly answers:

- `/libp2p/autonat/1.0.0` → v1 support
- `/libp2p/autonat/2/dial-request` → v2 support
- Both, neither, or one only

Since sybils cover the full keyspace, any peer doing any DHT operation
hits at least one sybil. This gives protocol support classification
for **every active peer** on the network.

### NAT Type Classification

With sybils on 2-3 different IPs, the multi-vantage classification
works as follows:

**Step 1: Mapping behavior** — When the same peer connects to sybils
on different VPS, compare the observed external ports from identify:

```
VPS-1 sybil sees peer at 73.1.2.3:5001
VPS-2 sybil sees peer at 73.1.2.3:5001  → same port → EIM (cone NAT)
VPS-2 sybil sees peer at 73.1.2.3:8472  → different port → ADPM (symmetric)
```

A peer doing a single DHT lookup contacts ~20 peers across the keyspace.
With sufficient sybil coverage, most peers will hit sybils on 2+ VPS
naturally, enabling port comparison without any active probing.

**Step 2: Filtering behavior** (for EIM nodes) — A sybil on a VPS the
peer never contacted attempts to dial the peer's observed address:

```
VPS-3 sybil (never contacted by peer) dials 73.1.2.3:5001
  Success → EIF (full-cone)
  Failure → restricted filtering → continue to step 3
```

**Step 3: ADF vs APDF** — A sybil on a VPS the peer DID contact dials
from a different source port:

```
VPS-1 sybil (already contacted) dials from port 9999 to 73.1.2.3:5001
  Success → ADF (address-restricted — any port from known IP allowed)
  Failure → APDF (port-restricted — only exact IP:port allowed)
```

### Classification Matrix

| NAT type | Step 1 (port comparison) | Step 2 (unsolicited dial) | Step 3 (different port) |
|----------|------------------------|--------------------------|------------------------|
| **Full-cone** | Same port (EIM) | Success | — |
| **Address-restricted** | Same port (EIM) | Failure | Success |
| **Port-restricted** | Same port (EIM) | Failure | Failure |
| **Symmetric** | Different port (ADPM) | — | — |

### Timing Consideration

NAT mappings have a TTL (typically 30s-5min for UDP, longer for TCP).
The cross-vantage probing (steps 2-3) must happen while the mapping is
still alive. Sybils on different VPS should coordinate within seconds of
observing the same peer to ensure mapping validity.

### AutoNAT-Specific Metrics

Adding AutoNAT v2 server capability to the sybil nodes enables
additional metrics from peers that send AutoNAT requests:

| Metric | Method | Finding validated |
|--------|--------|-------------------|
| Amplification trigger rate | Log `DialDataRequest` events | Real-world byte overhead |
| v1 vs v2 request ratio | Count protocol streams | v2 adoption (complements Tier 1) |
| Dial-back success by transport | Log TCP vs QUIC outcomes | UDP black hole impact |
| Addresses per request | Log address count | Probe load model |
| Probe interval per peer | Log timestamps | Validates throttle/refresh assumptions |

### Infrastructure

- **VPS:** 2-3 nodes on different providers (Hetzner, Vultr, OVH —
  different ASNs for meaningful IP diversity)
- **Software:** ants-watch with extensions for:
  - Multi-vantage observed port coordination
  - Active filtering probes (steps 2-3)
  - AutoNAT v2 server with logging
  - Result storage and classification pipeline
- **Duration:** 2-4 weeks for stable sample (accounting for ~40%
  hourly peer churn)
- **Expected yield:** 50-100k unique peers with full NAT type
  classification + protocol support

### Relationship to Existing ProbeLab Infrastructure

| Component | Role |
|-----------|------|
| [ants-watch](https://github.com/probe-lab/ants-watch) | Sybil deployment framework — provides keyspace coverage |
| [Nebula](https://github.com/dennis-tra/nebula) | Tier 1 data source; Tier 2 results feed into same analysis pipeline |
| ProbeLab dashboards | Visualization of NAT type distribution over time |

### Deliverables

1. NAT type distribution for the IPFS network (first comprehensive
   measurement)
2. AutoNAT v2 adoption rate (from both crawl and sybil perspectives)
3. ADF prevalence — the key unknown behind the false positive finding
4. Symmetric NAT fraction — quantifies the autonat v2 blind spot
5. UDP blocking rate — real-world impact of black hole detector issue
6. Data suitable for a standalone measurement paper

---

## Key Questions (priority order)

1. **What % of IPFS peers support AutoNAT v2?** (Tier 1 — do now)
   If <10%, convergence is much slower than testbed's 6s.

2. **How common is address-restricted (ADF) NAT?** (Tier 2)
   If <5%, the false positive is an edge case. If >20%, protocol fix
   needed.

3. **What % of networks block UDP?** (Tier 2)
   Determines real-world QUIC dial-back failure rate.

4. **How often does v1 oscillate on the live network?** (Tier 2)
   What's the real fraction of unreliable peers on IPFS?

5. **What's the real amplification overhead?** (Tier 2)
   Testbed shows 0% (same subnet). Real-world NATted peers trigger
   30-100KB per probe.

---

## References

- [ants-watch](https://github.com/probe-lab/ants-watch) — ProbeLab's sybil deployment framework
- [Nebula](https://github.com/dennis-tra/nebula) — ProbeLab's DHT crawling tool
- [NAT Classification Crawl Idea](nat-classification-crawl-idea.md) — original technical design
- [ADF False Positive](adf-false-positive.md) — the finding that motivates ADF prevalence measurement
- [UDP Black Hole Detector](udp-black-hole-detector.md) — UDP blocking prevalence question
- Trautwein et al., "NAT Hole Punching in the Wild" (2022) — prior ProbeLab measurement work
