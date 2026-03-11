# AutoNAT v2 Trace Specification

This document defines the canonical trace format for AutoNAT v2 experiments.
Any libp2p implementation (Go, Rust, JS, etc.) that follows this spec can
produce traces compatible with the probelab testbed's analysis tooling
(`testbed/eval-assertions.py`, `testbed/analyze.py`).

## Transport

Traces are exported using **OpenTelemetry** (OTel). Two export mechanisms
are supported:

- **JSONL file** — each span written as one JSON object per line to a file
  (flag: `--trace-file=<path>` on the testbed node binary)
- **OTLP/HTTP** — spans pushed to a collector (flag: `--otlp-endpoint=<url>`);
  use with Jaeger or any OTel-compatible backend

Both mechanisms can be active simultaneously. Use `AlwaysSample` — all spans
must be recorded.

## Span Hierarchy

```
autonat.session                    (testbed harness — node lifetime)
  events: started, connected, reachability_changed,
          reachable_addrs_changed, addresses_updated, shutdown, ...

autonatv2.refresh_cycle            (protocol — one per confidence refresh)
  |
  +-- autonatv2.server_selection   (protocol — one per probe attempt)
        |
        +-- autonatv2.probe        (protocol — one per server dialed)
```

The `autonat.session` span is emitted by the **testbed harness**, not by the
AutoNAT protocol implementation. The `autonatv2.*` spans are emitted by the
**AutoNAT v2 client** inside the libp2p implementation.

---

## Span: `autonat.session`

Covers the entire node lifetime in the test. Created at startup, ended at
shutdown.

### Attributes

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `role` | string | yes | `"client"` or `"server"` |
| `transport` | string | yes | `"tcp"`, `"quic"`, or `"both"` |
| `peer_id` | string | yes | Node's libp2p peer ID (base58 or base32) |
| `listen_addrs` | string[] | yes | Listen multiaddrs at startup |

### Events

Each event carries an `elapsed_ms` attribute (int64): milliseconds elapsed
since the span's start time.

| Event name | Key attributes | Description |
|------------|---------------|-------------|
| `started` | `peer_id`, `addresses`, `message` | Node started and listening |
| `connected` | `peer_id` | Successfully connected to a peer |
| `connect_failed` | `peer_id`, `message` | Failed to connect to a peer |
| `reachability_changed` | `reachability`, `addresses` | AutoNAT v1 verdict (if applicable) |
| `reachable_addrs_changed` | `addresses`, `reachable`, `unreachable`, `unknown` | **AutoNAT v2 per-address verdict** |
| `addresses_updated` | `addresses`, `removed` | Local address set changed |
| `bootstrap_start` | `message` | DHT bootstrap starting |
| `bootstrap_connected` | `peer_id` | Connected to a bootstrap peer |
| `bootstrap_done` | `message` | Bootstrap complete |
| `bootstrap_error` | `message` | Bootstrap failed |
| `peer_discovery_start` | `message` | Reading server address files |
| `peer_discovery_done` | `message` | Server discovery complete |
| `peer_discovery_timeout` | `message` | Timed out waiting for servers |
| `shutdown` | `message` | Node shutting down |

#### `reachable_addrs_changed` detail

This is the primary event used by analysis tooling. Emitted every time the
AutoNAT v2 per-address reachability map changes.

| Attribute | Type | Description |
|-----------|------|-------------|
| `addresses` | string[] | All current listen multiaddrs |
| `reachable` | string[] | Addresses confirmed reachable (confidence ≥ threshold) |
| `unreachable` | string[] | Addresses confirmed unreachable |
| `unknown` | string[] | Addresses not yet determined |
| `elapsed_ms` | int64 | Ms since session start |

---

## Span: `autonatv2.refresh_cycle`

One span per AutoNAT v2 confidence refresh cycle. Wraps the goroutine that
dispatches probes to servers and updates per-address confidence scores.

### Attributes

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `autonat.max_concurrency` | int | no | Max concurrent probe goroutines |
| `autonat.num_probes` | int | yes | Total probes dispatched this cycle |
| `autonat.backoff` | bool | no | Whether cycle ended in backoff state |

### Events

| Event name | Key attributes | Description |
|------------|---------------|-------------|
| `probe_completed` | `addr`, `reachability`, `all_addrs_refused`, `autonat.confidence` | Emitted after each probe returns |

#### `probe_completed` attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `addr` | string | Multiaddr that was probed |
| `reachability` | string | `"public"`, `"private"`, `"unknown"` |
| `all_addrs_refused` | bool | Server refused all submitted addresses |
| `autonat.confidence` | int | Current confidence score for `addr` (successes − failures in sliding window) |

