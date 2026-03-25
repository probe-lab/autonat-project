# libp2p AutoNAT Ecosystem Survey

**Date:** 2026-03-24
**Scope:** All known projects using libp2p, with focus on AutoNAT adoption

---

## Summary

We surveyed 50+ projects to determine which use libp2p and whether they
enable AutoNAT. Key findings:

- **~25 projects** actively use libp2p for networking
- **~12 projects** enable AutoNAT (v1 or v2)
- **Only 2 projects** use AutoNAT v2: Kubo (deliberately) and Pactus (explicitly enabled)
- **Several large networks** skip AutoNAT entirely, relying on UPnP, discv5, or manual configuration
- **Avail disabled AutoNAT** because v1 was unreliable — the strongest signal that fixes are needed

---

## Projects Using AutoNAT

### AutoNAT v2 (explicit)

| Project | Lang | libp2p ver | AutoNAT | Nodes | Notes |
|---------|------|-----------|---------|-------|-------|
| **Kubo** (IPFS) | Go | v0.47 | v1 + v2 | ~575K total | Only production v2 deployment at scale |
| **Pactus** | Go | v0.47 | **v2** | ~hundreds | Explicitly calls `EnableAutoNATv2()` |

### AutoNAT v1 (explicit or default)

| Project | Lang | libp2p ver | Nodes | Notes |
|---------|------|-----------|-------|-------|
| **Lotus** (Filecoin) | Go | v0.47 | ~3-5K | Full AutoNAT service + API |
| **Forest** (Filecoin) | Rust | v0.56 | small | autonat feature, NatStatus exposed |
| **Venus** (Filecoin) | Go | v0.46 | small | Mirrors Lotus AutoNAT API |
| **Celestia** | Go | v0.47 | ~1,000+ | v1 by default, NATStatus API |
| **Avail** | Rust | v0.55 | ~500-1,200 | **Disabled since v1.13.2** due to reliability issues |
| **SSV Network** | Go | v0.45 | ~1,800 | v1 server mode only, manual port forwarding required |
| **Obol/Charon** | Go | v0.47 | ~hundreds | v1 oscillation observed in production |
| **Harmony** | Go | v0.36 | ~1,000 | EnableNATService + ForceReachabilityPublic |
| **Helia** (IPFS JS) | JS | v3.0 | unknown | v1 only (@libp2p/autonat) |
| **Pathfinder** (Starknet) | Rust | v0.53 | unknown | autonat feature enabled |
| **Ceramic** | Rust | v0.53 | unknown | autonat feature enabled |
| **Status/Waku** | Go | v0.39 | ~hundreds | v1 via go-waku |
| **Peergos** | Java | jvm | ~hundreds | Custom v1 implementation |
| **Optimism** (op-node) | Go | v0.36 | unknown | EnableNATService + rate limiting |

---

## Projects Using libp2p Without AutoNAT

### Uses alternative NAT traversal

| Project | Lang | libp2p ver | NAT strategy | Nodes |
|---------|------|-----------|-------------|-------|
| **Polkadot/Substrate** | Rust | rust-libp2p | None (skipped entirely) | ~10K |
| **Lighthouse** (Eth CL) | Rust | sigp fork | UPnP only | ~4,700 |
| **Grandine** (Eth CL) | Rust | sigp fork | UPnP only | ~200 |
| **MultiversX** | Go | v0.38 | UPnP only | ~3,200 |
| **Mina** | Go | v0.27 | UPnP only | ~1,000 |
| **IOTA/Shimmer** | Go | v0.33 | UPnP only | ~thousands |
| **Mysterium** | Go | v0.46 | Own STUN-based | ~22,000 |
| **Juno** (Starknet) | Go | v0.47 | Relay + hole punch | unknown |
| **nwaku** (Waku, Nim) | Nim | nim-libp2p | UPnP/NAT-PMP | ~hundreds |

### No NAT traversal

| Project | Lang | libp2p ver | Nodes | Notes |
|---------|------|-----------|-------|-------|
| **Lodestar** (Eth CL) | JS | v3.1 | ~1,000 | No autonat package |
| **Algorand** | Go | v0.47 | ~1-2K | NoListenAddrs, no NAT at all |
| **Fluence** | Rust | v0.53 | unknown | No autonat feature |
| **Papyrus** (Starknet) | Rust | v0.53 | unknown | No autonat feature |
| **Polygon Edge** | Go | v0.32 | unknown | No NAT options |
| **Bacalhau** | Go | v0.43 | unknown | Migrating to NATS |

