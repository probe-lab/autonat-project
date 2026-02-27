# AutoNAT v2 Measurement Project — Planning

Source: Notion project "Libp2p CF 2026" (exported 2026-02-25)

## Project Goals

Measure AutoNAT v2 behavior across different NAT types in a controlled lab:

- **False Negative Rate**: How often a reachable node incorrectly reports as "private"
- **False Positive Rate**: How often an unreachable node incorrectly reports as "public"
- **Time-to-Confidence**: How long / how many peers before stable reachability status
- **Time-to-Update**: Delay between network change and AutoNAT state update
- **Protocol Overhead**: Bandwidth cost of running AutoNAT continuously

---

## Task List

`[x]` completed · `[-]` in progress · `[ ]` not started

**Phase 0: Preparation**

- [x] P0.1 — Study AutoNAT v2 specification
- [x] P0.2 — Review go-libp2p AutoNAT implementation

**Phase 1: Core Setup**

- [x] P1.1 — Set up minimal Docker topology
- [x] P1.2 — Get AutoNAT working in test environment
- [ ] P1.3 — Add basic OpenTelemetry tracing
- [-] P1.4 — Implement first measurement: False Negative Rate
- [x] P1.5 — Create simple test runner script
- [ ] P1.6 — Design controllable AutoNAT server interface
- [ ] P1.7 — Implement controllable AutoNAT server
- [ ] P1.8 — Integrate controllable servers into test framework

**Phase 2: Symmetric NAT Testing**

- [-] P2.1 — Implement Symmetric NAT configuration
- [-] P2.2 — Run False Negative measurement for Symmetric NAT

**Phase 3: Additional Measurements**

- [-] P3.1 — Implement False Positive Rate measurement
- [-] P3.2 — Implement Time-to-Confidence measurement
- [ ] P3.3 — Implement Time-to-Update measurement
- [ ] P3.4 — Implement Protocol Overhead measurement *(low priority)*

**Phase 4: Test Framework**

- [ ] P4.1 — Define declarative test description schema
- [ ] P4.2 — Build test parser and orchestrator
- [-] P4.3 — Add latency simulation

**Phase 5: Instrumentation & Analysis**

- [-] P5.1 — Define implementation-agnostic trace format
- [-] P5.2 — Document instrumentation requirements
- [ ] P5.3 — Create trace analysis tooling

**Phase 6: Measurement & Reporting**

- [-] P6.1 — Run measurements across Port-Restricted and Symmetric NAT
- [-] P6.2 — Analyze results and identify patterns
- [-] P6.3 — Draft findings report

**Phase 7: Advanced NAT Types**

- [-] P7.1 — Implement Restricted Cone (address-restricted) NAT configuration
- [-] P7.2 — Implement Full Cone NAT configuration
- [-] P7.3 — Run measurements for Restricted Cone and Full Cone NAT

---

## Task Details

### Phase 0: Preparation

#### P0.1 — Study AutoNAT v2 specification
- **Status:** In progress
- **Notes:** Spec fully reviewed, documented in `docs/autonat-v2.md`

Thoroughly read and understand the AutoNAT v2 specification. Key areas: protocol
flow (DialRequest, DialBack, DialResponse), message formats, nonce-based
verification, amplification attack prevention (DialDataRequest/DialDataResponse),
status codes (E_DIAL_ERROR, E_DIAL_BACK_ERROR, OK), response codes
(E_REQUEST_REJECTED, E_DIAL_REFUSED, E_INTERNAL_ERROR, OK), implementation
recommendations (3+ server confirmations heuristic).

