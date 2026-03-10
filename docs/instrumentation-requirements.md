# AutoNAT v2 Instrumentation Requirements

This document specifies what instrumentation a libp2p AutoNAT v2 implementation
needs to be compatible with the probelab testbed. Any implementation that emits
traces matching [trace-spec.md](trace-spec.md) can be measured with the same
tooling used for go-libp2p.

## Requirements Summary

| Span | Layer | Required? | Notes |
|------|-------|-----------|-------|
| `autonat.session` | Testbed harness | yes | Wrap the node lifetime |
| `autonatv2.refresh_cycle` | AutoNAT client | yes | One per confidence refresh |
| `autonatv2.server_selection` | AutoNAT client | recommended | One per probe attempt |
| `autonatv2.probe` | AutoNAT client | yes | One per server dialed |

The `autonat.session` span is emitted by your **test harness** (the binary that
runs the node under test), not by the AutoNAT implementation itself.

The `autonatv2.*` spans are emitted from inside the **AutoNAT v2 client**.

---

## OpenTelemetry Setup

### 1. Add the OTel SDK

Use the OTel SDK for your language:

- **Go**: `go.opentelemetry.io/otel` + `go.opentelemetry.io/otel/sdk`
- **Rust**: `opentelemetry` crate + `opentelemetry-otlp`
- **JS/TS**: `@opentelemetry/sdk-node` + `@opentelemetry/exporter-otlp-http`

### 2. Configure exporters

Support two export modes, ideally both simultaneously:

**JSONL file export** (for offline analysis):
```
--trace-file=<path>   Write spans as JSONL to a file
```
Use the OTel file exporter or a custom span exporter that writes one JSON
object per line.

**OTLP/HTTP export** (for live visualization):
```
--otlp-endpoint=<url>   Push spans to OTel collector (e.g. http://jaeger:4318)
```
Use the OTLP HTTP exporter pointing at the provided URL.

When neither flag is set, disable tracing entirely (no overhead).

### 3. Sampler

Use `AlwaysSample` — every span must be recorded. Do not use probabilistic
sampling, as it would corrupt rate measurements (FNR, FPR).

### 4. Reference: go-libp2p setup

```go
// From testbed/main.go (simplified)
tp := trace.NewTracerProvider(
    trace.WithBatcher(fileExporter),
    trace.WithSampler(trace.AlwaysSample()),
)
otel.SetTracerProvider(tp)
tracer := otel.Tracer("autonat-testbed")
```

---

## Testbed Span (`autonat.session`)

Your test harness must create this span at node startup and end it at shutdown.

```go
ctx, span := tracer.Start(ctx, "autonat.session")
defer span.End()

span.SetAttributes(
    attribute.String("role", "client"),
    attribute.String("transport", "tcp"),
    attribute.String("peer_id", host.ID().String()),
    attribute.StringSlice("listen_addrs", addrsToStrings(host.Addrs())),
)
```

### Required events

Emit these events on the session span as they occur:

**`started`** — immediately after the node is listening:
```go
span.AddEvent("started", trace.WithAttributes(
    attribute.String("peer_id", host.ID().String()),
    attribute.StringSlice("addresses", addrsToStrings(host.Addrs())),
    attribute.String("message", "node started"),
    attribute.Int64("elapsed_ms", elapsedMs()),
))
```

**`reachable_addrs_changed`** — subscribe to `EvtHostReachableAddrsChanged`
(go-libp2p) or the equivalent event in your implementation. Emit whenever
the per-address reachability map changes:
```go
span.AddEvent("reachable_addrs_changed", trace.WithAttributes(
    attribute.StringSlice("addresses", allAddrs),
    attribute.StringSlice("reachable", reachableAddrs),
    attribute.StringSlice("unreachable", unreachableAddrs),
    attribute.StringSlice("unknown", unknownAddrs),
    attribute.Int64("elapsed_ms", elapsedMs()),
))
```

**`shutdown`** — just before the node stops:
```go
span.AddEvent("shutdown", trace.WithAttributes(
    attribute.String("message", "shutting down"),
    attribute.Int64("elapsed_ms", elapsedMs()),
))
```

### Recommended events

`connected`, `connect_failed`, `addresses_updated` — see
[trace-spec.md](trace-spec.md) for attribute details.

