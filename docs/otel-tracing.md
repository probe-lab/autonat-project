# OpenTelemetry Tracing

## Overview

All experiment output uses OpenTelemetry traces as the single structured data
format. There is no separate JSONL log — everything lives in one trace file.

Two categories of data are captured:

1. **Testbed lifecycle** — node startup, peer connections, reachability changes,
   address updates, bootstrap progress, shutdown. These are events on a
   long-lived `autonat.session` span created by the testbed binary.

2. **AutoNAT v2 internals** — per-probe details that go-libp2p's event bus
   never exposes: which server was selected, what addresses were sent, whether
   dial-back succeeded, throttle state, confidence progression. These are
   separate spans (`autonatv2.refresh_cycle`, `autonatv2.server_selection`,
   `autonatv2.probe`) emitted from instrumented go-libp2p code.

## Enabling Tracing

Pass `--trace-file=<path>` to the autonat-node binary:

```bash
# Local
./autonat-node --role=client --trace-file=/tmp/trace.json --bootstrap

# Docker (already configured in compose.yml)
# Traces are written to /results/trace.json
```

When `--trace-file` is empty (the default), no tracing overhead is incurred.

## Output Format

Each span is written as a single-line JSON object (JSONL). This makes it easy
to parse with `jq`, `grep`, or the assertions system.

## Span Hierarchy

```
autonat.session                    (testbed: entire node lifetime)
  events: started, connected, reachability_changed,
          reachable_addrs_changed, addresses_updated, shutdown, ...

autonatv2.refresh_cycle            (go-libp2p: one per refresh interval)
  |
  +-- autonatv2.server_selection   (go-libp2p: one per probe attempt)
  |     |
  |     +-- autonatv2.probe        (go-libp2p: one per server dial)
  |
  +-- autonatv2.server_selection
        |
        +-- autonatv2.probe
```

## Span Reference

### `autonat.session` (testbed)

Created in `main.go`. Covers the entire node lifetime.

Span attributes:
| Attribute | Type | Description |
|-----------|------|-------------|
| `role` | string | Node role (client, server) |
| `transport` | string | Transport config (tcp, quic, both) |
| `peer_id` | string | Node's peer ID |
| `listen_addrs` | string[] | Listen multiaddrs |

Events (each includes `elapsed_ms` attribute):

| Event | Key attributes | Description |
|-------|---------------|-------------|
| `started` | `peer_id`, `addresses`, `message` | Node started |
| `connected` | `peer_id` | Connected to a peer |
| `connect_failed` | `peer_id`, `message` | Connection failed |
| `reachability_changed` | `reachability`, `addresses` | v1 reachability verdict |
| `reachable_addrs_changed` | `addresses`, `unreachable`, `unknown` | v2 per-address reachability |
| `addresses_updated` | `addresses`, `removed` | Local addresses changed |
| `bootstrap_start` | `message` | DHT bootstrap starting |
| `bootstrap_connected` | `peer_id` | Connected to bootstrap peer |
| `bootstrap_done` | `message` | Bootstrap complete |
| `bootstrap_error` | `message` | Bootstrap failed |
| `peer_discovery_start` | `message` | Reading server addr files |
| `peer_discovery_done` | `message` | Discovery complete |
| `peer_discovery_timeout` | `message` | Timeout waiting for servers |
| `shutdown` | `message` | Shutting down |

### `autonatv2.refresh_cycle` (go-libp2p)

Created in `addrs_reachability_tracker.go:refreshReachability()`.

| Attribute | Type | Description |
|-----------|------|-------------|
| `autonat.max_concurrency` | int | Concurrent worker goroutines |
| `autonat.num_probes` | int | Total probes dispatched |
| `autonat.backoff` | bool | Whether cycle ended with backoff |

Events:
- `probe_completed` — per successful probe: `addr`, `reachability`, `all_addrs_refused`

### `autonatv2.server_selection` (go-libp2p)

Created in `autonat.go:GetReachability()`.

