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
