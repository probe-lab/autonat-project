-- 06_oscillation_refined.sql — kad-only vs AutoNAT-driven flip rate by Kubo version
-- Output: 06_oscillation_refined.csv (columns: version, total, kad_toggling, autonat_driven, pct_kad, pct_autonat)
-- Chart: Appendix C.2 (06_oscillation_refined_appendix.png)
--
-- Per Kubo version, counts peers whose protocol set toggles over a 7-day window.
-- "kad toggling" = /ipfs/kad/1.0.0 appears and disappears in silver table.
-- "AutoNAT-driven" = tighter: peer has both Public (kad+autonat on) and
--   non-Public (Unknown or Private) states in the window.
--
-- Uses the silver change-log table (peer_logs_protocols) which only
-- contains rows from successful Identify exchanges.

WITH
-- Get Kubo version for each peer from the agent version silver table
peer_versions AS (
    SELECT
        peer_id,
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
        ) AS version
    FROM nebula_ipfs_amino_silver.peer_logs_agent_version
    WHERE updated_at > now() - INTERVAL 7 DAY
      AND agent_version LIKE 'kubo/%'
),
-- For each peer, classify each silver protocol observation
peer_states AS (
    SELECT
        p.peer_id,
        pv.version,
        has(p.protocols, '/ipfs/kad/1.0.0') AS has_kad,
        has(p.protocols, '/libp2p/autonat/1.0.0') AS has_autonat_v1,
        -- State classification:
        -- Public = kad ON, autonat v1 ON
        -- Unknown = kad OFF, autonat v1 ON
        -- Private = kad OFF, autonat v1 OFF
        -- Inconsistent = kad ON, autonat v1 OFF
        multiIf(
            has(p.protocols, '/ipfs/kad/1.0.0') AND has(p.protocols, '/libp2p/autonat/1.0.0'), 'Public',
            NOT has(p.protocols, '/ipfs/kad/1.0.0') AND has(p.protocols, '/libp2p/autonat/1.0.0'), 'Unknown',
            NOT has(p.protocols, '/ipfs/kad/1.0.0') AND NOT has(p.protocols, '/libp2p/autonat/1.0.0'), 'Private',
            'Inconsistent'
        ) AS state
    FROM nebula_ipfs_amino_silver.peer_logs_protocols p
    JOIN peer_versions pv ON p.peer_id = pv.peer_id
    WHERE p.updated_at > now() - INTERVAL 7 DAY
),
-- Per-peer summary: did kad toggle? did autonat-driven flip happen?
peer_summary AS (
    SELECT
        peer_id,
        version,
        -- kad toggling: peer has both kad-on and kad-off observations
        (max(has_kad) = 1 AND min(has_kad) = 0) AS kad_toggles,
        -- AutoNAT-driven: peer has both Public and non-Public (Unknown or Private) states
        (hasAny(groupArray(state), ['Public']) AND hasAny(groupArray(state), ['Unknown', 'Private'])) AS is_autonat_driven
    FROM peer_states
    GROUP BY peer_id, version
    HAVING count(*) >= 2  -- at least 2 observations
)
SELECT
    version,
    count(*) AS total,
    sum(kad_toggles) AS kad_toggling,
    sum(is_autonat_driven) AS autonat_driven,
    round(sum(kad_toggles) * 100.0 / count(*), 2) AS pct_kad,
    round(sum(is_autonat_driven) * 100.0 / count(*), 2) AS pct_autonat
FROM peer_summary
GROUP BY version
ORDER BY version
