# Scenario YAML Schema

Testbed experiments are described as YAML files consumed by `testbed/run.sh`.
The formal JSON Schema is at `testbed/schema/scenario-schema.json`.

## Overview

A scenario file has a `name`, optional `description`, optional `defaults`, and
**either** a `matrix` **or** a `scenarios` list (not both).

```yaml
name: my-experiment
description: "What this measures"
defaults:
  timeout_s: 120
  runs: 1
# Use matrix OR scenarios:
matrix:
  nat_type: [none, symmetric]
  transport: [tcp, quic]
  server_count: [7]
```

```yaml
name: my-explicit-experiment
defaults:
  timeout_s: 90
scenarios:
  - name: case-1
    nat_type: symmetric
    transport: quic
    server_count: 7
```

### Matrix mode

`matrix` keys are scenario field names; values must be arrays. `run.sh` expands
all combinations into individual scenarios (Cartesian product).

### Scenarios mode

`scenarios` is a list of scenario objects. Each may override any field from
`defaults`.

---

## Field Reference

### Top-level fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Scenario set name; used in output directory |
| `description` | string | no | Human-readable description |
| `defaults.timeout_s` | integer ≥ 1 | no | Default per-run timeout (seconds); default 120 |
| `defaults.runs` | integer ≥ 1 | no | Default run count per scenario; default 1 |
| `matrix` | object of arrays | one of | Cartesian product expansion |
| `scenarios` | array | one of | Explicit scenario list |

### Scenario fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | — | Unique label; used in result file names |
| `nat_type` | enum | — | NAT topology (see below) |
| `transport` | enum | — | Client transport(s): `tcp`, `quic`, `both` |
| `server_count` | int 3–7 or `"ipfs-network"` | — | AutoNAT servers to use |
| `timeout_s` | integer ≥ 1 | defaults | Per-run timeout override |
| `runs` | integer ≥ 1 | defaults | Run count override |
| `packet_loss` | integer 0–100 | 0 | Packet loss % via `tc netem` on router |
| `latency_ms` | integer ≥ 0 | 0 | One-way added latency via `tc netem` (RTT = 2×) |
| `tcp_block_port` | integer 1–65535 | — | Drop outbound TCP to this port |
| `port_remap` | string `"INT:INT"` | — | Remap source port (e.g. `"4001:29538"`) |
| `port_forward` | bool | — | Static DNAT from router public IP to client |
| `upnp` | bool | — | Enable miniupnpd on router |
| `obs_addr_thresh` | integer ≥ 1 | auto | Override `--obs-addr-threshold` on client |
| `mock_behaviors` | array[3] of string | — | Behavior per mock server (replaces real servers) |
| `mock_delays` | array[3] of integer | — | Response delay (ms) per mock server |
| `mock_jitters` | array[3] of integer | — | Delay jitter (ms) per mock server |
| `mock_probabilities` | array[3] of float 0–1 | — | Probability for `probabilistic` behavior |
| `mock_tcp_behaviors` | array[3] | — | TCP-specific behavior override per server |
| `mock_quic_behaviors` | array[3] | — | QUIC-specific behavior override per server |
| `measurements` | array of string | — | Metrics to compute via `analyze.py` (see below) |
| `assertions` | array of objects | — | Pass/fail checks on trace events |

### `nat_type` values

| Value | Description |
|-------|-------------|
| `none` | No NAT — client has a public IP and is directly reachable |
| `full-cone` | EIM + EIF — any external host can reach the client |
| `address-restricted` | EIM + ADF — inbound allowed from previously contacted IPs |
| `port-restricted` | EIM + APDF — inbound only from exact IP:port pair contacted |
| `symmetric` | ADPM + APDF — different port per destination, no inbound |

### `mock_behaviors` values

| Value | Description |
|-------|-------------|
| `force-reachable` | Always reports address as reachable |
| `force-unreachable` | Always reports address as unreachable |
| `reject` | Rejects the dial request (protocol error) |
| `refuse` | Refuses to dial back (connection refused) |
| `internal-error` | Returns an internal error response |
| `timeout` | Hangs and never responds |
| `wrong-nonce` | Sends dial-back with incorrect nonce |
| `no-dialback-msg` | Dials back but sends no DialBack message |
| `probabilistic` | Randomly succeeds/fails at `mock_probabilities` rate |
| `actual` | Performs a real dial-back attempt |

### `measurements` values

Declare which metrics to compute from collected traces with `testbed/analyze.py`:

| Value | Description |
|-------|-------------|
| `false_negative_rate` | How often a reachable node is reported as private |
| `false_positive_rate` | How often an unreachable node is reported as public |
| `time_to_confidence` | Time from start to stable reachability verdict |
| `time_to_update` | Time to detect a mid-session network change |
| `protocol_overhead` | Probe count and estimated bandwidth cost |

### `assertions` fields

Each assertion object:

