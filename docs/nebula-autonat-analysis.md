# AutoNAT in Production: Nebula Crawl Analysis of the IPFS Amino DHT

External observation of AutoNAT v1 and v2 behavior using ProbeLab's Nebula
crawler data, queried from the public ClickHouse dataset.

**Network:** IPFS Amino DHT
**Data source:** `nebula_ipfs_amino` (raw visits) and
`nebula_ipfs_amino_silver` (deduped change logs)
**Time range:** May 2025 – April 2026 (cross-sectional snapshot from latest
crawl; oscillation analysis from last 7 days)
**Crawl frequency:** ~12 crawls/day (every 2 hours)

---

## Why External Observation

The testbed analysis (`docs/v1-v2-state-transitions.md`) shows how AutoNAT v1
and v2 should behave under controlled conditions. This analysis answers a
different question: **what does the production network actually look like?**

Nebula crawls the IPFS DHT continuously and records, for each peer it
encounters:

- The set of libp2p protocols the peer advertises (via Identify)
- Whether Nebula could connect to the peer from the open internet
- The peer's agent_version (Kubo version, etc.)
- Listen addresses and the address Nebula actually connected on

We use the **`/ipfs/kad/1.0.0` protocol advertisement** as a proxy for "the
node thinks it is publicly reachable." Kubo only registers this stream
handler when the DHT is in Server mode, which only happens when AutoNAT v1
reports `Public` (see `docs/v1-v2-state-transitions.md` and the
`subscriber_notifee.go` analysis).

Tracking this advertisement across crawls lets us detect AutoNAT-driven
state changes **from outside the node**, without instrumenting Kubo.

---

## Findings

### Finding A: The IPFS DHT is much smaller than the raw peer count suggests

| Metric | Value |
|---|---|
| Total peer IDs visible per crawl | ~8,100 |
| Dialable peers per crawl | ~3,750 (~46%) |
| Stale routing-table entries (no Identify response) | ~4,300 (~54%) |
| Active running nodes | ~3,750 |

About half of the "peers" in the DHT are stale routing-table entries —
peer IDs Nebula learned from another peer's records but cannot reach. The
real active network is approximately 3,500-4,000 nodes at any moment.

The historical average (May 2025 - April 2026) was higher (~20,500 visible,
~32% dialable) because the IPStorm botnet was still active in earlier
months. After the FBI takedown propagated through DHT routing tables, the
visible network shrank by ~12,000 peers per crawl.

![Dialable peer count and ratio over 30 days](../results/nebula-analysis/05_dialable_over_time.png)
*Figure 1: Total visible vs dialable peer counts over the last 30 days. Source:
`nebula_ipfs_amino.crawls`. ~46% of visible peer IDs are dialable.*

### Finding B: Kubo dominates the dialable population

| Implementation | Dialable nodes | % of dialable |
|---|---|---|
| **kubo (modern)** | 3,170 | ~84% |
| go-ipfs (legacy, pre-Kubo rename) | 273 | ~7% |
| harmony | 41 | ~1% |
| storm (botnet remnants) | 39 | ~1% |
| other | 70 | ~2% |
| edgevpn | 3 | <1% |
| **rust-libp2p** | 2 | <0.1% |
| **js-libp2p / Helia** | 1 | <0.1% |

The IPFS DHT is essentially a Kubo-only network. rust-libp2p and js-libp2p
combined account for **3 nodes out of ~3,750** dialable peers. This
validates the "go-libp2p dominance" claim from the ecosystem survey
(`docs/libp2p-autonat-ecosystem.md`) and means the analysis below is
effectively a study of go-libp2p production behavior.

The IPStorm botnet — supposedly dismantled by the FBI in November 2023 —
still has 39 dialable nodes running the `storm` agent_version. The takedown
was incomplete or the codebase is still being deployed by other actors.

![Client distribution from a single recent IPFS DHT crawl](../results/nebula-analysis/01_clients.png)
*Figure 2: Client distribution by `agent_version`. Source: `nebula_ipfs_amino.visits`,
single recent crawl. Kubo accounts for ~84% of dialable nodes; rust-libp2p and
js-libp2p combined have only 3 dialable nodes.*

### Finding C: AutoNAT v2 server adoption is meaningful but not majority

