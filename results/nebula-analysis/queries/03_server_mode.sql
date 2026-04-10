-- 03_server_mode.sql — DHT kad protocol presence by Kubo version (snapshot)
-- Output: 03_server_mode.csv (columns: version, total, kad_server, pct_server)
-- Chart: Figure 4 (04_server_mode.png)
--
-- For each Kubo version bucket, counts dialable peers advertising /ipfs/kad/1.0.0.

WITH latest_crawl AS (
    SELECT id
    FROM nebula_ipfs_amino.crawls
    WHERE state = 'succeeded'
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT
    multiIf(
        agent_version LIKE 'kubo/0.1%', '0.1x',
        agent_version LIKE 'kubo/0.2%', '0.2x',
        agent_version LIKE 'kubo/0.30%', '0.30',
        agent_version LIKE 'kubo/0.31%', '0.31',
        agent_version LIKE 'kubo/0.32%', '0.32',
        agent_version LIKE 'kubo/0.33%', '0.33',
        agent_version LIKE 'kubo/0.34%', '0.34',
        agent_version LIKE 'kubo/0.35%', '0.35',
        agent_version LIKE 'kubo/0.36%', '0.36',
        agent_version LIKE 'kubo/0.37%', '0.37',
        agent_version LIKE 'kubo/0.38%', '0.38',
        agent_version LIKE 'kubo/0.39%', '0.39',
        '0.4x'
    ) AS version,
    count(*) AS total,
    countIf(has(protocols, '/ipfs/kad/1.0.0')) AS kad_server,
    round(countIf(has(protocols, '/ipfs/kad/1.0.0')) * 100.0 / count(*), 2) AS pct_server
FROM nebula_ipfs_amino.visits
WHERE crawl_id = (SELECT id FROM latest_crawl)
  AND agent_version LIKE 'kubo/%'
  AND connect_maddr IS NOT NULL
GROUP BY version
ORDER BY version