---

## Protocol Spans (`autonatv2.*`)

Add these spans inside the AutoNAT v2 client implementation.

### `autonatv2.refresh_cycle`

Wrap each confidence refresh cycle (the periodic function that dispatches
probes to servers):

```go
ctx, span := tracer.Start(ctx, "autonatv2.refresh_cycle")
defer span.End()
span.SetAttributes(
    attribute.Int("autonat.num_probes", numProbes),
    // optional:
    attribute.Int("autonat.max_concurrency", maxConcurrency),
    attribute.Bool("autonat.backoff", inBackoff),
)
// After each probe returns:
span.AddEvent("probe_completed", trace.WithAttributes(
    attribute.String("addr", addr.String()),
    attribute.String("reachability", reachability), // "public"/"private"/"unknown"
    attribute.Bool("all_addrs_refused", allRefused),
    attribute.Int("autonat.confidence", confidenceScore),
))
```

### `autonatv2.server_selection`

Wrap server selection (choosing which peer to send a probe to):

```go
ctx, span := tracer.Start(ctx, "autonatv2.server_selection")
defer span.End()
// optional:
span.SetAttributes(
    attribute.Int("autonat.num_candidates", numCandidates),
    attribute.Int("autonat.num_throttled", numThrottled),
    attribute.String("autonat.selected_peer", selectedPeer.String()),
)
```

### `autonatv2.probe`

Wrap the full dial-request exchange with one server:

```go
ctx, span := tracer.Start(ctx, "autonatv2.probe")
defer span.End()
span.SetAttributes(
    attribute.String("autonat.server_peer_id", serverPeer.String()),
    attribute.String("autonat.reachability", result),    // required
    attribute.String("autonat.dialed_addr", dialedAddr), // required
    // optional:
    attribute.Int64("autonat.nonce", nonce),
    attribute.Int("autonat.num_addrs", len(addrs)),
    attribute.StringSlice("autonat.addrs", addrsToStrings(addrs)),
)

// Events in order:
span.AddEvent("dial_request_sent")
// if server requested amplification data:
span.AddEvent("dial_data_requested", trace.WithAttributes(
    attribute.Int64("num_bytes", numBytes),
))
span.AddEvent("response_received", trace.WithAttributes(
    attribute.String("response_status", status),
    attribute.String("dial_status", dialStatus),
    attribute.Int("addr_idx", addrIdx),
))
// one of:
span.AddEvent("dial_back_received", trace.WithAttributes(
    attribute.String("addr", addr.String()),
))
// or:
span.AddEvent("dial_back_timeout", trace.WithAttributes(
    attribute.String("reason", reason),
))
```

---

## Reference Implementation

The go-libp2p instrumentation lives in the `probe-lab/go-libp2p` fork:

```
https://github.com/probe-lab/go-libp2p/tree/v0.47.0-autonat_otel
```

Relevant files:
- `p2p/net/swarm/autonatv2/client.go` — `autonatv2.probe` span
- `p2p/net/swarm/autonatv2/autonat.go` — `autonatv2.server_selection` span
- `p2p/net/swarm/autonatv2/addrs_reachability_tracker.go` — `autonatv2.refresh_cycle` span
- `testbed/main.go` — `autonat.session` span and event subscriptions

The testbed node binary wraps these into the Docker-based experiment runner.

---

## Testing Your Instrumentation

1. Build and run the testbed pointing at your implementation
2. Verify traces are written:
   ```bash
   ./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none,transport=tcp,server_count=3 --dry-run
   ./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none,transport=tcp,server_count=3
   ```
3. Check the trace file contains the expected spans:
   ```bash
   jq -r '.Name' results/testbed/<run>/none-tcp-3.json | sort | uniq -c
   # Expect: autonat.session, autonatv2.probe, autonatv2.refresh_cycle, autonatv2.server_selection
   ```
4. Run analysis:
   ```bash
   python3 testbed/analyze.py results/testbed/<run>/*.json
   ```
5. Check that assertions pass:
   ```bash
   cat results/testbed/<run>/none-tcp-3.assertions.json | jq '.[] | select(.pass == false)'
   # Should be empty for a working reachable-node scenario
   ```
