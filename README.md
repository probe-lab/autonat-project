# AutoNAT v2 Measurement Lab

The first systematic performance evaluation of
[libp2p](https://libp2p.io/)'s
[AutoNAT v2](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
protocol across three implementations (go-libp2p, rust-libp2p, js-libp2p).

AutoNAT v2 lets libp2p nodes determine whether their addresses are publicly
reachable — critical for DHT participation, relay decisions, and hole
punching. This project evaluates its accuracy, performance, and
cross-implementation consistency using a Docker-based testbed with
configurable NAT types.

## Report

**[Final Report](docs/final-report.md)** — 5 findings, 183 testbed runs,
cross-implementation comparison, and recommendations.

Key results:
- **0% FNR / 0% FPR** for standard NAT types with ~6s convergence
- **Inconsistent global vs per-address reachability**: v1's global flag and v2's per-address signal can disagree, and no spec or implementation defines a canonical reduction. In go-libp2p the DHT consumes v1, which oscillates in 60% of runs with unreliable peers; v2's stable per-address result is ignored. rust/js don't have the bug today but nothing prevents it either (High)
- **UDP black hole**: blocks QUIC dial-back on fresh servers (Medium)
- **ADF false positive**: 100% FPR for address-restricted NAT (Low — protocol-level, ADF rare in modern routers)
- **Symmetric NAT missing signal**: no explicit UNREACHABLE emitted, but node is definitively unreachable on its IPv4 NAT path and DCUtR cannot rescue it either — impact is operational (no relay activation, no observability) (Low)
- **Transport resilience**: both TCP and QUIC maintain 0% error rates under packet loss; neither shows a statistically significant convergence advantage

## Quick Start

```bash
# Preview the full matrix without running
python3 testbed/run.py testbed/scenarios/matrix.yaml --dry-run

# Run a single scenario
python3 testbed/run.py testbed/scenarios/matrix.yaml \
  --filter=nat_type=none,transport=quic

# Run the full matrix
python3 testbed/run.py testbed/scenarios/matrix.yaml

# Run on your machine (no Docker, real network)
./testbed/run-local.sh --runs=3 --label=home-wifi
```

Requires Docker + Docker Compose on a **native Linux host** (Docker Desktop
on macOS has bridge networking issues). See [testbed.md](docs/testbed.md)
for requirements.

## Documentation

### Analysis

| Document | Description |
|----------|-------------|
| [Final Report](docs/final-report.md) | Findings, metrics, recommendations |
| [Cross-Implementation Comparison](docs/cross-implementation-comparison.md) | How each finding manifests in go/rust/js, feature matrix, adoption status |
| [v1 vs v2 Analysis](docs/v1-v2-analysis.md) | State transitions, wiring gap, fix options (F1 deep dive) |
| [Measurement Results](docs/measurement-results.md) | Complete testbed data + v1/v2 performance analysis |
| [ADF False Positive](docs/adf-false-positive.md) | Protocol design issue with testbed evidence |
| [Symmetric NAT Missing Signal](docs/symmetric-nat-silent-failure.md) | Cross-implementation root cause analysis |
| [UDP Black Hole Detector](docs/udp-black-hole-detector.md) | QUIC dial-back issue + fix options |
| [UPnP + NAT Detection](docs/upnp-nat-detection.md) | UPnP interaction with AutoNAT v2 + local home-router tests |
| [libp2p AutoNAT Ecosystem](docs/libp2p-autonat-ecosystem.md) | Survey of ~25 libp2p projects and their AutoNAT adoption |
| [Future Work: NAT Monitoring](docs/future-work-nat-monitoring.md) | Nebula + ants-watch proposal |

### Implementation Analysis

| Document | Description |
|----------|-------------|
| [go-libp2p](docs/go-libp2p-autonat-implementation.md) | Internals, constants, confidence system |
| [rust-libp2p](docs/rust-libp2p-autonat-implementation.md) | Port reuse analysis, address translation |
| [js-libp2p](docs/js-libp2p-autonat-implementation.md) | Confidence system, DHT integration |
| [Obol Impact](docs/obol.md) | Impact on Obol Network (Charon) |
| [Avail Impact](docs/avail.md) | Impact on Avail Network |

### Testbed

| Document | Description |
|----------|-------------|
| [Testbed Architecture](docs/testbed.md) | Docker setup, NAT rules, experiments |
| [AutoNAT v2 Protocol](docs/autonat-v2.md) | Protocol walkthrough |
| [Scenario Schema](docs/scenario-schema.md) | YAML scenario format |
| [OTel Tracing](docs/otel-tracing.md) | Trace format and Jaeger queries |
| [Running Experiments](testbed/README.md) | Commands, filtering, options |

## Project Structure

```
autonat/
├── docs/                    # Analysis and protocol documentation
├── testbed/                 # Docker testbed and experiment runner
│   ├── main.go              # go-libp2p node (server + client)
│   ├── run.py               # YAML-driven experiment runner
│   ├── analyze.py           # Trace analysis (FNR, FPR, TTC, TTU)
│   ├── docker/
│   │   ├── compose.yml      # Network topology
│   │   ├── node/            # go-libp2p container
│   │   ├── node-rust/       # rust-libp2p container
│   │   ├── node-js/         # js-libp2p container
│   │   └── router/          # NAT router (iptables)
│   └── scenarios/           # YAML scenario definitions
└── results/                 # Experiment output and figures
    ├── figures/             # Generated charts
    └── generate_figures.py  # Figure generation script
```
