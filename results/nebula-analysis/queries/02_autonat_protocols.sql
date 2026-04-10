-- 02_autonat_protocols.sql — AutoNAT v1/v2 server protocols among dialable Kubo
-- Output: 02_autonat_protocols.csv (columns: category, cnt)
-- Chart: Figure 3 (03_autonat_protocols.png)
--
-- Classifies dialable Kubo nodes by which autonat server protocols they advertise.

WITH latest_crawl AS (
    SELECT id
    FROM nebula_ipfs_amino.crawls
    WHERE state = 'succeeded'
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT
    multiIf(
        has(protocols, '/libp2p/autonat/1.0.0') AND has(protocols, '/libp2p/autonat/2/dial-request'), 'v1 + v2',
        has(protocols, '/libp2p/autonat/1.0.0') AND NOT has(protocols, '/libp2p/autonat/2/dial-request'), 'v1 only',
        NOT has(protocols, '/libp2p/autonat/1.0.0') AND has(protocols, '/libp2p/autonat/2/dial-request'), 'v2 only',
        'neither'
    ) AS category,
    count(*) AS cnt
FROM nebula_ipfs_amino.visits
WHERE crawl_id = (SELECT id FROM latest_crawl)
  AND agent_version LIKE 'kubo/%'
  AND connect_maddr IS NOT NULL
GROUP BY category
ORDER BY cnt DESC
