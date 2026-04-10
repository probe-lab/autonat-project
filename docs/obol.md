# Obol Network — AutoNAT v2 Impact

## Affected Repository

**[charon](https://github.com/ObolNetwork/charon)** — Distributed Validator (DV) middleware
for Proof of Stake Ethereum. Uses **go-libp2p v0.47.0** (same version analyzed in this project).

### What Charon Does

Charon coordinates multiple operators running validator nodes. Each operator runs a
charon node at home or in a data center, and the nodes communicate peer-to-peer using
libp2p to perform distributed validator duties (threshold signing, consensus).

### Why AutoNAT Matters

Operators typically run nodes behind home or corporate NAT. AutoNAT determines whether
a node can receive inbound connections — this affects whether the node advertises its
address to the cluster or relies on relay infrastructure.

## Evidence of AutoNAT/Reachability Issues

### Reachability Metric

Charon exposes AutoNAT's verdict as a Prometheus gauge
([PR #938](https://github.com/ObolNetwork/charon/pull/938)):

```go
// p2p/metrics.go
reachableGauge = promauto.NewGauge(prometheus.GaugeOpts{
    Namespace: "p2p",
    Name:      "reachability_status",
    Help:      "Current libp2p reachability status of this node as detected by autonat: unknown(0), public(1) or private(2).",
})
```

The gauge subscribes directly to go-libp2p's `EvtLocalReachabilityChanged` (AutoNAT v1),
so any oscillation in AutoNAT verdicts is immediately visible to operators. AutoNAT was
enabled by default via [PR #889](https://github.com/ObolNetwork/charon/pull/889).

### Connectivity and Relay Issues

- [Issue #890 — Enable libp2p hole punching](https://github.com/ObolNetwork/charon/issues/890):
  Documents the core problem — NATed nodes are stuck on relay connections that "introduce
  another network hop" and are "recycled every few minutes requiring error handling and
  reconnects."
- [Issue #2114 — Regularly attempt direct dials even if relay conns exist](https://github.com/ObolNetwork/charon/issues/2114):
  Increasing relay connection TTL to 1 hour prevented direct connection upgrades — "libp2p
  not attempting to dial if existing connection is found. Since we now have long lived relay
  connections, they are used for the whole hour."
- [Issue #4233 — Improve NAT hole punching](https://github.com/ObolNetwork/charon/issues/4233)
  (open, February 2026): "Currently our success rate of NAT hole punching is quite low.
  With the same set of two peers hole punching is inconsistent."
- [Issue #4242 — Relay exhaustion during crash loops](https://github.com/ObolNetwork/charon/issues/4242):
  Crash-looping nodes exhaust `RESERVATION_REFUSED` limits, causing DKG failures for
  other peers sharing the relay.

### Official Documentation

- [Charon Networking docs](https://docs.obol.org/learn/charon/charon-networking): "If a
  pair of Charon clients are not publicly accessible, due to being behind a NAT, they will
  not be able to upgrade their relay connections to a direct connection." Relay connections
  "result in decreased validator effectiveness and possible missed block proposals and
  attestations."
- [Deployment Best Practices](https://docs.obol.org/run-a-dv/prepare/deployment-best-practices):
  Warns operators to open port 3610 or configure NAT gateway. States direct connections
  "can halve the latency."
- [Errors & Resolutions](https://docs.obol.org/advanced-and-troubleshooting/troubleshooting/errors):
  Documents `RESERVATION_REFUSED` and `NO_RESERVATION` relay errors operators encounter.

## Relevant AutoNAT v2 Issues

### Issue #1: Address-Restricted NAT False Positive

Nodes behind address-restricted NAT (ADF) would be classified as **reachable** by
AutoNAT v2 when they are not. The `dialerHost` shares the server's IP, so the
dial-back passes the NAT's address filter. See `docs/report.md`
Issue #1 for the full analysis.

**Impact on Charon:** A node behind ADF NAT would advertise its public address to
the cluster, skip relay usage, and be unreachable to other cluster members. This
could degrade or break validator coordination.

### Issue #16b: QUIC Dial-Back Failure

AutoNAT v2 QUIC dial-back fails — the `dialerHost` on the server has no listening
addresses, which may prevent QUIC transport initialization. Nodes relying on QUIC
cannot determine their reachability and remain in "unknown" state indefinitely.

**Impact on Charon:** Nodes using QUIC transport would never get a reachability
determination, potentially falling back to relay unnecessarily or remaining in an
indeterminate state.

## Obol Infrastructure Context

Obol maintains **[terraform-charon-relay](https://github.com/ObolNetwork/terraform-charon-relay)**
— Terraform modules for deploying relay nodes. This indicates they already use relays
for NAT traversal, but a false positive from AutoNAT v2 could cause a node to skip
relaying when it actually needs it.

## Related Obol Repositories

| Repository | Description | Relevance |
|-----------|-------------|-----------|
| [charon](https://github.com/ObolNetwork/charon) | DV middleware (go-libp2p v0.47.0) | Directly affected |
| [charon-distributed-validator-node](https://github.com/ObolNetwork/charon-distributed-validator-node) | Docker Compose deployment for charon | Deployment wrapper, inherits charon's libp2p |
| [lido-charon-distributed-validator-node](https://github.com/ObolNetwork/lido-charon-distributed-validator-node) | Lido-specific DV deployment | Deployment wrapper, inherits charon's libp2p |
| [terraform-charon-relay](https://github.com/ObolNetwork/terraform-charon-relay) | Relay node infrastructure | Relay fallback for NATed nodes |
| [obol-sdk](https://github.com/ObolNetwork/obol-sdk) | TypeScript client SDK | Not directly affected (no libp2p) |
