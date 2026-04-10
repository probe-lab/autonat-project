# Nebula Analysis — ClickHouse Queries

SQL queries that produce the CSVs used by `../plot.py` to generate the
charts in `docs/nebula-autonat-analysis.md`.

## Connection

The queries run against ProbeLab's public Nebula ClickHouse instance.
Connection details:

```
Host:     (ask ProbeLab for credentials)
Port:     9440 (native TLS)
Database: nebula_ipfs_amino (raw tables) / nebula_ipfs_amino_silver (change logs)
```

Example with the `clickhouse` CLI:

```bash
clickhouse client \
  --host <HOST> --user <USER> --password '<PASSWORD>' \
  --secure \
  --query "$(cat queries/01_clients.sql)" \
  --format CSVWithNames \
  > data/01_clients.csv
```

## Queries → CSVs → Charts

| Query file | CSV output | Chart | Doc figure |
|---|---|---|---|
| `01_clients.sql` | `data/01_clients.csv` | `02_clients.png` | Figure 2 |
| `02_autonat_protocols.sql` | `data/02_autonat_protocols.csv` | `03_autonat_protocols.png` | Figure 3 |
| `03_server_mode.sql` | `data/03_server_mode.csv` | `04_server_mode.png` | Figure 4 |
| `05_dialable_over_time.sql` | `data/05_dialable_over_time.csv` | `01_dialable_over_time.png` | Figure 1 |
| `06_oscillation_refined.sql` | `data/06_oscillation_refined.csv` | `06_oscillation_refined_appendix.png` | Appendix C.2 |
| `08_dialability_vs_public.sql` | `data/08_dialability_vs_public.csv` | `05_dialability_vs_public.png` | Figure 5 |

Note: CSV file names reflect the original query numbering order (01-08).
Chart file names were renumbered to match figure numbers in the doc.
Queries 04 and 07 are no longer used (superseded by 06 and 08).

## Regenerating all charts

```bash
# Run all queries (produces CSVs in data/)
for q in queries/*.sql; do
    csv="data/$(basename ${q%.sql}).csv"
    clickhouse client \
      --host <HOST> --user <USER> --password '<PASSWORD>' --secure \
      --query "$(cat $q)" --format CSVWithNames > "$csv"
    echo "$q → $csv"
done

# Generate PNGs from CSVs
python3 plot.py
```

## Tables used

| Table | Type | Contents |
|---|---|---|
| `nebula_ipfs_amino.crawls` | Raw | One row per crawl run (crawled/dialable/undialable counts) |
| `nebula_ipfs_amino.visits` | Raw | One row per peer per crawl (agent_version, protocols, connect_maddr) |
| `nebula_ipfs_amino_silver.peer_logs_protocols` | Change log | One row per protocol-set change per peer (deduped) |
| `nebula_ipfs_amino_silver.peer_logs_agent_version` | Change log | One row per agent_version change per peer |
