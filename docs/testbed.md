# Testbed Architecture & Experiments

System architecture, NAT implementation details, verification tests, and the
full experiment catalog. This document explains **how the testbed works and
why** it is designed this way.

For **how to run experiments** (commands, scenario files, YAML schema,
filtering, assertions), see [Running Experiments](../testbed/README.md).

---

## Requirements

### Host

The testbed requires a **native Linux host**. Docker Desktop on macOS does not
work — its bridge networking rewrites source IPs during cross-network DNAT,
breaking dial-back connections for full-cone and address-restricted NAT types
(see [Issue #16a](report.md#issue-16a-docker-desktop-macos-dnat-bug) in the
findings report). Port-restricted and symmetric NAT results are valid on macOS
but "reachable" scenarios are not.

Tested on **Ubuntu 22.04** (native, not in a VM on macOS).

### Software

| Tool | Purpose | Install (Ubuntu/Debian) |
|------|---------|------------------------|
| Docker + Docker Compose | Container runtime and orchestration | [docs.docker.com/engine/install](https://docs.docker.com/engine/install/ubuntu/) |
| [`yq`](https://github.com/mikefarah/yq) | YAML processing for scenario files | `sudo snap install yq` |
| `jq` | JSON processing for result parsing | `sudo apt install -y jq` |
| `python3` | Assertion evaluation (stdlib only, no pip packages) | `sudo apt install -y python3` |
| Go toolchain | Building the AutoNAT node binary (inside Docker) and running `run-local.sh` | [go.dev/doc/install](https://go.dev/doc/install) |

Internet access is required only for `ipfs-network` server mode (real IPFS DHT).

---

## Docker Architecture

```
┌──────── public-net (73.0.0.0/24) ────────┐
│  server1 (.10)    server2 (.11)           │
│  router  (.2, public side)                │
└───────────────────────────────────────────┘

┌──────── private-net (10.0.1.0/24) ───────┐
│  router  (.2, private side, runs NAT)     │
│  client  (.10, default gw = router)       │
└───────────────────────────────────────────┘
```

All containers use static IPs. The router is connected to both networks and
runs iptables rules configured by the `NAT_TYPE` environment variable. The
client's default gateway points to the router so all its traffic traverses
the NAT.

### Containers

| Container | Image | Role |
|-----------|-------|------|
| **router** | Alpine + iptables + iproute2 | NAT gateway. Reads `$NAT_TYPE` and configures iptables. |
| **server1**, **server2** | Alpine + socat + iproute2 | Hosts on the public network. Used to send/receive probes. |
| **client** | Alpine + socat + iproute2 | Host behind NAT. Default gateway set to the router. |

Two servers are needed to distinguish address-restricted from full-cone: the
client contacts server1, then we check whether server2 (never contacted) can
reach the client through the NAT.

### Why 73.x.x.x for the public network?

go-libp2p's `manet.IsPublicAddr()` classifies all RFC 1918 ranges, link-local,
CGNAT (100.64.0.0/10), and all documentation/test ranges (192.0.2.0/24,
198.51.100.0/24, 203.0.113.0/24) as non-public. AutoNAT v2 only probes
addresses that pass `IsPublicAddr()`. 73.0.0.0/24 is not in any exclusion
list. These IPs are only routed within Docker's bridge networks.

### Why socat?

BusyBox `nc` (Alpine's default) is unreliable for UDP in Docker exec. `socat`
handles UDP listening reliably and supports binding to specific source ports.
The `-u` (unidirectional) flag is required when piping `UDP-RECV` to a file.

### Why static IPs?

The router's entrypoint for full-cone and address-restricted NAT needs the
client's private IP (for DNAT rules). On Docker Desktop macOS, broadcast ping
doesn't reliably populate the ARP table. Static IPs plus `CLIENT_PRIVATE_IP`
solve this.

---

## NAT Types and iptables Rules

Each NAT type is implemented by a different set of iptables rules in the
router's entrypoint (`testbed/docker/router/entrypoint.sh`).

Variables used below:
- `$PUB_IFACE` — router's interface on public-net (detected dynamically)
- `$PRIV_IFACE` — router's interface on private-net (detected dynamically)
- `$ROUTER_PUBLIC_IP` — router's IP on public-net (73.0.0.2)
- `$CLIENT_PRIVATE_IP` — client's IP on private-net (10.0.1.10)

### No NAT (control)

```bash
iptables -A FORWARD -j ACCEPT
```

Pure IP forwarding, no address translation. The client is directly reachable
on the public network. Used as the baseline to verify AutoNAT correctly reports
reachable.

### Full Cone (EIM + EIF)

```bash
iptables -t nat -A POSTROUTING -o $PUB_IFACE -j SNAT --to-source $ROUTER_PUBLIC_IP
iptables -t nat -A PREROUTING  -i $PUB_IFACE -j DNAT --to-destination $CLIENT_PRIVATE_IP
iptables -A FORWARD -j ACCEPT
```

- **SNAT** rewrites the client's source IP to the router's public IP on
  outbound packets (endpoint-independent mapping — same external IP:port
  regardless of destination).
- **DNAT** forwards **all** inbound traffic on the router's public IP to the
  client (endpoint-independent filtering — any external host can reach the
  client, no prior contact required).
- `FORWARD ACCEPT` allows all forwarded traffic in both directions.

### Address-Restricted Cone (EIM + ADF)

```bash
iptables -t nat -A POSTROUTING -o $PUB_IFACE -j MASQUERADE
iptables -A FORWARD -i $PRIV_IFACE -o $PUB_IFACE \
    -m recent --set --name contacted --rdest -j ACCEPT
iptables -t nat -A PREROUTING -i $PUB_IFACE \
    -m recent --rcheck --seconds 300 --name contacted --rsource \
    -j DNAT --to-destination $CLIENT_PRIVATE_IP
iptables -A FORWARD -i $PUB_IFACE -o $PRIV_IFACE -j ACCEPT
iptables -A FORWARD -j DROP
```

- **MASQUERADE** provides endpoint-independent mapping (same external port for
  all destinations, port-preserving when possible).
- **`xt_recent` module** tracks destination IPs the client contacts (the
  `contacted` list). When an inbound packet arrives, `--rcheck --rsource`
  checks if its source IP is in the contacted list. If yes, the packet is
  DNATted to the client.
- This allows return traffic from **any port** on a previously-contacted IP
  (address-dependent filtering), which is what makes AutoNAT v2's dial-back
  succeed — the dial-back comes from the same server IP but a different port.
- Linux's native conntrack is port-restricted (full 5-tuple matching), so plain
  `MASQUERADE` + `RELATED,ESTABLISHED` cannot produce address-only filtering.
  The `xt_recent` + DNAT approach is necessary to simulate ADF.
- Entries expire after 300 seconds.

### Port-Restricted Cone (EIM + APDF)

```bash
iptables -t nat -A POSTROUTING -o $PUB_IFACE -j MASQUERADE
iptables -A FORWARD -i $PUB_IFACE -o $PRIV_IFACE \
    -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -A FORWARD -i $PRIV_IFACE -o $PUB_IFACE -j ACCEPT
iptables -A FORWARD -j DROP
```

- **MASQUERADE** provides endpoint-independent mapping (same external port,
  port-preserving).
- **conntrack `RELATED,ESTABLISHED`** only allows inbound packets that match
  an existing connection's full 5-tuple (protocol, source IP, source port,
  destination IP, destination port). This is address+port-dependent filtering.
- This is standard Linux NAT behavior — conntrack is naturally port-restricted.
  AutoNAT v2's dial-back fails because it comes from a different source port
  than the original connection, and no conntrack entry exists for that port.

### Symmetric (ADPM + APDF)

```bash
iptables -t nat -A POSTROUTING -o $PUB_IFACE -j MASQUERADE --random
iptables -A FORWARD -i $PUB_IFACE -o $PRIV_IFACE \
    -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -A FORWARD -i $PRIV_IFACE -o $PUB_IFACE -j ACCEPT
iptables -A FORWARD -j DROP
```

- **`MASQUERADE --random`** randomizes the external source port for each new
  connection, simulating address+port-dependent mapping (each destination sees
  a different external port).
- **conntrack `RELATED,ESTABLISHED`** provides address+port-dependent filtering
  (same as port-restricted).
- The combination means each peer sees a different external address, so the
  `ObservedAddrManager` never activates a public address (no single address
  reaches `ActivationThresh=4` observations), and AutoNAT v2 never runs.

### Additional Router Features

- `TCP_BLOCK_PORT`: Blocks outbound TCP on a specified port (reproduces hotel WiFi)
- `PORT_REMAP`: Forces consistent port remapping via SNAT (reproduces hotel WiFi's 4001→29538)
- `LATENCY_MS` / `PACKET_LOSS`: Applied via `tc netem` on router interfaces

---

## Verification Tests

The test suite (`testbed/verify-nat.sh`) verifies each NAT type at the network
layer using plain TCP and UDP probes (no libp2p or AutoNAT). Run
`./testbed/verify-nat.sh` before experiments to confirm the NAT simulation is
correct.

### Test Matrix

#### No NAT (3 tests)

| Test | Expected | Verifies |
|------|----------|----------|
| Server1 TCP to client | Reachable | Direct TCP forwarding works |
| Server1 UDP to client | Reachable | Direct UDP forwarding works |
| Server2 UDP to client (never contacted) | Reachable | No filtering at all |

#### Full Cone (2 tests)

| Test | Expected | Verifies |
|------|----------|----------|
| Server1 UDP to client (contacted) | Reachable | NAT mapping allows return traffic |
| Server2 UDP to client (never contacted) | Reachable | **Any** host can reach the mapped port |

#### Address-Restricted (2 tests)

| Test | Expected | Verifies |
|------|----------|----------|
| Server1 from **different port** to client | Reachable | Only IP is checked, not port |
| Server2 to client (never contacted) | Blocked | Unknown IP is rejected |

#### Port-Restricted (3 tests)

| Test | Expected | Verifies |
|------|----------|----------|
| Server1 from **same port** (7000) | Reachable | Exact IP:port match passes conntrack |
| Server1 from **different port** (7777) | Blocked | Different port is rejected |
| Server2 to client (never contacted) | Blocked | Unknown IP is rejected |

#### Symmetric (2 tests)

| Test | Expected | Verifies |
|------|----------|----------|
| Client sends to 2 servers from same port → different external ports | Different | Endpoint-dependent mapping confirmed |
| Server2 sends to server1's mapped port | Blocked | Cross-destination filtering confirmed |

### Example Output

```
============================================
  NAT Type: none
============================================
  ✓ PASS: TCP: Server1 reaches client directly
  ✓ PASS: UDP: Server1 reaches client directly
  ✓ PASS: UDP: Server2 (uncontacted) reaches client

  ...

============================================
  Results: 12 passed, 0 failed, 0 skipped
============================================
```

---

## Lab Modes

The testbed supports four modes of operation:

| Mode | Servers | Client location | Use case |
|------|---------|-----------------|----------|
| **Local servers** | Docker containers on `public-net` | Behind simulated NAT | Controlled, reproducible experiments |
| **Public servers** (`ipfs-network`) | Real IPFS DHT servers | Behind simulated NAT | Test real-world server behavior under controlled NAT |
| **Mock servers** | Controllable mock protocol responses | On `public-net` (no NAT) | Test client reactions to specific server behaviors |
| **Real-world** (`run-local.sh`) | Real IPFS DHT servers | Host machine (real NAT) | Field measurements |

For commands and configuration, see
[Running Experiments](../testbed/README.md).

### Mock Server Behaviors

Mock servers register `/libp2p/autonat/2/dial-request` via Identify, then
respond with pre-determined protobuf messages. Category A behaviors only
send a response message. Category B behaviors perform a real dial-back
connection using a separate `dialerHost` (different peer ID).

| Behavior | Category | Response | Client effect |
|----------|----------|----------|---------------|
| `reject` | A | `E_REQUEST_REJECTED` | No result, try another server |
| `refuse` | A | `E_DIAL_REFUSED` | No result, try another server |
| `force-unreachable` | A | `OK` + `E_DIAL_ERROR` | +1 failure (unreachable) |
| `internal-error` | A | `E_INTERNAL_ERROR` | No result |
| `timeout` | A | Never responds | Stream timeout |
| `force-reachable` | B | Dial back + `OK` + `OK` | +1 success (reachable) |
| `wrong-nonce` | B | Dial back with nonce-1 | Client rejects |
| `no-dialback-msg` | B | Connect but no DialBack msg | Client timeout |

---

## Experiment Catalog

Each experiment is run via `run.sh` with the appropriate scenario file and
filters. See [Running Experiments](../testbed/README.md) for commands.

### Experiment 1: Baseline True Positive (No NAT)

**Goal**: Verify that a publicly reachable node is correctly detected as public.

**Setup**: `matrix.yaml`, `nat_type=none`, `server_count=5`, `transport=both`

### Experiment 2: Baseline True Negative (Symmetric NAT)

**Goal**: Verify that a node behind symmetric NAT is correctly detected as private.

**Setup**: `matrix.yaml`, `nat_type=symmetric`, `server_count=5`, `transport=both`

### Experiments 3-4: NAT Type Matrix (TCP / QUIC)

**Goal**: Test detection accuracy across all NAT types, per transport. QUIC may
behave differently (shorter UDP NAT TTLs, different conntrack behavior).

**Setup**: `matrix.yaml`, `server_count=5`, `transport=tcp` or `transport=quic`

| NAT Type | Expected Detection | Correct? |
|----------|-------------------|----------|
| None | Public | True positive |
| Full Cone | Public | True positive |
| Address-Restricted | Public | **False positive** (Issue #1) |
| Port-Restricted | Private | True negative |
| Symmetric | Private | True negative |

### Experiment 5: Address-Restricted NAT Deep Dive

**Goal**: Confirm false positive hypothesis for address-restricted NAT with
multiple runs.

**Setup**: `matrix.yaml`, `nat_type=address-restricted`, `server_count=5`, `runs=5`

### Experiments 6-7: Server Count Impact

**Goal**: Measure how server count (3, 5, 7) affects convergence speed.

**Setup**: `matrix.yaml`, `nat_type=none` or `nat_type=symmetric` (all server counts)

### Experiment 8: Rate Limit Pressure

**Goal**: Test convergence under aggressive rate limiting.

**Status**: Not yet implemented (requires `--server-rpm` flag).

### Experiment 9: Packet Loss

**Goal**: Test detection reliability under packet loss (1%, 5%, 10%).

**Setup**: `packet-loss.yaml`

**Status**: Needs re-run — first attempt used `none` NAT type which bypasses
the router (where `tc netem` rules are applied). Must use `full-cone`.

### Experiment 10: High Latency

**Goal**: Test detection under high-latency conditions (200ms, 500ms one-way).

**Setup**: `high-latency.yaml`

**Status**: Needs re-run — same issue as Experiment 9.

### Experiments 11-12: Public Server Tests

**Goal**: Test AutoNAT behavior using real IPFS servers through each NAT type.

**Setup**: `matrix.yaml`, `server_count=ipfs-network`

### Experiment 13: Hotel WiFi Reproduction

**Goal**: Reproduce hotel WiFi conditions (port-restricted + TCP blocked on
port 4001 + port remap 4001:29538).

**Setup**: `hotel-wifi.yaml`

**Status**: Completed — field data (2026-02-19) + testbed match (2026-02-25).
QUIC activated at ~5s, unreachable at ~11s, v1 private at ~18s. Matches field
data within ±1s on all metrics.

### Experiment 14: Flight WiFi Reproduction

**Goal**: Reproduce in-flight satellite WiFi (symmetric NAT + 700ms RTT).

**Setup**: `flight-wifi.yaml`

**Status**: Completed — field data (2026-02-16) + testbed match (2026-02-25).
No public address activated, v2 never ran, v1 private at ~21s. Confirms
symmetric NAT prevents address activation.

### Experiment 15: Mock Server Behaviors

**Goal**: Test how an unmodified client reacts to controlled protocol responses.

**Setup**: `mock-server.yaml` (8 scenarios)

---

## Real-World NAT Identification

After running `./testbed/run-local.sh`, check the OTEL trace file:

1. **`addresses_updated` events with a public IP:**
   - Same port as listen port → **port-preserving EIM** (cone NAT)
   - Different port → **port-remapping EIM** (cone NAT with remapping)
   - No public address at all → **symmetric NAT** (each peer sees different port)

2. **`reachable_addrs_changed` events:**
   - Reachable → **full-cone** (or addr-restricted false positive)
   - Unreachable → **port-restricted** (dial-back blocked)
   - No event → **symmetric** (v2 never ran)

3. **`reachability_changed` (v1):**
   - v1 private + v2 unreachable → consistent, NAT is restrictive
   - v1 private + v2 never ran → symmetric NAT
   - v1 oscillates → confidence window instability (Issue #8)

See `docs/otel-tracing.md` for the full event and span reference.

---

## Coverage Status

### Testbed

| NAT Type | Real-World Match | Testbed Runs (Linux) | Status |
|----------|-----------------|---------------------|--------|
| `none` | — | TCP+QUIC reachable ~6s | Done |
| `full-cone` | — | TCP+QUIC reachable ~3s | Done |
| `address-restricted` | — | TCP+QUIC reachable ~3s (false positive) | Done — Issue #1 confirmed |
| `port-restricted` | Heathrow, Hotel | TCP+QUIC unreachable | Done — matches field data |
| `symmetric` | Flight | v2 never fires | Done — matches field data |

### Real-World

| NAT Type | Where to Find | Status |
|----------|--------------|--------|
| Port-restricted (port-preserving) | Airport WiFi | Done |
| Port-restricted (port-remapping) - UPnp | Hotel WiFi | Done |
| Symmetric | Satellite WiFi | Done |

---

## Output

Each experiment produces an OTEL trace file (one JSON span per line) in
`results/testbed/` or `results/local/`. When assertions are present, a
corresponding `.assertions.json` file contains pass/fail results.

See [OpenTelemetry Tracing](otel-tracing.md) for the span hierarchy,
attributes, and querying examples. See
[Running Experiments](../testbed/README.md#output) for directory structure
and file naming.

## Files

| File | Purpose |
|------|---------|
| `testbed/main.go` | libp2p node binary (server, client, mock-server roles) |
| `testbed/mock_server.go` | Mock AutoNAT v2 server (controllable behaviors) |
| `testbed/go.mod` | Go module definition |
| `testbed/run.py` | YAML-driven experiment runner (Python, replaces run.sh) |
| `testbed/run.sh` | Legacy bash experiment runner |
| `testbed/run-local.sh` | Local experiment runner (no Docker, real NAT) |
| `testbed/analyze.py` | Post-hoc trace analysis (FNR, FPR, TTC, TTU, overhead) |
| `testbed/eval-assertions.py` | Assertion evaluator for experiment logs |
| `testbed/verify-nat.sh` | NAT verification test runner (network-layer) |
| `testbed/scenarios/` | YAML scenario definitions |
| `testbed/docker/compose.yml` | Docker Compose topology |
| `testbed/docker/node/` | Go AutoNAT node container |
| `testbed/docker/node-rust/` | Rust AutoNAT node container (rust-libp2p) |
| `testbed/docker/node-js/` | JS AutoNAT node container (js-libp2p) |
| `testbed/docker/router/` | NAT router (iptables) |
| `results/generate_figures.py` | Generate report figures from trace data |

## Related Documentation

| Document | Content |
|----------|---------|
| [otel-tracing.md](otel-tracing.md) | OTel span hierarchy, attributes, and Jaeger queries |
| [upnp-nat-detection.md](upnp-nat-detection.md) | UPnP/NAT-PMP port mapping and detection details |
| [scenario-schema.md](scenario-schema.md) | YAML scenario format reference |