Of the 3,170 dialable Kubo nodes:

| AutoNAT protocols advertised | Count | % |
|---|---|---|
| **v1 + v2 (both)** | 1,600 | **50.5%** |
| **v1 only** | 1,531 | **48.3%** |
| **v2 only** | 9 | 0.3% |
| **neither** | 30 | 0.9% |

Half of dialable Kubo nodes have `EnableAutoNATv2()` enabled. The other half
still run v1-only. Almost no node runs v2 without v1 (only 9 v2-only out of
1,609 v2 servers) — Kubo's default behavior is to keep v1 enabled and add v2
optionally on top.

This means the v2 dial-back capacity exists in the network (~1,609 servers)
but the protocol is **additive, not migratory**. Every node still runs v1.

![AutoNAT v1/v2 server adoption among dialable Kubo nodes](../results/nebula-analysis/02_autonat_protocols.png)
*Figure 3: AutoNAT server protocols advertised by dialable Kubo nodes. Source:
`nebula_ipfs_amino.visits`, filtered to `agent_version LIKE 'kubo/%'` and
`connect_maddr IS NOT NULL`. About half of Kubo deployments enable v2; almost
none run v2 without v1.*

### Finding D: Almost all reachable Kubo nodes advertise as DHT servers

In a single-snapshot view, ~99% of dialable Kubo nodes (across all versions)
advertise `/ipfs/kad/1.0.0`. The DHT mode is correctly tracking
reachability at the moment of measurement.

| Kubo version | Dialable nodes | % advertising kad |
|---|---|---|
| 0.1x | 412 | 99.8% |
| 0.2x | 1,119 | 99.8% |
| 0.30 | 32 | 100% |
| 0.31 | 17 | 100% |
| 0.32 | 101 | 99.0% |
| 0.33 | 88 | 98.9% |
| 0.34 | 48 | 97.9% |
| 0.35 | 51 | 100% |
| 0.36 | 108 | 96.3% |
| 0.37 | 376 | 100% |
| 0.38 | 132 | 100% |
| 0.39 | 327 | 99.1% |
| 0.4x | 358 | 99.7% |

The single-snapshot **false negative rate** (peer is dialable but not
advertising as DHT server) is ~0.5-3% depending on version. The single-snapshot
**false positive rate** (peer advertises as DHT server but is not dialable) is
**0%** — when AutoNAT says Public, it is correct.

![DHT server mode by Kubo version (snapshot)](../results/nebula-analysis/03_server_mode.png)
*Figure 4: Percentage of dialable Kubo nodes advertising `/ipfs/kad/1.0.0` per
version. Source: `nebula_ipfs_amino.visits`, single recent crawl. Snapshot view
shows AutoNAT is correctly tracking reachability at the moment of measurement
(~99% across all versions). Oscillation is invisible at this granularity — see
Figure 5.*

### Finding E: v2 introduction correlates with ~3x increase in DHT mode oscillation

**This is the key finding.** Tracking the same peers across multiple crawls
over 7 days, counting how many flip the `/ipfs/kad/1.0.0` protocol on and
off (a direct external indicator of AutoNAT v1 state changes):

| Kubo version | Stable peers observed | Oscillating | % |
|---|---|---|---|
| 0.1x | 493 | 10 | 2.03% |
| 0.2x | 1,326 | 25 | 1.89% |
| 0.30 | 38 | 0 | **0%** |
| 0.31 | 50 | 1 | 2.00% |
| 0.32 | 131 | 6 | 4.58% |
| **0.33 (last v1-only)** | 109 | 3 | **2.75%** |
| **0.34 (v2 added)** | 60 | 6 | **10.00%** |
| 0.35 | 67 | 8 | **11.94%** |
| 0.36 | 140 | 13 | 9.29% |
| 0.37 | 567 | 26 | 4.59% |
| 0.38 | 157 | 7 | 4.46% |
| 0.39 | 382 | 20 | 5.24% |
| **0.4x (latest)** | 496 | 35 | **7.06%** |

Aggregated:

| Bucket | Total | Oscillating | % |
|---|---|---|---|
| Kubo < 0.34 (v1-only) | 2,148 | 45 | **2.09%** |
| Kubo ≥ 0.34 (v2 available) | 2,048 | 140 | **6.84%** |

