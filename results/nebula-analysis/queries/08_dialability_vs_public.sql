-- 08_dialability_vs_public.sql — Dialability fraction × Public-state fraction (heatmap)
-- Output: 08_dialability_vs_public.csv (columns: dial_decile, public_decile, peers)
-- Chart: Figure 5 (05_dialability_vs_public.png)
--
-- For each Kubo peer with at least one silver observation in the 7-day window:
--   X-axis: fraction of 84 crawls in which Nebula successfully dialed the peer
--   Y-axis: fraction of silver-table observations in the Public state (kad+autonat v1 on)
-- Both axes bucketed into 11 deciles (0-10%, 10-20%, ..., 100%).
-- Peers never dialed by Nebula are excluded (they have no silver observations).

WITH
-- Count successful crawls in the window
crawl_count AS (
    SELECT count(*) AS total_crawls
    FROM nebula_ipfs_amino.crawls
    WHERE state = 'succeeded'
      AND created_at > now() - INTERVAL 7 DAY
),
-- Per Kubo peer: how many crawls was it dialed in?
peer_dialability AS (
    SELECT
        peer_id,
        countIf(connect_maddr IS NOT NULL) AS dialed_crawls,
        count(*) AS visited_crawls
    FROM nebula_ipfs_amino.visits v
    JOIN (
        SELECT id FROM nebula_ipfs_amino.crawls
        WHERE state = 'succeeded'
          AND created_at > now() - INTERVAL 7 DAY
    ) c ON v.crawl_id = c.id
    WHERE agent_version LIKE 'kubo/%'
    GROUP BY peer_id
    HAVING dialed_crawls > 0  -- exclude peers never dialed
),
-- Per Kubo peer: fraction of silver observations in Public state
peer_public_fraction AS (
    SELECT
        p.peer_id,
        count(*) AS total_obs,
        countIf(
            has(p.protocols, '/ipfs/kad/1.0.0')
            AND has(p.protocols, '/libp2p/autonat/1.0.0')
        ) AS public_obs,
        round(countIf(
            has(p.protocols, '/ipfs/kad/1.0.0')
            AND has(p.protocols, '/libp2p/autonat/1.0.0')
        ) * 1.0 / count(*), 1) AS public_fraction
    FROM nebula_ipfs_amino_silver.peer_logs_protocols p
    WHERE p.updated_at > now() - INTERVAL 7 DAY
    GROUP BY p.peer_id
)
SELECT
    floor(d.dialed_crawls * 10.0 / (SELECT total_crawls FROM crawl_count)) AS dial_decile,
    floor(pf.public_fraction * 10) AS public_decile,
    count(*) AS peers
FROM peer_dialability d
JOIN peer_public_fraction pf ON d.peer_id = pf.peer_id
GROUP BY dial_decile, public_decile
ORDER BY dial_decile, public_decile