| Field | Type | Description |
|-------|------|-------------|
| `type` | `has_event` \| `no_event` \| `info` | Assertion kind |
| `event` | string | OTel event name to match |
| `filter` | object | Attribute filters (see below) |
| `message` | string | Message shown on failure |
| `extract` | string | Attribute to extract (`info` only) |
| `select` | `first` \| `last` | Which match to use (`info` only) |
| `label` | string | Display label for extracted value (`info` only) |

**Filter operators** (keys in `filter`):

| Key | Description |
|-----|-------------|
| `not_empty: <field>` | Field must be a non-empty list |
| `is_empty: <field>` | Field must be an empty list |
| `reachability: <value>` | `reachability` attribute equals value |
| `address_contains: <str>` | `addresses` contains substring |
| `message_contains: <str>` | Any field contains substring |
| `<key>: <value>` | Exact match on any attribute |

---

## Scenario Files

Each YAML file groups scenarios by **what is being tested** (the network
condition), not by what is being measured. Multiple metrics (FNR, FPR, TTC,
TTU) are extracted from the same trace data.

### Reachability

| File | Scenarios | Description |
|------|-----------|-------------|
| `reachable.yaml` | 6 | Nodes with no NAT or full-cone NAT that **should** be publicly reachable. Measures FNR + TTC. 20 runs each for statistical confidence. |
| `unreachable.yaml` | 6 | Nodes behind symmetric or port-restricted NAT that should **not** be reachable. Measures FPR + TTC. 20 runs each. |
| `reachable-forwarded.yaml` | 15 | Nodes behind restrictive NAT made reachable via static port forwarding, UPnP, or dynamic mid-session toggles. Static scenarios measure FNR + TTC (20 runs); dynamic toggle scenarios measure TTU (1 run, 600s timeout). |

### Network degradation

| File | Scenarios | Description |
|------|-----------|-------------|
| `packet-loss.yaml` | 24 | Packet loss sweep: 4 NAT types x 2 transports x 3 loss rates (1%, 5%, 10%). Tests detection reliability under lossy conditions. |
| `high-latency.yaml` | 16 | Latency sweep: 4 NAT types x 2 transports x 2 RTT levels (200ms, 500ms). Tests whether high RTT triggers AutoNAT v2 timeouts. |

### Controlled server behavior

| File | Scenarios | Description |
|------|-----------|-------------|
| `mock-server.yaml` | 11 | Mock AutoNAT servers with controlled responses: rejections, forced verdicts, wrong nonces, delays, jitter, probabilistic behavior, and per-transport splits. |

### Real-world reproductions

| File | Scenarios | Description |
|------|-----------|-------------|
| `hotel-wifi.yaml` | 1 | Port-restricted NAT with TCP blocked on port 4001 and port remapped. Reproduces field data from 2026-02-19. |
| `flight-wifi.yaml` | 1 | Symmetric NAT with 350ms one-way latency (~700ms RTT). AutoNAT v2 never fires. Reproduces field data from 2026-02-16. |

### Protocol issues

| File | Scenarios | Description |
|------|-----------|-------------|
| `v1-v2-gap.yaml` | 2 | Reproduces v1/v2 reachability gap: mix of reliable + unreliable servers causes v1 oscillation while v2 stays stable. 240s observation window. |

### Broad coverage

| File | Scenarios | Description |
|------|-----------|-------------|
| `matrix.yaml` | 40 | Full Cartesian product: 5 NAT types x 2 transports x (3 local server counts + ipfs-network). Quick smoke test for all combinations. |

---

## Latency Simulation (P2.3)

Network latency and jitter are implemented via `tc netem` on the router's
interfaces. Setting `latency_ms: 50` adds 50 ms one-way delay (100 ms RTT).
Use with `nat_type: full-cone` (or any NAT type) since `tc netem` applies at
the router.

```yaml
matrix:
  nat_type: [full-cone]
  transport: [tcp, quic]
  server_count: [7]
  latency_ms: [10, 50, 150]
```

The router entrypoint applies the rule on both interfaces for symmetric delay:
```bash
tc qdisc add dev eth0 root netem delay 50ms
tc qdisc add dev eth1 root netem delay 50ms
```

---

## Test Orchestrator (P2.2)

`testbed/run.sh` is the test orchestrator. It:

1. Parses the YAML file with `yq`
2. Expands matrix or iterates explicit scenarios
3. Validates all fields against the rules encoded in the script
4. Maps `server_count` to Docker Compose profiles
5. Exports environment variables (`NAT_TYPE`, `TRANSPORT`, `LATENCY_MS`, etc.)
6. Starts containers, waits for health checks, monitors logs for convergence
7. Copies `trace.json` to the results directory
8. Runs `eval-assertions.py` against the trace if `assertions` are defined
9. Prints a summary with pass/fail counts

```bash
# Run all scenarios in a file
./testbed/run.sh testbed/scenarios/matrix.yaml

# Override runs and filter to one NAT type
./testbed/run.sh testbed/scenarios/reachable.yaml --runs=50 --filter=nat_type=none

# Dry-run to see expanded scenario table
./testbed/run.sh testbed/scenarios/matrix.yaml --dry-run

# Custom output directory
./testbed/run.sh testbed/scenarios/unreachable.yaml --output=results/my-run/
```
