# Running Experiments

Practical guide for running experiments: commands, scenario files, YAML
schema, filtering, assertions, and result output.

For **testbed architecture** (Docker networking, iptables rules, NAT
implementation, verification tests) and the **experiment catalog** (goals,
expected results, status), see
[Testbed Architecture & Experiments](../docs/testbed.md).

## Prerequisites

See [Testbed Architecture](../docs/testbed.md#requirements) for host
requirements (Linux only) and software installation instructions.

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

| File | Scenarios | What it tests |
|------|-----------|---------------|
| `matrix.yaml` | 40 | Full NAT type x transport x server count matrix |
| `packet-loss.yaml` | 6 | Packet loss at 1%, 5%, 10% (full-cone, both transports) |
| `high-latency.yaml` | 4 | Latency at 200ms, 500ms one-way (full-cone, both transports) |
| `hotel-wifi.yaml` | 1 | TCP blocked on port 4001, port remap 4001:29538 |
| `flight-wifi.yaml` | 3 runs | Symmetric NAT + 350ms latency (700ms RTT) |
| `mock-server.yaml` | 8 | Mock server behaviors: reject, unreachable, reachable, wrong nonce, etc. |

The WiFi scenarios include assertions that automatically validate expected
behavior (e.g., "AutoNAT v2 should never fire under symmetric NAT"). The mock
server scenarios test how an unmodified client reacts to controlled protocol
responses.

See the [experiment catalog](../docs/testbed.md#experiment-catalog) for goals,
expected results, and status of each experiment.

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

Use `--filter` to run subsets:

```bash
# Single NAT type + transport + server count
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none,transport=quic,server_count=5

# All transports and server counts for one NAT type
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=symmetric

# Public IPFS server experiments only
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=ipfs-network

# All local experiments (exclude ipfs-network)
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=5
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
via Cartesian product. Exactly one of `scenarios` or `matrix` must be present
at the root level (not both).

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

### Matrix Mode

When using `matrix`, every field value **must be an array**. The runner computes
the Cartesian product of all arrays to generate scenarios. Any scenario field
listed below can appear in a matrix:

```yaml
matrix:
  nat_type: [full-cone]
  transport: [tcp, quic]
  server_count: [7]
  packet_loss: [1, 5, 10]       # 2 transports × 3 loss values = 6 scenarios
```

Fields not included in the matrix use their defaults. You cannot mix scalar
values in a matrix — use `defaults` for fixed values and only put varying
dimensions in `matrix`.

### Scenario Fields

| Field | Required | Default | Valid values |
|-------|----------|---------|--------------|
| `nat_type` | yes | — | `none`, `full-cone`, `address-restricted`, `port-restricted`, `symmetric` |
| `transport` | yes | — | `tcp`, `quic`, `both` |
| `server_count` | yes | — | `3`, `5`, `7`, or `ipfs-network` (ignored when `mock_behaviors` is set) |
| `packet_loss` | no | 0 | Integer 0–100 (percentage via `tc netem` on router) |
| `latency_ms` | no | 0 | Non-negative integer, one-way ms via `tc netem` (RTT = 2×) |
| `tcp_block_port` | no | — | Port number 1–65535 |
| `port_remap` | no | — | `"INT:INT"` format (e.g. `"4001:29538"`) |
| `timeout_s` | no | 120 | Positive integer (seconds) |
| `runs` | no | 1 | Positive integer |
| `obs_addr_thresh` | no | auto | Positive integer (see auto-computation below) |
| `mock_behaviors` | no | — | Array of exactly 3 behavior strings (see below) |
| `mock_delays` | no | [0,0,0] | Array of exactly 3 non-negative integers (ms per mock server) |
| `assertions` | no | — | List of assertions (see below) |

**`obs_addr_thresh` auto-computation:** When not explicitly set, the runner
computes the observer address activation threshold as:
- **2** if `mock_behaviors` is set (mock mode always uses 3 servers)
- **2** if `server_count` < 4
- **4** otherwise (server_count ≥ 4 or `ipfs-network`)

**Note on `packet_loss` and `latency_ms`:** These require traffic to flow
through the router. `nat_type: none` places the client directly on `public-net`
(bypassing the router), so use `full-cone` instead when testing network
degradation.

**Note on `mock_behaviors`:** When present, `server_count` is ignored. The
runner uses the `mock` Docker profile (3 mock servers + `client-mock` on the
public network). Each element sets the `--behavior` flag for the corresponding
mock server. Valid behavior strings:

| Behavior | Category | Description |
|----------|----------|-------------|
| `reject` | A (response only) | `E_REQUEST_REJECTED` — no result, try another server |
| `refuse` | A | `E_DIAL_REFUSED` — no result, try another server |
| `force-unreachable` | A | `OK` + `E_DIAL_ERROR` — +1 failure (unreachable) |
| `internal-error` | A | `E_INTERNAL_ERROR` — no result |
| `timeout` | A | Never responds — stream timeout |
| `force-reachable` | B (dial-back) | Dial back + `OK` + `OK` — +1 success (reachable) |
| `wrong-nonce` | B | Dial back with nonce-1 — client rejects |
| `no-dialback-msg` | B | Connect but no DialBack msg — client timeout |

See [mock server behaviors](../docs/testbed.md#mock-server-behaviors) for
implementation details.

**Note on `server_count: ipfs-network`:** The client bootstraps to the real
IPFS/libp2p DHT and discovers actual AutoNAT v2 servers on the internet. The
client still goes through the local NAT router. This tests real-world server
behavior (rate limiting, DHT discovery time, rejection rates) vs the controlled
local environment.

### Assertions

Assertions are evaluated against the OTEL trace file after each run by
`eval-assertions.py`. Three types are supported:

| Assertion type | Behavior |
|----------------|----------|
| `no_event` | **FAIL** if any matching event exists in the log |
| `has_event` | **FAIL** if no matching event exists in the log |
| `info` | Extract and display a value from matching events (never fails) |

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

**Assertion fields:**

| Field | Required | Used by | Description |
|-------|----------|---------|-------------|
| `type` | yes | all | `no_event`, `has_event`, or `info` |
| `event` | yes | all | Event type to match (see valid event types below) |
| `filter` | no | all | Filter criteria (see filter fields below) |
| `message` | yes | `no_event`, `has_event` | Assertion description shown on pass/fail |
| `extract` | yes | `info` | Field name to extract from the matched event |
| `select` | no | `info` | `first` (default) or `last` — which matched event to extract from |
| `label` | no | `info` | Display label (defaults to `message` if omitted) |

**Valid event types** (emitted by the testbed node):

| Event | Description |
|-------|-------------|
| `started` | Node started (server or client) |
| `shutdown` | Node shutting down |
| `reachability_changed` | AutoNAT v1 reachability result |
| `reachable_addrs_changed` | AutoNAT v2 address reachability result |
| `addresses_updated` | Observed address activated by `ObservedAddrManager` |
| `connected` | Successfully connected to a peer |
| `connect_failed` | Failed to connect to a peer |
| `bootstrap_start` | Bootstrap process started |
| `bootstrap_connected` | Connected to a bootstrap peer |
| `bootstrap_error` | Bootstrap peer connection failed |
| `bootstrap_done` | Bootstrap process completed |
| `peer_discovery_start` | Peer discovery (from DHT routing table) started |
| `peer_discovery_done` | Peer discovery completed |
| `peer_discovery_timeout` | Peer discovery timed out |

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

Each `.json` file contains the experiment's OTEL trace output (one span per
line). When assertions are present, a corresponding `.assertions.json` file
contains the pass/fail results. See
[OpenTelemetry Tracing](../docs/otel-tracing.md) for the span hierarchy,
attributes, and querying examples.

## Local (Non-Docker) Mode

`run-local.sh` runs the AutoNAT client directly on your machine (no Docker,
no simulated NAT) against the real IPFS/libp2p network:

```bash
./testbed/run-local.sh
./testbed/run-local.sh --transport=quic --timeout=60
./testbed/run-local.sh --runs=3 --label=home-wifi
```

Results are saved to `results/local/`.

This is similar to the `server_count=ipfs-network` mode (Docker `--profile
public`), which also discovers AutoNAT servers from the real IPFS DHT.
The difference is where the client runs:

| Mode | Client runs | NAT | Server discovery |
|------|-------------|-----|------------------|
| `run-local.sh` | Directly on host | Host's real network (home WiFi, office, etc.) | Real IPFS DHT |
| `ipfs-network` (Docker) | In container behind simulated NAT | Controlled via `NAT_TYPE` env var | Real IPFS DHT |

Use `run-local.sh` to test how your actual network behaves. Use
`ipfs-network` to test real IPFS servers under a specific, reproducible NAT
configuration.

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

# Public server mode (no local servers, real IPFS DHT — same servers as run-local.sh)
NAT_TYPE=symmetric $DC --profile public up --build

# Mock servers: all force-unreachable
MOCK_BEHAVIOR_1=force-unreachable MOCK_BEHAVIOR_2=force-unreachable \
MOCK_BEHAVIOR_3=force-unreachable $DC --profile mock up --build

# Mock servers: 2 reachable + 1 unreachable (majority reachable)
MOCK_BEHAVIOR_1=force-reachable MOCK_BEHAVIOR_2=force-reachable \
MOCK_BEHAVIOR_3=force-unreachable $DC --profile mock up --build

# Mock servers: with 3s response delay
MOCK_BEHAVIOR_1=force-unreachable MOCK_DELAY_1=3000 \
MOCK_BEHAVIOR_2=force-unreachable MOCK_DELAY_2=3000 \
MOCK_BEHAVIOR_3=force-unreachable MOCK_DELAY_3=3000 \
$DC --profile mock up --build

# Add network degradation
NAT_TYPE=full-cone PACKET_LOSS=5 LATENCY_MS=100 $DC up --build

# Watch client logs
$DC logs -f client

# Tear down
$DC down --volumes
```