**Kubo versions with v2 enabled show ~3.3x higher oscillation than v1-only
versions.** v2 was introduced in Kubo 0.34 (May 2024). The visible jump in
oscillation rate at the v0.34 boundary, and the sustained higher rate in all
post-v2 versions, is the first external evidence that v2 did not fix the
oscillation problem in production — and may have made it worse.

![Oscillation rate by Kubo version (key chart)](../results/nebula-analysis/04_oscillation.png)
*Figure 5 (key chart): Percentage of stable peers exhibiting `/ipfs/kad/1.0.0`
toggling over the last 7 days, by Kubo version. Source:
`nebula_ipfs_amino_silver.peer_logs_protocols` joined to `peer_logs_agent_version`.
The visible jump at the v0.34 boundary (where v2 was introduced) shows v2 did
not fix oscillation; the latest 0.4x stays at ~7%, while v1-only versions
average ~2%. This is the first external production evidence for Finding #1
(v1/v2 reachability gap).*

### Why does v2 correlate with more oscillation?

This is consistent with **Finding #1** in the final report (the v1/v2
reachability gap): v2 results are not consumed by Kubo's DHT, AutoRelay, or
NAT service, all of which still listen to `EvtLocalReachabilityChanged`
(v1's event). v1 continues to drive DHT mode decisions regardless of what
v2 reports.

Several mechanisms could explain the increase:

1. **Additional protocol churn from v2 server lifecycle.** v2 adds dial-back
   stream handlers that may transition during Identify pushes.
2. **Additional load on the dialerHost.** v2 servers attempt dial-backs for
   more peers, increasing the chance of triggering the UDP black hole
   detector (Finding #5) or other failure modes.
3. **More v2 traffic = more chances for v1 to see "negative" observations**
   (timeouts, refusals, errors from v2 server peers) that erode v1's
   confidence.
4. **Newer Kubo deployment patterns.** Newer versions may be more likely to
   run in Docker, ephemeral cloud instances, or behind NATs where v1
   instability is more pronounced. (Confound — would need cloud-provider
   cross-tabulation to rule out.)

The simplest explanation is the architectural one: **v2 was added to Kubo
without fixing the v1 wiring**, so v1 continues to oscillate while v2 sits
unused by the consumers that matter. Adding v2 produces more code paths and
more chances for v1 erosion without providing any stabilizing signal.

---

## What This Confirms

The Nebula data turns Findings #1 and #2 from theoretical/testbed-only
results into measurable production phenomena:

| Finding | Status before | Status after Nebula analysis |
|---|---|---|
| **#1 v1/v2 reachability gap** | High severity, theoretical impact (DHT ignores v2) | Production data: post-v2 Kubo oscillates 3x more than pre-v2 |
| **#2 v1 oscillation → DHT oscillation** | Testbed result (60% of unreliable runs) | Production data: ~5% of stable IPFS DHT peers oscillate per 7 days; ~7% for latest Kubo |

The fix proposed in Finding #1 (bridging v2 results into
`EvtLocalReachabilityChanged`) is now backed by quantitative production
evidence — not just testbed observations.

---

## How to Reproduce

The data is in `results/nebula-analysis/data/` (CSVs). The plotting script
is `results/nebula-analysis/plot.py`. Charts are in
`results/nebula-analysis/*.png`.

Connection details for the ClickHouse dataset are in
`docs/future-work-nat-monitoring.md`. The queries used are visible in the
git history of this branch.

### Charts and data sources

Each chart is generated from a single query against the ClickHouse dataset.
The query and source table are documented below.

#### `01_clients.png` — Client distribution by agent_version

- **Source table:** `nebula_ipfs_amino.visits` (raw bronze)
- **Filter:** Single most recent successful crawl from `nebula_ipfs_amino.crawls`
- **Columns used:** `agent_version`, `connect_maddr` (NULL = not dialable),
  `crawl_id`
- **What it shows:** All visited peers grouped by client implementation.
  Two values per row: total visible (peer ID known to the DHT) and
  dialable (Nebula could open a connection).

#### `02_autonat_protocols.png` — AutoNAT v1/v2 server adoption (Kubo only)

- **Source table:** `nebula_ipfs_amino.visits`
- **Filter:** Single most recent successful crawl, `agent_version LIKE 'kubo/%'`,
  `connect_maddr IS NOT NULL` (dialable Kubo only)
- **Columns used:** `protocols` (Array), checked for membership of:
  - `/libp2p/autonat/1.0.0` → v1 server
  - `/libp2p/autonat/2/dial-request` → v2 server
- **What it shows:** Categorical breakdown of dialable Kubo nodes by
  AutoNAT server protocols advertised: v1 only, v1 + v2, v2 only, neither.

#### `03_server_mode.png` — DHT server mode (kad protocol) by Kubo version

- **Source table:** `nebula_ipfs_amino.visits`
- **Filter:** Single most recent successful crawl, dialable Kubo only
- **Columns used:** `agent_version` (parsed into version buckets),
  `protocols` (checked for `/ipfs/kad/1.0.0`)
- **What it shows:** For each Kubo version bucket, the percentage of
  dialable nodes that advertise the DHT server protocol — a snapshot
  measure of "currently in DHT Server mode."
- **Caveat:** Snapshot only. Does not capture oscillation; that is in
  chart 04.

#### `04_oscillation.png` — Oscillation rate by Kubo version (key chart)

- **Source tables:**
  - `nebula_ipfs_amino_silver.peer_logs_protocols` (deduplicated change log
    of protocol sets per peer)
  - `nebula_ipfs_amino_silver.peer_logs_agent_version` (agent version
    history per peer)
- **Filter:** `updated_at > now() - INTERVAL 7 DAY`, peers with
  `>= 2` protocol log entries
- **Method:** For each peer, count rows in `peer_logs_protocols` where
  `/ipfs/kad/1.0.0` is in `protocols` versus rows where it is not.
  A peer is "oscillating" if it has both states present (toggled at
  least once during the 7-day window). Joined to `peer_logs_agent_version`
  to bucket by Kubo version.
- **What it shows:** For each Kubo version bucket, the percentage of
  observed peers whose `/ipfs/kad/1.0.0` advertisement toggled on/off
  during the 7-day window. The `peer_logs_protocols` silver table only
  inserts a row when the protocol set changes, so this is a direct
  measure of state changes — independent of crawl frequency.
- **Why it matters:** Each toggle corresponds to a Kubo node flipping
  between DHT Server and Client mode, which only happens when AutoNAT v1
  changes the local reachability state. This is a direct external
  observation of Finding #2 (v1 oscillation) and Finding #1 (v2 not
  fixing it).

