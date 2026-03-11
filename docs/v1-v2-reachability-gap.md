# AutoNAT v1/v2 Reachability Gap

**Date:** 2026-03-11
**GitHub Issue:** [#60](https://github.com/probe-lab/autonat-project/issues/60)
**Affects:** go-libp2p (primary), rust-libp2p (partial), js-libp2p (resolved by design)

---

## Summary

AutoNAT v1 and v2 coexist in libp2p but produce **independent, incompatible
reachability signals**. v1 emits a global Public/Private/Unknown flag. v2 emits
per-address reachability (which addresses are confirmed reachable). There is no
bridge between them — v2 results do not feed into the v1 global flag.

This matters because every subsystem that reacts to reachability (AutoRelay,
Kademlia DHT mode, address advertisement) consumes the **v1 global flag only**.
A node can have v2-confirmed reachable addresses while v1 simultaneously reports
Private, triggering unnecessary relay usage and DHT client mode.

---

## The Problem by Implementation

### go-libp2p (worst affected)

v1 and v2 are completely independent systems with separate event types:

| System | Event | Type | Consumers |
|--------|-------|------|-----------|
| **v1** | `EvtLocalReachabilityChanged` | Global: Public / Private / Unknown | AutoRelay, Kademlia DHT, Address Manager, NAT Service |
| **v2** | `EvtHostReachableAddrsChanged` | Per-address: list of reachable/unreachable addrs | Address Manager (separate code path) |

**No reduction function exists.** v2 per-address results are never aggregated
into a v1-compatible global signal. The four v1 consumers have no way to learn
about v2 results.

#### Why v1 Is Inherently Flaky (Issue #8)

The v1 global flag oscillates because of how AutoNAT v1 selects dial-back peers
and accumulates confidence:

1. **Random peer selection:** v1 picks a random connected peer to perform each
   dial-back. On the public IPFS network, the node connects to dozens of DHT
   peers. Some are on the same subnet or have favorable NAT paths; others cannot
   reach the node.

2. **Sliding window confidence:** v1 uses a sliding window of the last
   `maxConfidence=3` results. Each probe replaces the oldest result. With random
   peer selection, a single failed dial-back from a peer that happens to be
   behind its own restrictive NAT can flip the window from Public to Private.

3. **DHT peer churn:** As DHT routing table maintenance connects/disconnects
   peers, the pool of potential dial-back peers changes continuously. A peer
   that successfully dialed back may disconnect, and the next randomly selected
   peer may fail — even though the node's actual network situation hasn't changed.

4. **No distinction between "peer can't reach me" and "I'm behind NAT":** v1
   treats any `IsDialError` as evidence of Private reachability. But the dial
   may fail because the *server* is behind NAT, has a full connection table, or
   has a transient network issue — none of which reflect the client's
   reachability.

**Observed rate:** ~33% of runs oscillate between Public and Private, confirmed
in both low-latency (6ms hotel WiFi) and high-latency (711ms satellite)
environments. The oscillation is independent of network conditions — it is
inherent to the random peer selection + sliding window design.

**Why v2 doesn't have this problem:** v2 tests specific addresses against
specific servers that support the `/libp2p/autonat/2/dial-request` protocol.
The server performs a structured dial-back with nonce verification. Results are
per-address and require `targetConfidence=3` consistent results before changing
state. There is no random peer selection — the client explicitly selects v2
servers from its connected peers.

#### v1 Global Flag Consumers in go-libp2p

**1. AutoRelay** (`p2p/host/autorelay/autorelay.go`)

Subscribes to `EvtLocalReachabilityChanged`:
- `ReachabilityPrivate` or `Unknown` → starts relay finder, reserves relay slots, advertises relay addresses
- `ReachabilityPublic` → stops relay finder, drops relay reservations

AutoRelay needs a binary decision: "should I use relays?" Per-address
reachability from v2 doesn't directly map — if TCP is reachable but QUIC is not,
what should AutoRelay do? Today it **strictly requires the v1 global flag**.

**2. Kademlia DHT** (`go-libp2p-kad-dht/subscriber_notifee.go`)

Subscribes to `EvtLocalReachabilityChanged` when in `ModeAuto` or `ModeAutoServer`:
- `ReachabilityPublic` → switches to **Server** mode (registers DHT protocol handlers, serves queries)
- `ReachabilityPrivate` → switches to **Client** mode (only issues queries)
- `ReachabilityUnknown` → Client (ModeAuto) or Server (ModeAutoServer)

The DHT's client/server distinction is global — a node either serves DHT queries
or doesn't. A reasonable v2 mapping would be "server mode if any address is
reachable," but this logic doesn't exist.

**3. Address Manager** (`p2p/host/basic/addrs_manager.go`)

Subscribes to `EvtLocalReachabilityChanged`, stores the value atomically.
In `getDialableAddrs()`:

- When `ReachabilityPrivate` **and** relay addresses are available → removes
  public addresses from the advertised set and replaces them with relay addresses

This is the mechanism that makes `host.Addrs()` return relay multiaddrs for
NATted nodes. With per-address v2 data, the address manager could selectively
replace only unreachable addresses while keeping reachable ones — but today it
uses the v1 global flag as a blanket decision.

**4. NAT Service (AutoNAT v1 server)** (`p2p/host/autonat/svc.go`)

Not a subscriber — the v1 client directly calls `Enable()`/`Disable()`:
- `ReachabilityPublic` → enables `/libp2p/autonat/1.0.0` stream handler (serves dial-back requests)
- `ReachabilityPrivate` → disables stream handler (can't usefully serve behind NAT)
- `ReachabilityUnknown` → enables (optimistic)

**5. Identify** (`p2p/protocol/identify/id.go`)

Does **not** subscribe to reachability events. Reacts indirectly to address
changes via `EvtLocalAddressesUpdated`. Reports whatever `host.Addrs()` returns.

#### Observed Consequences

In live testing against the IPFS network (2026-03-10, `run-local.sh`):

1. v2 confirms 2 addresses reachable at ~5s
2. v1 reports Public at ~8s (DHT peers confirm)
3. v1 decays to Private at ~45s (DHT peer churn — random peers can't dial back)
4. v2 addresses remain reachable throughout
5. AutoRelay activates, DHT switches to client mode — **despite confirmed v2 reachability**

The v1 oscillation (Issue #8, ~33% rate) compounds this: even when v1 briefly
reaches Public, it can decay back to Private due to DHT peer selection noise.
v2 results are stable but ignored by the subsystems that matter.

### rust-libp2p (partially affected)

rust-libp2p has a different architecture that partially mitigates the gap:

| System | Signal | Consumers |
|--------|--------|-----------|
| **v1** | `autonat::v1::Event::StatusChanged { NatStatus }` | User application code only |
| **v2** | `ToSwarm::ExternalAddrConfirmed(addr)` / `ExternalAddrExpired(addr)` | Kademlia, Identify, Rendezvous — via `FromSwarm` broadcast |

**Key difference:** rust-libp2p's Kademlia reacts to the **external address
list**, not a global reachability flag. When v2 confirms an address via
`ExternalAddrConfirmed`, the Swarm broadcasts it to all behaviours. Kademlia
checks `external_addresses.is_empty()` — if any address is confirmed, it
switches to server mode.

This means **v2 results DO flow into Kademlia** in rust-libp2p, unlike go-libp2p.

However, rust-libp2p has **no built-in AutoRelay** (the relay client is manual —
user code must decide when to reserve relay slots). There is no automatic
"private → start relaying" behavior to break.

| Aspect | go-libp2p | rust-libp2p |
|--------|-----------|-------------|
| v2 → Kademlia | Broken (v1 only) | Works (address list) |
| v2 → AutoRelay | Broken (v1 only) | N/A (no autorelay) |
| v2 → Address advertisement | Broken (v1 only) | Works (Identify uses external addrs) |
| Global flag needed? | Yes (4 consumers) | No (address-list based) |

### js-libp2p (resolved by design)

js-libp2p **never had a global reachability flag**. The architecture is
per-address from the ground up:

- AutoNAT (v1 or v2) calls `addressManager.confirmObservedAddr(addr)` or
  `removeObservedAddr(addr)`
- `AddressManager` updates the peer store, emitting `self:peer:update`
- Kademlia subscribes to `self:peer:update` and checks whether any public
  non-relay address exists → switches server/client mode accordingly
- Circuit relay operates independently (always searches for relays if configured)
- `getAddresses()` only returns verified addresses

Both v1 and v2 feed into the **same pipeline**. There is no separate v1 global
flag that can diverge from v2 per-address results. js-libp2p's design is what
go-libp2p should converge toward.

---

## Cross-Implementation Comparison

| Feature | go-libp2p | rust-libp2p | js-libp2p |
|---------|-----------|-------------|-----------|
| v1 global flag | `network.Reachability` enum | `NatStatus` enum | **None** |
| v2 signal | `EvtHostReachableAddrsChanged` | `ExternalAddrConfirmed/Expired` | `confirmObservedAddr()` |
| v2 → DHT mode | **No** (DHT reads v1 only) | **Yes** (reads address list) | **Yes** (reads address list) |
| v2 → Relay | **No** (AutoRelay reads v1 only) | N/A (manual relay) | N/A (relay is independent) |
| v2 → Address advertisement | **No** (addr mgr reads v1 only) | **Yes** (Identify reads external addrs) | **Yes** (returns verified addrs) |
| v1/v2 can diverge? | **Yes** — confirmed | Partially (v1 exists but less consumed) | **No** — single pipeline |

---

## Impact on External Projects

### Obol/Charon (go-libp2p v0.47.0) — HIGH IMPACT

Charon directly subscribes to `EvtLocalReachabilityChanged` and exports it as a
Prometheus gauge `p2p_reachability_status` (0=unknown, 1=public, 2=private) on
their Grafana dashboard.

**Consequences:**
- Dashboard metric oscillates due to v1 Issue #8 (~33% rate), even when v2
  confirms stable reachability
- AutoRelay activates unnecessarily when v1 decays to Private
- DHT switches to client mode, reducing the node's participation in peer
  discovery
- Validator coordination may degrade if relay paths have higher latency than
  direct paths

See [docs/obol.md](obol.md) for full Obol analysis.

### Avail (rust-libp2p v0.55.0) — MEDIUM IMPACT

Avail uses rust-libp2p AutoNAT v1 only. They **disabled AutoNAT entirely** in
v1.13.2 (September 2025) after progressive reliability issues. Their changelog
mentions "autonat-over-quic libp2p errors" as early as v1.7.4.

**Consequences:**
- AutoNAT disabled → nodes can't automatically determine reachability
- Operators must manually set `--external-address` for server mode
- Kademlia server mode chicken-and-egg: without AutoNAT, external addresses are
  only populated manually
- A working v2 (with the address-list-based Kademlia in rust-libp2p) could
  resolve this, but Issue #1 (addr-restricted false positive) would need fixing
  first

See [docs/avail.md](avail.md) for full Avail analysis.

### Celestia (go-libp2p) — LOW IMPACT

Celestia uses `libp2p.NATPortMap()` but does not directly subscribe to
reachability events. Affected indirectly:
- DHT auto-mode switches based on v1, so v1 oscillation can cause DHT mode
  flapping
- No direct reachability metric exposed

### Ethereum Consensus Layer

**Prysm** (go-libp2p): Uses `NATPortMap()` + manual configuration. No AutoNAT
or AutoRelay. Not affected.

**Lighthouse** (rust-libp2p): Uses UPnP + discv5 for peer discovery. No AutoNAT.
Not affected.

---

## Relationship to Other Issues

| Issue | Relationship |
|-------|-------------|
| **#8 v1 oscillation** | Root cause of v1 decay; v2 results are stable but not consumed |
| **#1 Addr-restricted false positive** | Affects v2 accuracy; even if v2 fed into v1, this FP would propagate |
| **#17 Symmetric blocks v2** | When v2 can't run, only v1 is available — and it oscillates |
| **#3 v1 stuck private after UPnP remap** | v1 fires Private on original port, never learns about UPnP-remapped port that v2 confirms |
| **#2 QUIC dial-back failure** | When v2 can't test QUIC, that transport remains "unknown" — only v1 provides (unreliable) signal |

---

## Recommended Fix for go-libp2p

The fix is a **reduction function** that bridges v2 per-address results into the
v1 global flag, or (preferably) refactors consumers to use address-level
reachability directly.

### Option A: Reduction Function (minimal change)

Add logic to the address reachability tracker that emits
`EvtLocalReachabilityChanged` based on v2 results:

```
if any v2-confirmed address is reachable → emit ReachabilityPublic
if all probed addresses are unreachable  → emit ReachabilityPrivate
if still probing                         → emit ReachabilityUnknown
```

This would make AutoRelay, DHT, and Address Manager react to v2 results without
changing their subscription code. The v1 AutoNAT client would still run
independently — the reduction function would override its signal when v2 has data.

**Pros:** Minimal change to consumers. Backward compatible.
**Cons:** Loses per-address granularity. Still a global binary decision.

### Option B: Per-Address Consumers (js-libp2p model)

Refactor consumers to check the confirmed address list instead of a global flag:

- **AutoRelay:** "start relaying if no address is confirmed reachable"
- **Kademlia:** "server mode if any address is confirmed reachable"
- **Address Manager:** "replace only unreachable addresses with relay equivalents"

This is what js-libp2p already does and what rust-libp2p's Kademlia already does.

**Pros:** Most correct. Enables per-transport decisions (relay QUIC but not TCP).
**Cons:** Larger refactor. Requires all consumers to change.

### Option C: Hybrid (pragmatic)

Implement Option A as an immediate fix, then migrate consumers to Option B over
time. The reduction function serves as a compatibility bridge during the
transition.