| Attribute | Type | Description |
|-----------|------|-------------|
| `autonat.num_candidates` | int | Known AutoNAT v2 peers |
| `autonat.num_throttled` | int | Peers skipped (throttle) |
| `autonat.selected_peer` | string | Chosen server peer ID |

### `autonatv2.probe` (go-libp2p)

Created in `client.go:getReachability()`.

| Attribute | Type | Description |
|-----------|------|-------------|
| `autonat.server_peer_id` | string | Server peer ID |
| `autonat.nonce` | int64 | Dial-back verification nonce |
| `autonat.num_addrs` | int | Addresses in request |
| `autonat.addrs` | string[] | Multiaddrs sent to server |
| `autonat.reachability` | string | Result: `public`, `private`, `unknown`, `refused` |
| `autonat.dialed_addr` | string | Address the server dialed |

Events (in order):
1. `dial_request_sent` — dial request written to stream
2. `dial_data_requested` — server requested amplification data (`num_bytes`)
3. `response_received` — server response (`response_status`, `dial_status`, `addr_idx`)
4. `dial_back_received` — dial-back received (`addr`)
5. `dial_back_timeout` — no dial-back within timeout (`reason`)

## Querying Traces

```bash
# List all span types
jq -r '.Name' trace.json | sort | uniq -c

# Extract session events
jq 'select(.Name == "autonat.session") | .Events[] | .Name' trace.json

# Find reachability changes
jq 'select(.Name == "autonat.session") | .Events[] |
  select(.Name == "reachability_changed") |
  { reachability: (.Attributes[] | select(.Key == "reachability") | .Value.Value),
    elapsed_ms: (.Attributes[] | select(.Key == "elapsed_ms") | .Value.Value) }
' trace.json

# Find probe results
jq 'select(.Name == "autonatv2.probe") | {
  server: (.Attributes[] | select(.Key == "autonat.server_peer_id") | .Value.Value),
  result: (.Attributes[] | select(.Key == "autonat.reachability") | .Value.Value)
}' trace.json
```

## Assertions

The `eval-assertions.py` script reads OTEL traces and evaluates assertions
against session events. It extracts events from all spans and flattens their
attributes into dicts, then matches on event name and attribute filters.

Assertion types: `has_event`, `no_event`, `info` (see scenario YAML files).

## Patched go-libp2p

The OTEL spans inside go-libp2p (`autonatv2.probe`, `autonatv2.server_selection`,
`autonatv2.refresh_cycle`) live on a fork branch:

```
github.com/probe-lab/go-libp2p @ v0.47.0-autonat_otel
```

The testbed's `go.mod` uses a `replace` directive to pull the patched version:

```
replace github.com/libp2p/go-libp2p v0.47.0 => github.com/probe-lab/go-libp2p v0.47.0-autonat_otel
```

Docker builds fetch it automatically via `go mod download` — no local copy or
rsync step is needed.

## Files Modified

| File | What changed |
|------|-------------|
| [`probe-lab/go-libp2p`](https://github.com/probe-lab/go-libp2p/tree/v0.47.0-autonat_otel) `client.go` | `autonatv2.probe` span |
| [`probe-lab/go-libp2p`](https://github.com/probe-lab/go-libp2p/tree/v0.47.0-autonat_otel) `autonat.go` | `autonatv2.server_selection` span |
| [`probe-lab/go-libp2p`](https://github.com/probe-lab/go-libp2p/tree/v0.47.0-autonat_otel) `addrs_reachability_tracker.go` | `autonatv2.refresh_cycle` span |
| `testbed/main.go` | `autonat.session` span, `--trace-file` flag |
| `testbed/go.mod` | `replace` directive, OTEL SDK deps |
| `testbed/eval-assertions.py` | Reads OTEL traces instead of JSONL |
| `testbed/docker/compose.yml` | `--trace-file` on all clients |
| `testbed/run.sh` | trace.json as primary output |
| `testbed/run-local.sh` | Uses trace file for convergence detection |