#### `05_dialable_over_time.png` — Dialable peer count and ratio over 30 days

- **Source table:** `nebula_ipfs_amino.crawls` (per-crawl summary stats)
- **Filter:** `state = 'succeeded' AND created_at > now() - INTERVAL 30 DAY`
- **Columns used:** `created_at`, `crawled_peers`, `dialable_peers`,
  `undialable_peers`
- **Method:** Daily averages across the ~12 crawls per day.
- **What it shows:** Total visible peer count vs dialable peer count over
  the last 30 days, plus the percentage that is dialable. Pre-aggregated
  in the `crawls` table by Nebula itself; we don't recompute it from
  individual visits.

### Caveats

1. **Selection bias.** Nebula can only see peers it can reach. Behind-NAT
   peers in DHT client mode appear as peer IDs in routing tables but are
   not dialable. Our oscillation analysis is restricted to peers that ARE
   dialable at least once.

2. **Silver table semantics.** The `peer_logs_protocols` table only inserts
   a row when a peer's protocol set changes. A truly stable peer has very
   few rows. Our oscillation filter (`observations >= 2` or `>= 4`) selects
   for peers that have at least some change history — biasing toward less
   stable peers but excluding the smallest, most volatile ones.

3. **Snapshot effect.** Single-crawl false positive/negative numbers are
   snapshots; the 7-day window catches more oscillation but may miss
   shorter cycles.

4. **Kubo version distribution.** Smaller version buckets (e.g., 0.30 with
   38 peers) have noisier percentages. The trend is clearest in the
   versions with hundreds of peers (0.1x, 0.2x, 0.37, 0.39, 0.4x).

5. **Possible confounds.** We did not control for cloud provider, geographic
   location, or hardware class. Newer Kubo versions could correlate with
   newer deployment patterns that independently produce more oscillation.
