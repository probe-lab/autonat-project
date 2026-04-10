-- 05_dialable_over_time.sql — Dialable peer counts over 30 days
-- Output: 05_dialable_over_time.csv (columns: day, avg_crawled, avg_dialable, avg_undialable, pct_dialable)
-- Chart: Figure 1 (01_dialable_over_time.png)
--
-- Daily averages across ~12 successful crawls per day.

SELECT
    toDate(created_at) AS day,
    avg(crawled_peers) AS avg_crawled,
    avg(dialable_peers) AS avg_dialable,
    avg(undialable_peers) AS avg_undialable,
    round(avg(dialable_peers) * 100.0 / avg(crawled_peers), 1) AS pct_dialable
FROM nebula_ipfs_amino.crawls
WHERE state = 'succeeded'
  AND created_at > now() - INTERVAL 30 DAY
GROUP BY day
ORDER BY day