#### P0.2 — Review go-libp2p AutoNAT implementation
- **Status:** In progress
- **Notes:** Deep-dived into `p2p/protocol/autonatv2/`, found black hole detector bug (#16b) and address-restricted false positive (#1)

Study the go-libp2p AutoNAT implementation: where AutoNAT state is managed,
how state transitions occur, what events/callbacks exist, how confidence/
confirmation counting works, existing metrics/logging, client and server
components. Relevant code: `p2p/host/autonat`, `p2p/protocol/autonatv2/`,
`test-plans/`.

---

### Phase 1: Core Setup

#### P1.1 — Set up minimal Docker topology
- **Status:** Completed
- **Notes:** Docker Compose testbed with configurable NAT types (iptables), public/private networks, 3/5/7 servers

Create the Docker-based test environment: 1 router container (iptables NAT),
1 NATed node container, 4+ public AutoNAT server containers. Two networks:
internal (router + NATed node) and external (router + public servers). Start
with default iptables MASQUERADE (port-restricted cone NAT). Router needs
NET_ADMIN capability. Deliverables: Dockerfiles, docker-compose.yml, README.

#### P1.2 — Get AutoNAT working in test environment
- **Status:** Completed
- **Notes:** Working on native Linux VM (Docker Desktop macOS has DNAT issue #16a)

Verify the Docker topology works: start all containers, verify NATed node
connects through router, verify servers provide AutoNAT service, verify
protocol runs and determines reachability, check logs for dial-back attempts,
manually verify correct reachability status.

#### P1.3 — Add basic OpenTelemetry tracing
- **Status:** Not started

Instrument go-libp2p's AutoNAT with OpenTelemetry: span for each probe,
events for dial-back success/failure and reachability status changes, attributes
(peer ID, address, result, confidence). Add OTel collector container, configure
OTLP export, set up visualization (Jaeger or JSON export). Evaluate if
go-libp2p's event bus can be used without code changes; if not, create minimal
fork with instrumentation.

#### P1.4 — Implement first measurement: False Negative Rate
- **Status:** In progress
- **Notes:** Measured across all NAT types — no false negatives observed for port-restricted/symmetric NAT

A "false negative" = node IS reachable but AutoNAT incorrectly reports
"private". Configure test so NATed node IS reachable (e.g., port forwarding),
run AutoNAT, check if it correctly identifies as "public", count incorrect
"private" reports. Run N iterations, calculate rate = (incorrectly private) /
(total runs).

#### P1.5 — Create simple test runner script
- **Status:** Completed
- **Notes:** `testbed/run.sh`, `testbed/run-matrix.sh`, `testbed/run-local.sh`, `testbed/run-flight-wifi.sh`

Script that orchestrates a complete test run: start Docker Compose, wait for
healthy containers, wait for AutoNAT convergence (configurable timeout),
collect traces, parse and extract measurements, output results (JSON), tear
down. Config options: NAT type, iteration count, timeout, output path.

#### P1.6 — Design controllable AutoNAT server interface
- **Status:** Not started

Design interface for controllable AutoNAT servers with configurable behaviors:
1) Response type control (always reachable, always unreachable, actual,
probabilistic). 2) Timing control (response delay, timeout simulation, variable
delays). 3) Error injection (specific error codes, malformed responses,
connection drops). 4) Selective behavior (different responses per address or
over time).

#### P1.7 — Implement controllable AutoNAT server
- **Status:** Not started

Implement the controllable server from P1.6. Options: wrapper around go-libp2p
server (intercept dial-back, override based on config), custom implementation
(full control, more work), or proxy/middleware. Recommended: wrapper approach.
Config via YAML/environment (response_type, probability, delay_ms, error
injection settings).

#### P1.8 — Integrate controllable servers into test framework
- **Status:** Not started

Integrate controllable servers into Docker testbed: Docker image accepting
behavior config via env vars or mounted config, update docker-compose.yml to
support per-server behavior settings, extend test schema to include server
behaviors. Enables scenarios like "3 servers force reachable, 1 actual".

---

### Phase 2: Symmetric NAT Testing

#### P2.1 — Implement Symmetric NAT configuration
- **Status:** In progress
- **Notes:** `MASQUERADE --random` in router entrypoint, verified with `verify-nat.sh` (12/12 tests pass)

Add symmetric NAT via `iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
--random-fully` (different source port per destination). Create separate
iptables script, update router to accept NAT type as env var, update
docker-compose.yml. Validate via STUN-based NAT type detection.

