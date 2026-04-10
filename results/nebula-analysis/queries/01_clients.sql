-- 01_clients.sql — Client distribution from one recent crawl
-- Output: 01_clients.csv (columns: client, total, dialable)
-- Chart: Figure 2 (02_clients.png)
--
-- Buckets peers by agent_version into named clients.
-- Includes both dialable (connect_maddr IS NOT NULL) and undialable peers.

WITH latest_crawl AS (
    SELECT id
    FROM nebula_ipfs_amino.crawls
    WHERE state = 'succeeded'
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT
    multiIf(
        agent_version LIKE 'kubo/%', 'kubo',
        agent_version LIKE 'go-ipfs/%', 'go-ipfs (legacy)',
        agent_version LIKE 'storm%', 'storm (botnet)',
        agent_version LIKE '%harmony%', 'harmony',
        agent_version LIKE '%edgevpn%', 'edgevpn',
        agent_version LIKE 'rust-libp2p/%', 'rust-libp2p',
        agent_version LIKE 'js-libp2p/%', 'js-libp2p',
        agent_version = '' OR agent_version IS NULL, '(empty)',
        'other'
    ) AS client,
    count(*) AS total,
    countIf(connect_maddr IS NOT NULL) AS dialable
FROM nebula_ipfs_amino.visits
WHERE crawl_id = (SELECT id FROM latest_crawl)
GROUP BY client
ORDER BY total DESC
