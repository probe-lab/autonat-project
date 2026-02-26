# Testbed & Experiments

This document describes the Docker-based testbed architecture, iptables rules
for each NAT type, the verification test suite, and the full experiment catalog.

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

Pure IP forwarding. No address translation.

### Full Cone (EIM + EIF)

```bash
iptables -t nat -A POSTROUTING -o $PUB_IFACE -j SNAT --to-source $ROUTER_PUBLIC_IP
iptables -t nat -A PREROUTING  -i $PUB_IFACE -j DNAT --to-destination $CLIENT_PRIVATE_IP
iptables -A FORWARD -j ACCEPT
```

Static SNAT rewrites the client's source IP to the router's public IP. Static
DNAT forwards all inbound traffic to the client. Any external host can reach
the client.

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

Linux's native conntrack is port-restricted (full 5-tuple), so plain
`MASQUERADE` + `RELATED,ESTABLISHED` can't produce address-only filtering.
The `xt_recent` module tracks contacted destination IPs. Inbound packets from
those IPs are DNATted to the client, allowing return traffic from any port on
a previously-contacted IP.

### Port-Restricted Cone (EIM + APDF)

```bash
iptables -t nat -A POSTROUTING -o $PUB_IFACE -j MASQUERADE
iptables -A FORWARD -i $PUB_IFACE -o $PRIV_IFACE \
    -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -A FORWARD -i $PRIV_IFACE -o $PUB_IFACE -j ACCEPT
iptables -A FORWARD -j DROP
```

Standard Linux NAT. Conntrack's `ESTABLISHED` state requires the exact source
IP:port pair to match, making this naturally port-restricted.

### Symmetric (ADPM + APDF)

```bash
iptables -t nat -A POSTROUTING -o $PUB_IFACE -j MASQUERADE --random
iptables -A FORWARD -i $PUB_IFACE -o $PRIV_IFACE \
    -m state --state RELATED,ESTABLISHED -j ACCEPT
iptables -A FORWARD -i $PRIV_IFACE -o $PUB_IFACE -j ACCEPT
iptables -A FORWARD -j DROP
```

The `--random` flag forces a random source port per connection, simulating
endpoint-dependent mapping.

### Additional Router Features