---

## Span: `autonatv2.server_selection`

One span per server selection attempt within a refresh cycle. Wraps the logic
that picks which AutoNAT v2 peer to send a probe to.

### Attributes

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `autonat.num_candidates` | int | no | Known AutoNAT v2 peers in peerstore |
| `autonat.num_throttled` | int | no | Peers skipped due to throttle |
| `autonat.selected_peer` | string | no | Peer ID chosen as server |

---

## Span: `autonatv2.probe`

One span per dial request sent to a single server. Wraps the full
request–response–dial-back exchange.

### Attributes

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `autonat.server_peer_id` | string | yes | Server's peer ID |
| `autonat.reachability` | string | yes | Result: `"public"`, `"private"`, `"unknown"`, `"refused"` |
| `autonat.dialed_addr` | string | yes | Address the server attempted to dial back |
| `autonat.nonce` | int64 | no | Nonce sent in DialRequest for verification |
| `autonat.num_addrs` | int | no | Number of addresses in DialRequest |
| `autonat.addrs` | string[] | no | Addresses sent in DialRequest |

### Events (in order of occurrence)

| Event name | Key attributes | Description |
|------------|---------------|-------------|
| `dial_request_sent` | — | DialRequest written to stream |
| `dial_data_requested` | `num_bytes` | Server requested amplification data |
| `response_received` | `response_status`, `dial_status`, `addr_idx` | Server DialResponse received |
| `dial_back_received` | `addr` | Server's dial-back connection received with correct nonce |
| `dial_back_timeout` | `reason` | No dial-back received within timeout |

---

## JSONL Format

Each span is serialized as a single-line JSON object. The exact field names
follow the Go OTel SDK export format:

```json
{
  "Name": "autonat.session",
  "SpanContext": { "TraceID": "...", "SpanID": "..." },
  "StartTime": "2024-01-15T10:00:00.000000000Z",
  "EndTime": "2024-01-15T10:01:23.456789000Z",
  "Attributes": [
    { "Key": "role", "Value": { "Type": "STRING", "Value": "client" } },
    { "Key": "transport", "Value": { "Type": "STRING", "Value": "tcp" } },
    { "Key": "peer_id", "Value": { "Type": "STRING", "Value": "12D3Koo..." } },
    { "Key": "listen_addrs", "Value": { "Type": "STRINGSLICE", "Value": ["/ip4/73.0.0.10/tcp/4001"] } }
  ],
  "Events": [
    {
      "Name": "reachable_addrs_changed",
      "Time": "2024-01-15T10:00:09.123000000Z",
      "Attributes": [
        { "Key": "elapsed_ms", "Value": { "Type": "INT64", "Value": 9123 } },
        { "Key": "reachable", "Value": { "Type": "STRINGSLICE", "Value": ["/ip4/73.0.0.10/tcp/4001"] } },
        { "Key": "unreachable", "Value": { "Type": "STRINGSLICE", "Value": [] } },
        { "Key": "unknown", "Value": { "Type": "STRINGSLICE", "Value": [] } },
        { "Key": "addresses", "Value": { "Type": "STRINGSLICE", "Value": ["/ip4/73.0.0.10/tcp/4001"] } }
      ]
    }
  ]
}
```

STRINGSLICE values are JSON arrays. INT64 values may be represented as numbers
or strings depending on the OTel exporter version — parsers should handle both.

---

## Querying Traces

```bash
# Count spans by type
jq -r '.Name' trace.json | sort | uniq -c

# Extract all reachable_addrs_changed events
jq 'select(.Name == "autonat.session") | .Events[]
  | select(.Name == "reachable_addrs_changed")' trace.json

# Find time to first reachable address
jq 'select(.Name == "autonat.session") | .Events[]
  | select(.Name == "reachable_addrs_changed")
  | select(.Attributes[] | select(.Key == "reachable") | .Value.Value | length > 0)
  | first
  | .Attributes[] | select(.Key == "elapsed_ms") | .Value.Value' trace.json

# Count total probes
jq 'select(.Name == "autonatv2.probe")' trace.json | jq -s 'length'
```

---

## Compatibility Notes

- This spec is based on the `probe-lab/go-libp2p` instrumentation
  (`v0.47.0-autonat_otel` branch). Other implementations may omit optional
  attributes but must emit all required ones for analysis tools to function.
- The `autonat.session` span is a testbed concern and does not need to be
  emitted by standalone AutoNAT implementations — only by the test harness
  wrapping the node.
- Analysis tools (`testbed/eval-assertions.py`, `testbed/analyze.py`) parse
  the JSONL format described above.