### Explicitly disabled AutoNAT

| Project | Lang | Notes |
|---------|------|-------|
| **Avail** | Rust | Disabled v1.13.2 (Sep 2025), operators must set `--external-address` |
| **Berty** | Go | `AutoNATServiceDisabled`, focuses on BLE/mDNS |

---

## Projects NOT Using libp2p

| Project | Networking stack | Notes |
|---------|-----------------|-------|
| Ethereum EL (Geth, Nethermind) | devp2p | Custom Ethereum protocol |
| Ethereum CL (Teku, Nimbus) | jvm/nim-libp2p | Have autonat in lib, not configured |
| Prysm (Eth CL) | go-libp2p v0.39 | v2 enabled by go-libp2p default but not used for reachability |
| Arbitrum (Nitro) | WebSocket feeds | No P2P layer |
| Celo | devp2p (geth fork) | Ethereum-style |
| Aptos | Custom (aptos-network) | Noise-based, not libp2p |
| Sui | Anemo (MystenLabs) | Custom P2P library |
| Solana | Custom gossip + QUIC | Own protocol |
| Nym | Custom Sphinx mixnet | libp2p only in SDK examples |
| Cosmos/CometBFT | MConnection (custom) | libp2p transport planned (ADR-073) |
| EigenDA | gRPC | No P2P |
| Chainlink | Custom OCR P2P | Not libp2p |
| iroh (n0) | Custom QUIC | Own relay + STUN-like NAT |
| Drand | gRPC/HTTP | No P2P |
| Rocket Pool | None (uses EL/CL clients) | Smart contracts + daemon |
| Lido | None (smart contracts) | Relies on Obol/SSV operators |
| Matrix/Element | HTTPS federation | Experimental libp2p branch abandoned |
| Gun.js | WebSocket/WebRTC | Own protocol |

---

## NAT Monitoring Priority

Based on network size, AutoNAT usage, and known issues:

### Tier 1: Highest value

| Network | Why |
|---------|-----|
| **IPFS Amino** | Largest libp2p network (~575K peers), v2 deployed, ProbeLab infrastructure ready |
| **Avail** | Disabled AutoNAT due to our documented issues — monitoring proves the fix works |
| **Obol/Charon** | Home stakers behind NAT by design, v1 oscillation confirmed |
| **Celestia** | Light client reachability critical for data availability sampling |

### Tier 2: Moderate value

| Network | Why |
|---------|-----|
| **Filecoin** (Lotus/Forest) | Mature AutoNAT deployment, mostly datacenter but retrieval clients growing |
| **SSV Network** | ~1,800 operators, manual port forwarding pain point |
| **Polkadot** | ~10K peers with no AutoNAT at all — monitoring could justify enabling it |
| **Pactus** | Only other v2 deployment — validation of v2 in production |

### Tier 3: Lower priority

| Network | Why |
|---------|-----|
| **Harmony, Optimism, Starknet** | Smaller networks or unknown deployment scale |
| **Mysterium** | Large (22K) but uses own STUN-based NAT detection |
| **Helia, Ceramic, Peergos** | Smaller deployments |

---

## Key Observations

1. **AutoNAT v2 adoption is near-zero.** Only Kubo and Pactus use it.
   Everyone else is on v1 or nothing.

2. **UPnP is the most common alternative.** Lighthouse, MultiversX, Mina,
   IOTA, and nwaku all prefer UPnP/NAT-PMP over AutoNAT.

3. **Large networks skip AutoNAT entirely.** Polkadot (10K nodes),
   Ethereum CL clients (9K+ combined), and Mysterium (22K) don't use it.
   This suggests either: (a) AutoNAT isn't trusted, (b) these networks
   assume public IPs, or (c) operators are expected to configure manually.

4. **The go-libp2p ecosystem dominates.** Most AutoNAT users are Go
   projects. rust-libp2p has the ephemeral port bug (Finding #3),
   js-libp2p emits no events (Finding #7) — which may explain low
   adoption in Rust/JS projects.

5. **Filecoin is the most mature AutoNAT deployment** after IPFS. All
   three implementations (Lotus, Forest, Venus) enable it with API
   endpoints.

6. **CometBFT/Cosmos is planning libp2p adoption** (ADR-073). When this
   ships (~Q3 2026), ~180+ Cosmos Hub validators plus the broader
   Cosmos ecosystem will become libp2p users — potentially the largest
   new deployment.