#### P2.2 — Run False Negative measurement for Symmetric NAT
- **Status:** In progress
- **Notes:** Correctly detected as private (~13-18s). v2 never fires (Issue #17: address activation threshold never met). Confirmed in testbed + flight WiFi field data

Execute False Negative Rate measurement with symmetric NAT. Run same test
protocol as port-restricted cone, collect and compare results. Symmetric NAT
is more restrictive, so we may see different behavior. Deliverables:
measurement data, comparison notes with port-restricted.

---

### Phase 3: Additional Measurements

#### P3.1 — Implement False Positive Rate measurement
- **Status:** In progress
- **Notes:** Discovered Issue #1: address-restricted NAT false positive. Filed as [go-libp2p#3467](https://github.com/libp2p/go-libp2p/issues/3467)

A "false positive" = node is NOT reachable but AutoNAT reports "public". Set
up NATed node behind restrictive NAT (no port forwarding), run AutoNAT, count
incorrect "public" reports. For standard configs, false positives should be
rare — high rates indicate protocol issues.

#### P3.2 — Implement Time-to-Confidence measurement
- **Status:** In progress
- **Notes:** Measured convergence times across NAT types (3-6s TCP reachable, 6-11s QUIC, 13-18s private). Formal metric not yet isolated

Measure how long AutoNAT takes to reach a confident determination. Collect:
wall-clock time from startup to stable status, number of server responses
before stabilization, successful vs failed dial-backs before convergence.
Output: mean/median/p95 time-to-confidence, probes-to-confidence distribution.

#### P3.3 — Implement Time-to-Update measurement
- **Status:** Not started

Measure how quickly AutoNAT detects network changes. Scenario: start behind
NAT (stable "private"), dynamically add port forwarding, measure time until
"public", remove forwarding, measure time back to "private". Requires script
to modify iptables mid-test and correlate timestamps with trace events.

#### P3.4 — Implement Protocol Overhead measurement
- **Status:** Not started
- **Priority:** Low

Measure bandwidth cost of AutoNAT: bytes sent/received, probes per time
period, breakdown by message type (DialRequest, DialResponse, DialBack,
DialData). Methods: iptables counters, tcpdump, or OTel metrics. Test
scenarios: idle node, varying peer counts, different re-check intervals.

---

### Phase 4: Test Framework

#### P4.1 — Define declarative test description schema
- **Status:** Not started
- **Notes:** Current approach uses shell scripts with flags (`--packet-loss`, `--latency`, `--tcp-block-port`, `--port-remap`)

Design YAML schema for test scenarios: topology (nat_type, num_servers,
latency_ms, jitter_ms), scenario (node_reachable), measurements to run,
iteration count, timeout. Deliverables: schema definition (JSON Schema),
example test files, documentation.

#### P4.2 — Build test parser and orchestrator
- **Status:** Not started
- **Notes:** `run-matrix.sh` partially covers this

Tooling to execute tests from YAML descriptions: parse and validate against
schema, generate docker-compose.yml for topology, configure NAT type, execute
iterations, collect traces, compute metrics, output structured JSON. CLI:
`./run-test.sh tests/scenario.yaml --output results/`.

#### P4.3 — Add latency simulation
- **Status:** In progress
- **Notes:** `tc netem` on router interfaces. Note: only affects traffic through router (not `none` NAT mode)

Integrate `tc netem` for latency/jitter/packet-loss simulation. Apply on
router's external interface. Support in YAML test descriptions. Test scenarios:
low (10ms), medium (50ms), high (150ms), variable (high jitter).

---

### Phase 5: Instrumentation & Analysis

#### P5.1 — Define implementation-agnostic trace format
- **Status:** Done
- **Notes:** All output uses OpenTelemetry traces (JSONL, one span per line). Testbed lifecycle events are on an `autonat.session` span; AutoNAT v2 internals are separate spans (`autonatv2.refresh_cycle`, `autonatv2.server_selection`, `autonatv2.probe`). See `docs/otel-tracing.md`.

Design a trace/event format that any libp2p implementation can emit (go, rust,
js). Potentially add to libp2p specs.

#### P5.2 — Document instrumentation requirements
- **Status:** Done
- **Notes:** Full span/event reference in `docs/otel-tracing.md`

Specify what instrumentation other libp2p implementations need: events to emit
(names, when, required/optional fields), export mechanism (OTel preferred,
JSON Lines fallback), OTLP config, example implementations and pseudocode.

#### P5.3 — Create trace analysis tooling
- **Status:** Not started
- **Notes:** Results currently analyzed manually from JSON files

Build scripts/tools to analyze collected traces: compute False Negative/Positive
rates from reachability_changed events, Time-to-Confidence from first probe to
stable status, Time-to-Update from network change to status change, Protocol
Overhead from probe byte counts. Output: summary statistics (mean, median, p95),
raw data, optional visualizations.

---

### Phase 6: Measurement & Reporting

#### P6.1 — Run measurements across Port-Restricted and Symmetric NAT
- **Status:** In progress
- **Notes:** Full matrix: 5 NAT types × 3 transports × 7 servers = 15 tests, all passing. Packet loss/latency experiments need re-run (testbed limitation: `none` NAT bypasses router)

Execute the complete measurement suite across both supported NAT types. Run
N iterations per cell (suggest 10-20 for significance), collect traces, compute
metrics. Latency variations recommended (10ms, 100ms). Deliverables: complete
dataset, summary tables, raw trace archives.

#### P6.2 — Analyze results and identify patterns
- **Status:** In progress
- **Notes:** Two issues confirmed (#1 false positive, #16b QUIC black hole), two field-confirmed (#17 symmetric bypass, #8 v1 oscillation). Analysis in `docs/report.md`

Analyze measurement data: cross-NAT comparison (do rates differ by NAT type?),
latency impact (does high latency increase error rates?), edge cases and
anomalies (unexpected results, flapping, timeouts), statistical significance.
Deliverables: analysis document, charts, list of potential improvements.

#### P6.3 — Draft findings report
- **Status:** In progress
- **Notes:** `docs/report.md` — comprehensive report with protocol analysis, issue descriptions, testbed architecture, and full test results

Write final report: executive summary (key findings, recommendations),
methodology (test environment, NAT types, metrics, statistical approach),
results (tables and charts for all metrics by NAT type), discussion
(interpretation, comparison with expected behavior, identified issues),
recommendations (protocol improvements, configuration, further investigation),
appendix (data tables, environment specs, raw data links).

---

### Phase 7: Advanced NAT Types

#### P7.1 — Implement Restricted Cone (address-restricted) NAT configuration
- **Status:** In progress
- **Notes:** `xt_recent` module + DNAT rules in router entrypoint, verified with `verify-nat.sh`

Implement address-restricted cone NAT (EIM + ADF) using iptables. Challenge:
Linux conntrack is address+port-dependent by default; need custom rules to
relax port matching. Document limitations vs true restricted cone. Validate
via STUN-based NAT detection.

#### P7.2 — Implement Full Cone NAT configuration
- **Status:** In progress
- **Notes:** SNAT + DNAT rules, verified with `verify-nat.sh`

Implement full cone NAT (EIM + EIF). Challenge: standard MASQUERADE doesn't
allow unsolicited inbound. Approach: static DNAT for known ports + permissive
forwarding. Limitation: requires knowing ports in advance (not dynamic).
Document limitations. Validate via STUN.

#### P7.3 — Run measurements for Restricted Cone and Full Cone NAT
- **Status:** In progress
- **Notes:** Full-cone: correctly reachable. Address-restricted: false positive confirmed (Issue #1)

Execute measurement suite for restricted cone and full cone NAT. Document
that these are approximations. Compare with port-restricted and symmetric
results. Assess whether approximations are "good enough" for meaningful
results.

---

## Summary

| Phase | Total | Completed | In Progress | Not Started |
|-------|-------|-----------|-------------|-------------|
| Phase 0: Preparation | 2 | 0 | 2 | 0 |
| Phase 1: Core Setup | 7 | 0 | 4 | 3 |
| Phase 2: Symmetric NAT | 2 | 0 | 2 | 0 |
| Phase 3: Measurements | 4 | 0 | 2 | 2 |
| Phase 4: Test Framework | 3 | 0 | 1 | 2 |
| Phase 5: Instrumentation | 3 | 0 | 2 | 1 |
| Phase 6: Reporting | 3 | 0 | 3 | 0 |
| Phase 7: Advanced NATs | 3 | 0 | 3 | 0 |
| **Total** | **27** | **0** | **19** | **8** |

## Key Deliverables

- [-] Docker testbed with 5 NAT types — `testbed/docker/compose.yml`
- [-] NAT verification test suite — `tests/verify-nat.sh` (12/12 pass)
- [-] Baseline test matrix (15/15 pass) — `testbed/run-matrix.sh`
- [-] QUIC dial-back fix — `go-libp2p-patched/config/config.go`
- [-] Filed Issue #1 on go-libp2p — https://github.com/libp2p/go-libp2p/issues/3467
- [-] Hotel WiFi + Flight WiFi testbed reproductions
- [-] Findings report — `docs/report.md`
- [ ] OpenTelemetry tracing integration
- [ ] Packet loss / latency experiments (need re-run with `full-cone` NAT)
- [ ] Time-to-Update measurement
- [ ] Controllable AutoNAT server for rate-limit testing
- [ ] Trace analysis tooling

## Outstanding Issues

1. **Packet loss / latency experiments invalid** — `tc netem` is on the router,
   but `none` NAT bypasses it. Re-run with `full-cone` NAT type.
2. **Issue #1 real-world prevalence unknown** — need NAT type distribution data
   to quantify address-restricted (ADF) vs port-restricted (APDF) prevalence.
3. **Controllable server not implemented** — P1.6/P1.7/P1.8 blocked on
   design decisions about how to control server behavior (rate limits, selective
   failures, etc.).
