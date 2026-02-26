# Running Experiments

Experiments are defined as YAML scenario files and executed by a single runner
script (`run.sh`). Each scenario file specifies NAT types, transports, server
counts, network conditions, and optional assertions. The runner expands
matrices, starts Docker containers, monitors for convergence, evaluates
assertions, and collects results.

## Prerequisites

See [docs/testbed.md](../docs/testbed.md#requirements) for host requirements
(Linux only) and software installation instructions.

## Quick Start

```bash
# Preview the full matrix (40 scenarios) without running anything
./testbed/run.sh testbed/scenarios/matrix.yaml --dry-run

# Run a single scenario: no NAT, QUIC, 5 servers
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none,transport=quic,server_count=5

# Run the full matrix
./testbed/run.sh testbed/scenarios/matrix.yaml

# Run flight WiFi reproduction (3 runs with assertions)
./testbed/run.sh testbed/scenarios/flight-wifi.yaml

# Run with custom timeout and extra runs
./testbed/run.sh testbed/scenarios/matrix.yaml --timeout=180 --runs=3
```

## Scenario Files

All scenario files live in `testbed/scenarios/`:

| File | Scenarios | Experiments | What it tests |
|------|-----------|-------------|---------------|
| `matrix.yaml` | 40 | 1-7, 11-12 | Full NAT type x transport x server count matrix |
| `packet-loss.yaml` | 6 | 9 | Packet loss at 1%, 5%, 10% (full-cone, both transports) |
| `high-latency.yaml` | 4 | 10 | Latency at 200ms, 500ms one-way (full-cone, both transports) |
| `hotel-wifi.yaml` | 1 | 13 | TCP blocked on port 4001, port remap 4001:29538 |
| `flight-wifi.yaml` | 3 runs | 14 | Symmetric NAT + 350ms latency (700ms RTT) |

The last two include assertions that automatically validate expected behavior
(e.g., "AutoNAT v2 should never fire under symmetric NAT").

See [testbed.md](../docs/testbed.md) for detailed descriptions
of each experiment, iptables rules, and the NAT verification test suite.

### matrix.yaml Breakdown

`matrix.yaml` is the Cartesian product of:

- **NAT types** (5): `none`, `full-cone`, `address-restricted`, `port-restricted`, `symmetric`
- **Transports** (2): `tcp`, `quic`
- **Server counts** (4): `3`, `5`, `7`, `ipfs-network`

This produces 40 scenarios covering:

| Scenarios | Server count | Description |
|-----------|--------------|-------------|
| 30 local | 3, 5, 7 | All NAT types × both transports × 3 server counts |
| 10 network | ipfs-network | All NAT types × both transports against real IPFS DHT |

Use `--filter` to run the subsets that correspond to individual experiments:

```bash
# Exp 1: Baseline true positive (no NAT, 5 servers)
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none,server_count=5

# Exp 2: Baseline true negative (symmetric NAT, 5 servers)
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=symmetric,server_count=5

# Exp 3: NAT type matrix, TCP only
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=transport=tcp,server_count=5

# Exp 4: NAT type matrix, QUIC only
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=transport=quic,server_count=5

# Exp 5: Address-restricted deep dive (5 runs)
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=address-restricted,server_count=5 --runs=5

# Exp 6-7: Server count impact (all server counts for a given NAT type)
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=symmetric

# Exp 11-12: Public server experiments only
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=ipfs-network

# All 30 local experiments (exclude ipfs-network)
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=3
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=5
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=7

# Single NAT type across all server counts and transports
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=port-restricted
```

## Runner Usage

```bash
./testbed/run.sh <scenario.yaml> [options]
```

| Option | Description |
|--------|-------------|
| `--dry-run` | Print expanded scenario table and exit (no Docker) |
| `--timeout=N` | Override timeout per scenario in seconds |
| `--runs=N` | Override number of runs per scenario |
| `--filter=K=V,...` | Filter to matching scenarios (AND logic) |

### Filtering

Filters select scenarios where all specified fields match. Comma-separated
key=value pairs are combined with AND logic:

```bash
# Only symmetric NAT scenarios
--filter=nat_type=symmetric

# Only QUIC with 5 servers
--filter=transport=quic,server_count=5

# Only real IPFS network experiments
--filter=server_count=ipfs-network

# Combine with --runs for a deep dive
./testbed/run.sh testbed/scenarios/matrix.yaml \
    --filter=nat_type=address-restricted,server_count=5 --runs=5
```

### Dry Run

`--dry-run` expands the YAML (including matrix Cartesian products and filters)
and prints a table without starting any containers:

```
$ ./testbed/run.sh testbed/scenarios/packet-loss.yaml --dry-run

Scenario file: packet-loss.yaml (6 scenarios)

  #    NAT Type             Transport  Servers        Loss   Latency  Runs
  1    full-cone            tcp        7              1%     0ms      1
  2    full-cone            tcp        7              5%     0ms      1
  3    full-cone            tcp        7              10%    0ms      1
  4    full-cone            quic       7              1%     0ms      1
  5    full-cone            quic       7              5%     0ms      1
  6    full-cone            quic       7              10%    0ms      1
```

## YAML Schema

Scenario files use either an explicit scenario list or a matrix that expands
via Cartesian product:

```yaml
name: string              # Required. Identifier used in result directory names.
description: string       # Optional. Human-readable explanation.

defaults:                 # Optional. Merged into every scenario.
  timeout_s: 120
  runs: 1

# Option A: explicit scenarios
scenarios:
  - nat_type: symmetric
    transport: both
    server_count: 5
    latency_ms: 350
    runs: 3
    assertions: [...]

# Option B: matrix (Cartesian product of all lists)
matrix:
  nat_type: [none, full-cone, address-restricted, port-restricted, symmetric]
  transport: [tcp, quic]
  server_count: [3, 5, 7, ipfs-network]
```

### Scenario Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `nat_type` | yes | — | `none`, `full-cone`, `address-restricted`, `port-restricted`, `symmetric` |
| `transport` | yes | — | `tcp`, `quic`, `both` |
| `server_count` | yes | — | `3`, `5`, `7`, or `ipfs-network` (real IPFS DHT) |
| `packet_loss` | no | 0 | Packet loss % applied via `tc netem` on router |
| `latency_ms` | no | 0 | One-way latency in ms via `tc netem` (RTT = 2x) |
| `tcp_block_port` | no | — | Block outbound TCP to this port on router |
| `port_remap` | no | — | Remap source port `"internal:external"` (e.g. `"4001:29538"`) |
| `timeout_s` | no | 120 | Max seconds to wait for convergence |
| `runs` | no | 1 | Repeat this scenario N times |
| `obs_addr_thresh` | no | auto | Observer address activation threshold (auto: 2 if servers<4, else 4) |
| `assertions` | no | — | List of assertions to evaluate against the experiment log |

**Note on `packet_loss` and `latency_ms`:** These require traffic to flow
through the router. `nat_type: none` places the client directly on `public-net`
(bypassing the router), so use `full-cone` instead when testing network
degradation.

**Note on `server_count: ipfs-network`:** The client bootstraps to the real
IPFS/libp2p DHT and discovers actual AutoNAT v2 servers on the internet. The
client still goes through the local NAT router. This tests real-world server
behavior (rate limiting, DHT discovery time, rejection rates) vs the controlled
local environment.

### Assertions

Assertions are evaluated against the JSONL experiment log after each run by
`eval-assertions.py`. Three types are supported:

```yaml
assertions:
  # FAIL if any matching event exists in the log
  - type: no_event
    event: addresses_updated
    filter: { address_contains: "73." }
    message: "No public address should be activated"

  # FAIL if no matching event exists in the log
  - type: has_event
    event: reachability_changed
    filter: { reachability: private }
    message: "Final reachability should be private"

  # Extract and display a value (never fails)
  - type: info
    event: connected
    extract: elapsed_ms
    select: first             # or "last"
    label: "Bootstrap latency (first peer)"
```

**Filter fields:**

| Filter | Matches against |
|--------|-----------------|
| `address_contains` | Any address in the event's `addresses` or `address` field |
| `message_contains` | The full JSON-serialized event |
| `reachability` | The event's `reachability` field (exact match) |

## Output

Results are saved to `results/testbed/<scenario-name>-<timestamp>/`:

```
results/testbed/full-matrix-20260225T103000Z/
  none-tcp-3.json
  none-quic-5.json
  symmetric-quic-7.json
  ...

results/testbed/flight-wifi-20260225T110000Z/
  symmetric-both-5-lat350-run1.json
  symmetric-both-5-lat350-run1.assertions.json
  symmetric-both-5-lat350-run2.json
  symmetric-both-5-lat350-run2.assertions.json
  symmetric-both-5-lat350-run3.json
  symmetric-both-5-lat350-run3.assertions.json
```

Each `.json` file contains the experiment's JSONL event log. When assertions
are present, a corresponding `.assertions.json` file contains the pass/fail
results.

## Local (Non-Docker) Mode

`run-local.sh` runs the AutoNAT client directly on your machine (no Docker)
against the real IPFS/libp2p network. It is not affected by the YAML runner:

```bash
./testbed/run-local.sh
./testbed/run-local.sh --transport=quic --timeout=60
./testbed/run-local.sh --runs=3 --label=home-wifi
```

Results are saved to `results/local/`.

## Manual Docker Compose Usage

For more control, use docker compose directly from the project root:

```bash
DC="docker compose -f testbed/docker/compose.yml"

# Start with 3 servers, symmetric NAT
NAT_TYPE=symmetric TRANSPORT=quic $DC up --build

# Start with 5 servers
NAT_TYPE=none TRANSPORT=tcp $DC --profile 5servers up --build

# Start with 7 servers
NAT_TYPE=none $DC --profile 5servers --profile 7servers up --build

# Public server mode (no local servers, real IPFS DHT)
NAT_TYPE=symmetric $DC --profile public up --build

# Add network degradation
NAT_TYPE=full-cone PACKET_LOSS=5 LATENCY_MS=100 $DC up --build

# Watch client logs
$DC logs -f client

# Tear down
$DC down --volumes
```
