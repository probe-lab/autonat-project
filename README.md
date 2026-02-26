# AutoNAT v2 Measurement Lab

The first systematic performance evaluation of
[libp2p](https://libp2p.io/)'s
[AutoNAT v2](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
protocol. Despite being critical infrastructure used by every libp2p peer to
determine whether its addresses are publicly reachable, AutoNAT has never been
rigorously tested for accuracy and performance across different NAT
configurations.

## Background

### NAT and Peer-to-Peer Networking

[Network Address Translation (NAT)](https://datatracker.ietf.org/doc/html/rfc3022)
allows multiple devices on a private network to share a single public IP
address. When a device sends a packet to the internet, the NAT router replaces
the private source address with its own public IP and an assigned external port,
records the mapping, and forwards the packet. Responses are translated back
using the same mapping.

This works well for client-server communication where the private device
initiates every connection. But peer-to-peer protocols like
[libp2p](https://docs.libp2p.io/) need nodes to accept **inbound**
connections from peers they have never contacted. NAT breaks this because
unsolicited inbound packets have no matching mapping and are dropped.

The impact on libp2p is significant: a node behind NAT cannot serve content,
participate as a DHT server, or accept direct connections from other peers.
Instead, it must rely on [relay servers](https://github.com/libp2p/specs/blob/master/relay/circuit-v2.md)
(adding latency and consuming shared infrastructure) or
[hole punching](https://github.com/libp2p/specs/blob/master/relay/DCUtR.md)
(which only works with certain NAT types). The first step to choosing the
right strategy is knowing whether a node's addresses are reachable at all.

### NAT Types

Not all NATs behave the same way. [RFC 4787](https://datatracker.ietf.org/doc/html/rfc4787)
classifies NAT behavior along two independent axes — **mapping** (how external
ports are assigned) and **filtering** (which inbound packets are allowed) —
producing four classic NAT types that determine whether peer-to-peer
connections are possible.

#### Mapping Behavior

How the NAT assigns external ports when the device contacts different
destinations.

| Type | Behavior |
|------|----------|
| **Endpoint-Independent Mapping (EIM)** | Same external port regardless of destination. Both Server A and Server B see the same external address. Predictable. |
| **Address-Dependent Mapping (ADM)** | External port may differ per destination IP. Same destination IP always gets the same mapping. |
| **Address+Port-Dependent Mapping (ADPM)** | External port may differ per destination IP:port pair. Most restrictive — each connection gets a unique mapping. |

#### Filtering Behavior

Which inbound packets are allowed through an existing mapping.

| Type | Behavior |
|------|----------|
| **Endpoint-Independent Filtering (EIF)** | Any external host can send packets to the mapped port. Most permissive. |
| **Address-Dependent Filtering (ADF)** | Only IPs the device has previously sent to. Any port from that IP is allowed. |
| **Address+Port-Dependent Filtering (APDF)** | Only the exact IP:port pair the device sent to. Most restrictive. |

#### The Four Classic Types

| NAT Type | Mapping | Filtering | Where Found |
|----------|---------|-----------|-------------|
| **Full Cone** | EIM | EIF | DMZ, port forwarding, UPnP (rare) |
| **Address-Restricted Cone** | EIM | ADF | Home routers (Linux iptables default) |
| **Port-Restricted Cone** | EIM | APDF | Stricter routers, airports, hotels |
| **Symmetric** | ADPM | APDF | Mobile carriers (CGNAT), satellite, enterprise |

For iptables rules implementing each type in the testbed, see
[Testbed & Experiments](docs/testbed.md#nat-types-and-iptables-rules).

### AutoNAT: How libp2p Detects Reachability

[AutoNAT](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md) is
libp2p's protocol for determining whether a node's addresses are publicly
reachable. A node cannot know on its own whether it sits behind a NAT — it
needs an external peer to try connecting to it.

In AutoNAT v2, the client sends a
[DialRequest](docs/autonat-v2.md#protocol-flow) containing its addresses and a
random nonce. The server selects an address and attempts to **dial back** to
the client using a separate connection (different peer ID, different source
port). If the connection succeeds and the nonce matches, the address is
confirmed reachable. The client repeats this with multiple servers to build
[confidence](docs/autonat-v2.md#confidence-system) (default: 3 confirmations
needed).

Key design choices in v2 compared to [v1](https://github.com/libp2p/specs/blob/master/autonat/autonat-v1.md):
- Tests **individual addresses** rather than the node as a whole
- Requires **nonce-based verification** to prevent spoofing
- Uses a **separate peer ID** for dial-back to prevent connection reuse

For the full protocol specification, message format, and go-libp2p
implementation details, see [AutoNAT v2 Protocol](docs/autonat-v2.md).

### Detection Issues in Production

AutoNAT v2 results are unreliable in production. Nodes report incorrect
reachability — false positives, false negatives, and oscillating detection.
Incorrect reachability has real consequences:

- **False positives** cause nodes to advertise unreachable addresses. Peers
  that attempt direct connections fail, adding latency before relay fallback.
- **False negatives** cause nodes to unnecessarily use relays, wasting relay
  capacity and increasing latency.
- **Oscillation** causes nodes to repeatedly switch between direct and relayed
  connectivity, disrupting existing connections.

The root cause of these issues depends on the NAT type. The server's dial-back
comes from the **same IP** but a **different port** than the original
connection. Whether this dial-back gets through the NAT depends on the
filtering behavior:

| NAT Type | Dial-back (same IP, different port) | AutoNAT v2 Result | Correct? |
|----------|-------------------------------------|-------------------|----------|
| Full Cone (EIF) | Allowed — any source passes | Reachable | Yes |
| Addr-Restricted (ADF) | Allowed — server IP is trusted | Reachable | **No** (false positive) |
| Port-Restricted (APDF) | Blocked — different port rejected | Unreachable | Yes |
| Symmetric (APDF) | N/A — v2 never runs | v1 only | [Blind spot](docs/report.md#additional-confirmed-issues) |

The address-restricted cone false positive is particularly concerning because
this is the **most common NAT type** on home routers — the default behavior of
Linux iptables MASQUERADE. See
[Issue #1](docs/report.md#issue-1-address-restricted-nat-false-positive) for
root cause analysis.

It was unclear whether these issues stemmed from the protocol design, the
go-libp2p implementation, or the public AutoNAT server infrastructure. This
project was created to find out. For all confirmed issues and root cause
analysis, see the [Findings Report](docs/report.md).

## Goals

Measure AutoNAT v2 accuracy and performance across different NAT types in a
controlled environment:

- **False Positive Rate**: How often an unreachable node is classified as
  public (leads to failed inbound connections)
- **False Negative Rate**: How often a reachable node is classified as
  private (leads to unnecessary relay usage)
- **Time-to-Confidence**: How long until stable reachability status
- **Time-to-Update**: Delay between a network change and updated AutoNAT state
- **Protocol Overhead**: Bandwidth cost of running AutoNAT continuously

### Scope

- **Implementation under test**: [go-libp2p](https://github.com/libp2p/go-libp2p)
  (initially), with architecture designed for future
  [rust-libp2p](https://github.com/libp2p/rust-libp2p)/[js-libp2p](https://github.com/libp2p/js-libp2p)
  testing
- **Protocol version**: AutoNAT v2 only (not
  [v1](https://github.com/libp2p/specs/blob/master/autonat/autonat-v1.md))
- **IP version**: IPv4 (prioritized), IPv6 (future)

## Approach

Docker containers for node isolation and
[iptables](https://netfilter.org/projects/iptables/) rules for NAT simulation
at the kernel level. This approach is implementation-agnostic (libp2p nodes run
unmodified, NAT behavior is controlled at the network layer), reproducible
(the entire topology is defined in Docker Compose), and validated against
three real-world field experiments (airport, in-flight WiFi, hotel WiFi).
See [Testbed & Experiments](docs/testbed.md) for the full architecture and
experiment catalog.

## How It Works

The lab creates a Docker network topology with:
- A **NAT router** container (Alpine + iptables) that simulates different NAT types
- **AutoNAT server** containers (go-libp2p nodes)
- A **client** container behind the NAT router

The client probes the servers to determine its reachability. We compare the
detected result against the ground truth (we know the NAT type because we
configured it).

```
┌─────────────── public-net ──────────────────┐
│  server-1    server-2    ...    server-N     │
│  router (public side)                        │
└─────────────────────────────────────────────┘

┌─────────────── private-net ─────────────────┐
│  router (private side, runs NAT)             │
│  client (test subject)                       │
└─────────────────────────────────────────────┘
```

## Quick Start

```bash
# Build the node binary and router image
docker compose -f testbed/docker/compose.yml build

# Run the full experiment matrix (40 scenarios)
./testbed/run.sh testbed/scenarios/matrix.yaml

# Preview what will run
./testbed/run.sh testbed/scenarios/matrix.yaml --dry-run

# Run a single scenario subset
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none,transport=quic,server_count=5

# Run the flight WiFi reproduction
./testbed/run.sh testbed/scenarios/flight-wifi.yaml

# Results are saved to results/testbed/, results/local/, etc.
ls results/
```

## Local (Non-Docker) Mode

Run the AutoNAT client directly on your machine behind your real NAT,
connecting to public IPFS/libp2p servers. Useful for gathering baseline
results and comparing with the Docker testbed:

```bash
# Single run with default settings (both transports, 120s timeout)
./testbed/run-local.sh

# QUIC only, shorter timeout
./testbed/run-local.sh --transport=quic --timeout=60

# Multiple runs to check consistency
./testbed/run-local.sh --runs=3 --label=home-wifi
```

Results are saved to `results/local/` with the same structure as
testbed results.

## Public Server Mode

Instead of local servers, the client can bootstrap to the real IPFS/libp2p
network and use actual public AutoNAT servers:

```bash
./testbed/run.sh testbed/scenarios/matrix.yaml --filter=server_count=ipfs-network
```

This tests the same NAT scenarios against real infrastructure. Requires
internet access.

## Verifying the NAT Testbed

Before running AutoNAT experiments, verify that the NAT simulation itself is
correct. The test suite uses plain TCP/UDP (no libp2p) to confirm each NAT
type's filtering and mapping behavior:

```bash
# Run all 12 tests across all 5 NAT types (~3 minutes)
./testbed/verify-nat.sh

# Run a single NAT type
./testbed/verify-nat.sh address-restricted
```

See [docs/testbed.md](docs/testbed.md) for the full test matrix and
iptables implementation details.

## Documentation

- [AutoNAT v2 Protocol](docs/autonat-v2.md) — protocol spec and go-libp2p implementation
- [Findings Report](docs/report.md) — confirmed issues, root cause analysis
- [Testbed & Experiments](docs/testbed.md) — Docker architecture, iptables rules, experiment catalog
- [Project Planning](docs/planning.md) — task tracking and phases
- [Obol Impact](docs/obol.md) — impact analysis for Obol Network
- [Avail Impact](docs/avail.md) — impact analysis for Avail Network

## Network Layout

```
public-net  (73.0.0.0/24)   servers + router public side
private-net (10.0.1.0/24)   client + router private side
```

The public network uses a non-reserved range (73.0.0.0/24) because go-libp2p's
`manet.IsPublicAddr()` filters all private, reserved, and CGNAT ranges. The
router's public IP (73.0.0.2) is what servers report as the client's observed
address via Identify, and AutoNAT v2 only probes addresses that pass
`IsPublicAddr()`. The private network uses a standard RFC 1918 range (10.0.1.0/24)
— the client's private address is correctly filtered, just like in production.

## Requirements

- **Native Linux host** (Docker Desktop on macOS does not work — see
  [docs/testbed.md](docs/testbed.md#requirements) for details)
- Tested on Ubuntu 22.04
- Docker, Docker Compose, `yq`, `jq`, `python3`, Go toolchain

## Project Structure

```
autonat/
├── docs/                    # Protocol and experiment documentation
├── testbed/                 # Docker testbed, Go source, and runner scripts
│   ├── main.go              # libp2p node binary (server + client roles)
│   ├── go.mod               # Go module definition
│   ├── go.sum               # Go dependency checksums
│   ├── docker/              # Docker build files
│   │   ├── compose.yml      # Network topology (profiles: local, test, public)
│   │   ├── node/            # AutoNAT node container
│   │   │   ├── Dockerfile
│   │   │   └── entrypoint.sh
│   │   └── router/          # NAT router (iptables simulator)
│   │       ├── Dockerfile
│   │       └── entrypoint.sh
│   ├── run.sh               # YAML-driven experiment runner
│   ├── run-local.sh         # Local experiment runner (no Docker, real NAT)
│   ├── eval-assertions.py   # Assertion evaluator for experiment logs
│   ├── scenarios/           # YAML scenario definitions
│   │   ├── matrix.yaml      # Full NAT × transport × server matrix (40 scenarios)
│   │   ├── packet-loss.yaml # Packet loss sweep (6 scenarios)
│   │   ├── high-latency.yaml# High latency sweep (4 scenarios)
│   │   ├── hotel-wifi.yaml  # Hotel WiFi reproduction with assertions
│   │   └── flight-wifi.yaml # Flight WiFi reproduction with assertions
│   └── verify-nat.sh        # NAT verification (12 tests, no libp2p)
└── results/                 # Experiment output (gitignored)
```