- `TCP_BLOCK_PORT`: Blocks outbound TCP on a specified port (reproduces hotel WiFi)
- `PORT_REMAP`: Forces consistent port remapping via SNAT (reproduces hotel WiFi's 4001→29538)
- `LATENCY_MS` / `PACKET_LOSS`: Applied via `tc netem` on router interfaces

---

## Verification Tests

The test suite (`testbed/verify-nat.sh`) verifies each NAT type at the network
layer using plain TCP and UDP probes. No libp2p or AutoNAT is involved.

### Running

```bash
# Run all NAT types (~3 minutes)
./testbed/verify-nat.sh

# Run a single NAT type
./testbed/verify-nat.sh symmetric
./testbed/verify-nat.sh address-restricted
```

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

### Mode A: Local Servers

All AutoNAT servers are Docker containers on the local `public-net` network.
Fully controlled and reproducible.

```bash
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=5
```

### Mode B: Public Servers

No local servers. The client bootstraps to the IPFS DHT and discovers real
AutoNAT v2 servers on the internet. The client still goes through the local
NAT router.

```bash
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=ipfs-network
```

### Mode C: Real-World Networks

Run the AutoNAT client locally (no Docker) against the real IPFS network,
using whatever NAT the current network provides.

```bash
./testbed/run-local.sh --runs=3 --label=<location>
```

---

## Experiments

### Experiment 1: Baseline True Positive (No NAT)

**Goal**: Verify that a publicly reachable node is correctly detected as public.

**Setup**: NAT: none, servers: 5, transport: both

**Run**: `./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none,server_count=5`

### Experiment 2: Baseline True Negative (Symmetric NAT)

**Goal**: Verify that a node behind symmetric NAT is correctly detected as private.

**Setup**: NAT: symmetric, servers: 5, transport: both

**Run**: `./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=symmetric,server_count=5`

### Experiment 3: NAT Type Matrix (TCP)

**Goal**: Test detection accuracy across all NAT types using TCP.

| NAT Type | Expected Detection | Correct? |
|----------|-------------------|----------|
| None | Public | True positive |
| Full Cone | Public | True positive |
| Address-Restricted | Public | **False positive** (Issue #1) |
| Port-Restricted | Private | True negative |
| Symmetric | Private | True negative |

**Run**: `./testbed/run.sh testbed/scenarios/matrix.yaml --filter=transport=tcp,server_count=5`

### Experiment 4: NAT Type Matrix (QUIC)

**Goal**: Same as experiment 3 but with QUIC. QUIC may behave differently
(shorter UDP NAT TTLs, different conntrack behavior).

**Run**: `./testbed/run.sh testbed/scenarios/matrix.yaml --filter=transport=quic,server_count=5`

### Experiment 5: Address-Restricted NAT Deep Dive

**Goal**: Confirm false positive hypothesis for address-restricted NAT.

**Run**: `./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=address-restricted,server_count=5 --runs=5`

### Experiments 6-7: Server Count Impact

**Goal**: Measure how server count (3, 5, 7) affects convergence speed.

**Run**:
```bash
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=symmetric
```

### Experiment 8: Rate Limit Pressure

**Goal**: Test convergence under aggressive rate limiting.

**Status**: Requires `--server-rpm` flag (not yet implemented).

### Experiment 9: Packet Loss

**Goal**: Test detection reliability under packet loss.

**Status**: **Needs re-run** — first attempt used `none` NAT type which bypasses
the router (where `tc netem` rules are applied). Must use `full-cone`.

**Run**: `./testbed/run.sh testbed/scenarios/packet-loss.yaml`

### Experiment 10: High Latency

**Goal**: Test detection under high-latency conditions.

**Status**: **Needs re-run** — same issue as Experiment 9.

**Run**: `./testbed/run.sh testbed/scenarios/high-latency.yaml`

### Experiments 11-12: Public Server Tests

**Goal**: Test AutoNAT behavior using real IPFS servers through each NAT type.

**Run**: `./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=ipfs-network`

### Experiment 13: Hotel WiFi Reproduction

**Goal**: Reproduce hotel WiFi conditions (port-restricted + TCP blocked + port remap).

**Status**: **COMPLETED** — field data (2026-02-19) + testbed match (2026-02-25)

**Testbed result**: QUIC activated at ~5s, unreachable at ~11s, v1 private at ~18s.
Matches field data within ±1s on all metrics.

**Run**: `./testbed/run.sh testbed/scenarios/hotel-wifi.yaml`

### Experiment 14: Flight WiFi Reproduction

**Goal**: Reproduce in-flight satellite WiFi (symmetric NAT + 700ms RTT).

**Status**: **COMPLETED** — field data (2026-02-16) + testbed match (2026-02-25)

**Testbed result**: No public address activated, v2 never ran, v1 private at ~21s.
Confirms symmetric NAT prevents address activation.

**Run**: `./testbed/run.sh testbed/scenarios/flight-wifi.yaml`

---

## Running the Full Matrix

```bash
# All 40 scenarios (30 local + 10 ipfs-network)
./testbed/run.sh testbed/scenarios/matrix.yaml

# Preview without executing
./testbed/run.sh testbed/scenarios/matrix.yaml --dry-run

# Run a subset
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=5
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=symmetric,transport=quic
```

This runs all local-server combinations:
- 5 NAT types × 2 transports × 3 server counts = 30 experiments

Plus ipfs-network experiments:
- 5 NAT types × 2 transports = 10 experiments

Total: 40 experiments, each up to 120 seconds + setup/teardown.

---

## Real-World NAT Identification

After running `./testbed/run-local.sh`, check the JSONL log:

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

| NAT Type | Where to Find | Runs | Status |
|----------|--------------|------|--------|
| None (public IP) | VPS (DigitalOcean, AWS) | 0 | Not tested |
| Full-cone | Home router with DMZ | 0 | Not tested |
| Address-restricted | Default home router | 0 | Not tested |
| Port-restricted (port-preserving) | Airport WiFi | 4 | Done |
| Port-restricted (port-remapping) | Hotel WiFi | 3 | Done |
| Symmetric | Satellite WiFi | 3 | Done |

### Gaps

| Gap | Impact | Blocks |
|-----|--------|--------|
| No real-world full-cone data | Can't validate full-cone behavior | Full-cone confidence |
| No real-world address-restricted data | Can't test Issue #1 in the wild | ADF prevalence measurement |
| No real-world none/public data | Can't compare testbed baseline with field | Baseline confidence |
| Server rate limiting not implemented | Can't run Experiment 8 | Rate limit testing |
| Packet loss/latency experiments invalid | Used wrong NAT type (none vs full-cone) | Experiments 9, 10 |

### Pending Real-World Tests

- [ ] Home network (no DMZ): likely address-restricted or port-restricted
- [ ] Home network (DMZ enabled): full-cone equivalent
- [ ] VPS / cloud server: true public IP
- [ ] Mobile hotspot (4G/5G): likely symmetric (CGNAT)

---

## Output Format

Each experiment produces a JSON file in `results/testbed/` or `results/local/`:

```json
{
  "experiment": {
    "nat_type": "symmetric",
    "transport": "quic",
    "server_count": 5,
    "server_source": "local",
    "timestamp": "2024-01-15T10:30:00Z"
  },
  "events": [
    {
      "time": "2024-01-15T10:30:05Z",
      "elapsed_ms": 5000,
      "type": "reachability_changed",
      "reachability": "private"
    }
  ],
  "result": {
    "final_reachability": "private",
    "convergence_time_ms": 25000,
    "total_probes": 4,
    "successful_probes": 0,
    "failed_probes": 4,
    "rejected_probes": 0
  }
}
```

## Files

| File | Purpose |
|------|---------|
| `testbed/main.go` | libp2p node binary (server + client roles) |
| `testbed/go.mod` | Go module definition |
| `testbed/run.sh` | YAML-driven experiment runner |
| `testbed/run-local.sh` | Local experiment runner (no Docker, real NAT) |
| `testbed/verify-nat.sh` | NAT verification test runner (network-layer) |
| `testbed/eval-assertions.py` | Assertion evaluator for experiment logs |
| `testbed/scenarios/` | YAML scenario definitions |
| `testbed/docker/compose.yml` | Docker Compose topology |
| `testbed/docker/node/` | AutoNAT node container |
| `testbed/docker/router/` | NAT router (iptables) |
