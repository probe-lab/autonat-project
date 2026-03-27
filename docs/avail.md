# Avail Network — AutoNAT Impact

## Affected Repository

**[avail-light](https://github.com/availproject/avail-light)** — Light client for the Avail
data availability network. Uses **rust-libp2p v0.55.0** with `libp2p-autonat` v0.14.0
(**AutoNAT v1 only** — v2 module exists in the crate but Avail imports the default `v1::*`
re-export).

### What Avail Light Does

Avail is a data availability (DA) layer for blockchains. The light client performs Data
Availability Sampling (DAS) — downloading and verifying random cells from block data to
confirm availability without downloading entire blocks. Light clients form a peer-to-peer
network using libp2p's Kademlia DHT for peer discovery and data distribution.

The `avail-light` workspace contains multiple binaries:
- **`client/`** — standard light client
- **`fat/`** — fat client (stores more data)
- **`bootstrap/`** — bootstrap/relay node
- **`core/`** — shared networking library
- **`crawler/`** — network crawler

### Why AutoNAT Matters

Light clients need to determine whether they can receive inbound connections. This affects:
- **Kademlia mode** — nodes with confirmed external addresses can switch from Kademlia
  client to server mode, making them full DHT participants
- **Network health** — nodes that cannot serve data reduce the network's sampling capacity
- **Bootstrap load** — when nodes can't determine reachability, they remain dependent on
  bootstrap nodes for all interactions

## Current State: AutoNAT Disabled by Default

Avail has **disabled AutoNAT by default** for light clients since
[v1.13.2](https://github.com/availproject/avail-light/releases/tag/avail-light-client-v1.13.2)
(September 2025). The changelog history reveals a progressive retreat from AutoNAT:

| Version | Date | Change | Source |
|---------|------|--------|--------|
| v1.7.4 | Nov 2023 | "Bootstrap nodes are now used as primary autonat servers; in order to mitigate existing **autonat-over-quic libp2p errors**, bootstraps TCP listeners are used" | [Release notes](https://github.com/availproject/avail-light/releases/tag/v1.7.4) |
| — | Dec 2023 | Removed QUIC listener entirely "due to ongoing autonat issues" (references upstream [rust-libp2p#3900](https://github.com/libp2p/rust-libp2p/issues/3900)) | [PR #390](https://github.com/availproject/avail-light/pull/390) |
| v1.12.12 | 2025-05-12 | Changed `only_global_ips` from `false` to `true`; increased AutoNAT timeouts | [PR #827](https://github.com/availproject/avail-light/pull/827), [core CHANGELOG](https://github.com/availproject/avail-light/blob/main/core/CHANGELOG.md) |
| v1.12.13 | 2025-05-30 | Exposed additional AutoNAT configurations; reduced throttling (`global_max` 30→10, `peer_max` 3→1) | [PR #835](https://github.com/availproject/avail-light/pull/835) |
| v1.13.0 | 2025-07-21 | Added AutoNAT service mode configs; **attempted switch to AutoNAT v2** | [PR #887](https://github.com/availproject/avail-light/pull/887) |
| — | 2025-07-16 | **Reverted AutoNAT v2 after 7 days** | [PR #896](https://github.com/availproject/avail-light/pull/896) |
| v1.13.2 | 2025-09-15 | **Disabled AutoNAT and automatic server mode by default**; added `--external-address` parameter | [PR #932](https://github.com/availproject/avail-light/pull/932) |

The upstream bug that started the retreat is
[rust-libp2p#3900](https://github.com/libp2p/rust-libp2p/issues/3900) — "AutoNAT on QUIC
falsely reports public NAT status" — where AutoNAT v1 over QUIC incorrectly reports
`NatStatus::Public` for nodes behind NAT because QUIC hole-punching reuses the same 4-tuple.

The v2 attempt and revert (PRs #887 → #896) is notable: Avail tried switching to AutoNAT
v2 in July 2025 but reverted within a week, before ultimately disabling AutoNAT entirely
in September. This suggests v2 also did not solve their production issues — likely due to
the TCP port reuse problem documented in Finding 6.

The final step — disabling AutoNAT entirely — indicates that neither v1 nor v2 was working
reliably for their network. Operators must now manually set `--external-address` for
server-mode nodes.

## AutoNAT v1 Configuration (When Enabled)

From `core/src/network/p2p/configuration.rs`:

```rust
retry_interval: Duration::from_secs(90),
refresh_interval: Duration::from_secs(15 * 60),
boot_delay: Duration::from_secs(15),
throttle: Duration::from_secs(90),
only_global_ips: true,
throttle_clients_global_max: 30,
throttle_clients_peer_max: 3,
```

Bootstrap nodes have relaxed settings (`global_max: 120`, `peer_max: 4`,
`only_global_ips: false`) and serve as AutoNAT servers via:

```rust
pub async fn bootstrap_on_startup(&self) -> Result<()> {
    for (peer, addr) in self.bootstraps.iter().map(Into::into) {
        self.dial_peer(peer, vec![addr.clone()], PeerCondition::Always).await?;
        if self.auto_nat_mode == AutoNatMode::Enabled {
            self.add_autonat_server(peer, addr).await?;
        }
    }
    Ok(())
}
```

## NAT Traversal Gaps

Avail light clients have **no NAT traversal capability**:

| Feature | Status | Notes |
|---------|--------|-------|
| AutoNAT v1 | Disabled by default | Caused reliability issues |
| AutoNAT v2 | Not used | `libp2p-autonat` 0.14.0 has v2 module but Avail imports v1 |
| Relay client | **Not implemented** | Relay server exists (`avail-light-relay`) but light clients have no relay client behaviour |
| DCUtR / hole punching | **Not implemented** | No hole punching support |
| UPnP | **Removed** (v1.2.1, Feb 2025) | Explicitly removed |
| mDNS | **Removed** (v1.2.6, Apr 2025) | Removed due to arithmetic overflow bug |

This means a light client behind NAT has **no path to becoming reachable**. It can only
operate as a Kademlia client, never contributing DHT serving capacity to the network.

## Relevant AutoNAT v2 Issues

### Issue #1: Address-Restricted NAT False Positive

If Avail were to adopt AutoNAT v2, nodes behind address-restricted NAT (ADF) would be
incorrectly classified as **reachable**. The `dialerHost` shares the server's IP, so the
dial-back passes the NAT's address filter. See `docs/report.md` Issue #1.

**Impact on Avail:** A light client behind ADF NAT would switch to Kademlia server mode and
attempt to serve DAS data. Other peers trying to connect from different IPs would fail,
reducing data availability sampling success rates.

### QUIC AutoNAT False Positive (rust-libp2p#3900)

Avail's "autonat-over-quic libp2p errors" trace back to a known upstream bug:
[rust-libp2p#3900](https://github.com/libp2p/rust-libp2p/issues/3900) — "AutoNAT on QUIC
falsely reports public NAT status." AutoNAT v1 over QUIC incorrectly reports
`NatStatus::Public` for nodes behind NAT because QUIC connection reuse means the dial-back
uses the same 4-tuple as the original connection, so the NAT lets it through. This is a
**false positive** — NATed nodes are told they are publicly reachable when they are not.

This is a different issue from go-libp2p's UDP black hole detector problem (Finding 5 in
the final report), which causes **false negatives** — the server refuses to attempt QUIC
dial-backs entirely because its black hole counter is in `Blocked` state. rust-libp2p has
no black hole detector, so that issue does not apply.

Avail's response to rust-libp2p#3900 was progressive:
1. v1.7.4 (Nov 2023): switched AutoNAT to TCP-only bootstrap listeners as a workaround
2. Dec 2023: removed the QUIC listener entirely ([PR #390](https://github.com/availproject/avail-light/pull/390))
3. v1.13.2 (Sep 2025): disabled AutoNAT altogether

**Impact on Avail:** Light clients behind NAT were incorrectly classified as publicly
reachable, causing them to switch to Kademlia server mode and advertise addresses that
peers from other IPs could not reach — reducing data availability sampling success rates.

### Issue #17: Symmetric NAT Bypasses v2

Symmetric NAT (common on mobile carriers and CGNAT) prevents AutoNAT v2 from ever
activating a public address. The observed address manager requires consistent external ports
across peers, which symmetric NAT cannot provide.

**Impact on Avail:** Light clients on mobile networks (a key target demographic for light
clients) would never get a reachability determination from v2, falling back to v1 which
Avail has already found unreliable.

## Kademlia Server Mode Chicken-and-Egg Problem

Avail's automatic Kademlia server mode switch requires external addresses to be confirmed:

```rust
if matches!(context.kad_mode, Mode::Client) && !external_addresses.is_empty() {
    if memory_gb > memory_gb_threshold && cpus > cpus_threshold {
        kad.set_mode(Some(Mode::Server));
    }
}
```

With AutoNAT disabled, `external_addresses` is only populated via the manual
`--external-address` CLI flag. This creates a barrier for casual operators who don't know
their public IP or can't configure it statically (e.g., behind DHCP NAT).

A working AutoNAT v2 would resolve this by automatically confirming external addresses,
enabling the server mode switch without manual configuration.

## Related Avail Repositories

| Repository | Description | Relevance |
|-----------|-------------|-----------|
| [avail-light](https://github.com/availproject/avail-light) | Light client (rust-libp2p v0.55.0) | Directly affected — AutoNAT disabled |
| [avail-light-relay](https://github.com/availproject/avail-light-relay) | Circuit Relay v2 server (rust-libp2p v0.53.1) | Relay server exists but clients can't use it |
| [avail](https://github.com/availproject/avail) | Full node (Substrate/Polkadot SDK) | Uses Substrate's built-in libp2p |
| [rust-libp2p](https://github.com/availproject/rust-libp2p) | Fork of libp2p/rust-libp2p | Upstream fork for custom patches |

## Potential Value of AutoNAT v2 for Avail

1. **Re-enable automatic reachability detection** — v2's per-address testing and nonce-based
   verification could provide the reliability that v1 lacked
2. **Resolve the Kademlia server mode problem** — confirmed external addresses would enable
   automatic server mode without `--external-address`
3. **QUIC support** — v2 uses a separate dial-back connection with nonce verification,
   which avoids the QUIC connection-reuse false positive (rust-libp2p#3900) that forced
   Avail to TCP-only bootstrapping
4. **Reduce bootstrap node dependency** — reliable reachability detection would allow more
   nodes to serve as DHT servers, distributing load away from bootstrap nodes
5. **Enable relay client** — with accurate reachability info, nodes could automatically
   decide whether to use relay infrastructure (currently the relay server exists but clients
   can't use it)

However, two issues would need to be addressed before Avail could rely on v2:
- The TCP port reuse safety net (Finding 6) — rust-libp2p's v2 produces 100% false
  negatives when TCP port reuse fails silently, which is likely what caused the v2
  attempt/revert cycle (PRs #887 → #896).
- The ADF false positive (Finding 3) — nodes behind address-restricted NAT would be
  incorrectly classified as globally reachable, causing them to advertise unreachable
  addresses.
