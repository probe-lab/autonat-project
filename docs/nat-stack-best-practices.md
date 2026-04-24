# NAT Best Practices for libp2p developers

This document is aimed to be a series of runbooks (per-implementation) for libp2p developers 
for enabling and wiring libp2p's NAT-detection stack (Identify, AutoNAT v1/v2, UPnP/NAT-PMP) and
integrating it with existing libp2p protocols, such as DHT and AutoRelay. This document integrates the findings from 
[this report](final-report.md).

Servers with known static public IPs (cloud, bare metal) can usually
skip AutoNAT entirely and announce their address directly using `libp2p.ForceReachabilityPublic()`— the
runbooks below target apps that need runtime NAT discovery.

## go-libp2p

### 1. Enable AutoNAT v2
```go
host, err := libp2p.New(
    libp2p.EnableAutoNATv2(),
)
```

`EnableAutoNATv2()` activates the v2 **client** (probes other peers
to learn your own reachability) and the v2 **server** (responds to
other peers' probes).

Important: **v1 is not controlled by this option.** The v1 **client**
runs ambiently in go-libp2p regardless of what you set — there is no
way to disable it. It probes other peers and publishes
`EvtLocalReachabilityChanged` (Public/Private/Unknown) on the event
bus, which DHT and AutoRelay consume by default. So "v2-only" is not
achievable in go-libp2p today — your options are v1 alone, or
v1+v2.

The v1 **server** (responding to other peers' v1 probes) is opt-in
via a separate option:

```go
libp2p.New(
    libp2p.EnableAutoNATv2(),
    libp2p.EnableNATService(),  // adds v1 server — respond to others' v1 probes
)
```

Practical combinations: v1 client alone (no options), v1 client +
v1 server (`EnableNATService`), v1 client + v2 (`EnableAutoNATv2`),
or all four (both options).

### 2. Detect NAT mapping type and pick a fallback

Beyond "am I reachable?", knowing the NAT's *mapping* behavior
determines which fallbacks are available. go-libp2p's
`ObservedAddrManager` classifies mapping type (cone vs symmetric) from
observed-port patterns and publishes `EvtNATDeviceTypeChanged` roughly
60 s after it has seen enough peers. **No go-libp2p subsystem consumes
this event today** — see
[Finding 4: Symmetric NAT missing signal](final-report.md#finding-4-symmetric-nat-missing-signal), so your
app has to subscribe itself and branch on the result:

```go
sub, _ := h.EventBus().Subscribe(new(event.EvtNATDeviceTypeChanged))
go func() {
    defer sub.Close()
    for e := range sub.Out() {
        evt := e.(event.EvtNATDeviceTypeChanged)
        switch evt.NatDeviceType {
        case network.NATDeviceTypeEndpointIndependent:
            // Cone NAT — DCUtR can hole-punch through a relay.
        case network.NATDeviceTypeEndpointDependent:
            // Symmetric NAT — hole-punching fails; fall back to IPv6.
        }
    }
}()
```

**Try UPnP / NAT-PMP first, regardless of NAT type.** Enabling
`libp2p.NATPortMap()` at startup asks the router for an inbound port
mapping; AutoNAT v2 then confirms whether it took. When the router
cooperates, the mapped port is directly reachable and the fallbacks
below become unnecessary. Many routers silently drop UPnP requests,
which is why UPnP and AutoNAT are complementary — treat UPnP as
best-effort and let AutoNAT's verdict drive the rest:

```go
libp2p.NATPortMap()
```

**Reserve on a relay (AutoRelay).** When you are NAT'd and UPnP
doesn't produce a reachable address, reserving on a public
circuit-v2 relay lets peers reach you through the relay. All traffic
is proxied — higher latency, but it works for any NAT type, and the
relayed connection is also the prerequisite for DCUtR below:

```go
libp2p.EnableAutoRelayWithStaticRelays(relays)
// or, for dynamic relay discovery:
libp2p.EnableAutoRelayWithPeerSource(peerSourceFn)
```

Options:
- **Default:** AutoRelay activates when v1 says Private — same
  v1-driven oscillation sensitivity the DHT has (see the DHT step
  below).
- **Always-on:** `libp2p.ForceReachabilityPrivate()` + AutoRelay →
  v1 publishes constant Private, reservation happens immediately.
  Good for known-NAT'd nodes that always need the relay.
- **Manual:** skip AutoRelay and use the circuit-v2 client directly
  (`github.com/libp2p/go-libp2p/p2p/protocol/circuitv2/client` →
  `Reserve(ctx, h, relayInfo)` returning a `*Reservation`) when your
  reachability logic is custom.

**Endpoint-independent (cone) + address unreachable → DCUtR on top of
AutoRelay.** Two NAT'd cone peers can upgrade a relayed connection to
a direct one by hole-punching (the
[DCUtR](https://github.com/libp2p/specs/blob/master/relay/DCUtR.md)
protocol). Enable both:

```go
libp2p.EnableHolePunching()
libp2p.EnableAutoRelayWithStaticRelays(relays)
```

When the direct connection succeeds, the relayed one is closed.

**Endpoint-dependent (symmetric) → IPv6 (when reachable).**
Hole-punching will fail symmetric × anything. Include IPv6 listen
addresses so the v6 path bypasses the v4 NAT mapping:

```go
libp2p.ListenAddrStrings(
    "/ip4/0.0.0.0/tcp/4001",
    "/ip6/::/tcp/4001",
    "/ip4/0.0.0.0/udp/4001/quic-v1",
    "/ip6/::/udp/4001/quic-v1",
)
```

IPv6 removes the NAT *mapping* layer (no port translation) but not
the *filtering* layer — stateful v6 firewalls on home routers and
OS-level firewalls are common, and privacy addresses (RFC 4941) or
carrier translation (464XLAT) can also keep v6 unreachable. AutoNAT
v2 tests v4 and v6 separately, so v6 is confirmed only if it is
actually reachable. When ISPs deploy CGNAT they often dual-stack
with permissive v6 firewalls, which is why this works in practice
for many mobile/CGNAT users — it is a probabilistic fallback, not a
guaranteed one.

### 3. Wire DHT (in case you need)

> Skip this step if your application does not use the Kademlia DHT.
>
> When it does, the DHT needs a reachability signal to pick server vs client mode — the options below cover how to feed it.

`go-libp2p-kad-dht` decides server vs client mode by subscribing to
`EvtLocalReachabilityChanged`, which is produced by v1 only. v2's
per-address signal (`EvtHostReachableAddrsChanged`) is not consumed.
Under an unreliable peer pool, v1 can oscillate between
`Public`/`Private`/`Unknown` and flip the DHT mode even when v2 has
confirmed an address is reachable — this is the most impactful
issue documented in the report and worth reading the full analysis
in
[Finding 1](final-report.md#finding-1-inconsistent-global-vs-per-address-reachability-v1-vs-v2)
before choosing a wiring.

**Recommended — publish a v2-derived reachability event.** The DHT
subscribes to whatever is on the event bus; you can write a small
reducer that listens for `EvtHostReachableAddrsChanged`, derives a
global verdict from v2's per-address output, and emits
`EvtLocalReachabilityChanged` itself. `dht.Mode(dht.ModeAuto)` then
consumes your v2-derived signal instead of v1's oscillating one. A
reasonable reduction: emit `Public` if any v2-confirmed address is
reachable, `Private` if all v2 addresses are unreachable, `Unknown`
otherwise. The DHT has no public `SetMode()` method — mode changes
only happen via `ModeAuto` observing the reachability event, which
is why a reducer works.

Other options, ordered by how much they avoid the v1 oscillation:

- **Force Server:** `dht.Mode(dht.ModeServer)` when creating the DHT —
  ignores AutoNAT, always serves. Use when the node is known to be
  reachable (static IP, manual port-forward, confirmed UPnP).
- **Force Client:** `dht.Mode(dht.ModeClient)` — never serves.
  Appropriate for NAT'd peers that won't get a reachable address.
- **ModeAutoServer:** `dht.Mode(dht.ModeAutoServer)` — serves by
  default; only switches to client when reachability is explicitly
  `Private`. Optimistic; still subject to v1's `Private` verdicts
  that may oscillate.
- **Plain ModeAuto without a reducer:** `dht.Mode(dht.ModeAuto)`
  alone — fully v1-driven, vulnerable to oscillation. Use when your
  peer pool is known-reliable or occasional flipping is acceptable.
  
## rust-libp2p

### 1. Enable AutoNAT v2

```rust
#[derive(NetworkBehaviour)]
struct MyBehaviour {
    autonat_v2: libp2p::autonat::v2::client::Behaviour,
    // optional:
    // autonat_v2_server: libp2p::autonat::v2::server::Behaviour,
    // ... your other behaviours
}
```

rust-libp2p's client and server are separate `Behaviour` structs that
you explicitly compose. v1 lives in the older `libp2p::autonat` module
and only runs if you explicitly add its behaviour — it does **not**
run ambiently the way go-libp2p's v1 client does. So v2-only is a
real, clean configuration: just compose v2 behaviours and nothing
else. If you need both (rare), add `libp2p::autonat::Behaviour`
alongside.

### 2. Know the event model

rust-libp2p uses swarm-event composition, not a pub-sub event bus.
AutoNAT v2 results arrive as `autonat::v2::client::Event::Probed
{ address, result }` variants in your `SwarmEvent` handler. **v2-only
is clean** — there is no v1 client running in the background and no
`EvtLocalReachabilityChanged` equivalent to oscillate. You don't need
to compose v1 at all.

### 3. Detect NAT mapping type and pick a fallback

rust-libp2p has **no equivalent of go-libp2p's
`EvtNATDeviceTypeChanged`** — it does not classify endpoint-independent
vs endpoint-dependent mapping. The only signal you have is AutoNAT
v2's per-address result. This means you cannot branch on "cone vs
symmetric" a priori; you enable the fallbacks statically and let the
attempts sort it out.

**Try UPnP / NAT-PMP first.** Separate crate `libp2p-upnp`:

```rust
let upnp = libp2p::upnp::tokio::Behaviour::default();
```

Compose it into your behaviour. When the router cooperates, you get
an inbound port mapping and AutoNAT v2 confirms it.

**Reserve on a relay (manual).** No AutoRelay behaviour — compose
`relay::client::Behaviour` and manage reservations explicitly:

```rust
let relay_multiaddr: Multiaddr = "/ip4/.../p2p/...".parse()?;
swarm.listen_on(relay_multiaddr.with(Protocol::P2pCircuit))?;
```

Your app decides when to reserve. A common pattern: listen for
`FromSwarm::ExternalAddrExpired` and reserve on a chosen relay when
the external address goes away. The relayed connection is also the
prerequisite for DCUtR below.

**DCUtR on top of the relay.** Compose `libp2p::dcutr::Behaviour`
alongside the relay client:

```rust
let dcutr = libp2p::dcutr::Behaviour::new(local_peer_id);
```

When a peer dials you via `/p2p-circuit`, the relayed connection
triggers DCUtR, which attempts to upgrade to direct. Works on
endpoint-independent NAT pairs; fails on symmetric — but without the
NAT-type event you can't predict, so enable it and accept that some
attempts will fail.

**IPv6 listen addresses as a symmetric-NAT fallback.** Include v6
listen addresses so the v6 path bypasses the v4 NAT mapping:

```rust
swarm.listen_on("/ip4/0.0.0.0/tcp/4001".parse()?)?;
swarm.listen_on("/ip6/::/tcp/4001".parse()?)?;
swarm.listen_on("/ip4/0.0.0.0/udp/4001/quic-v1".parse()?)?;
swarm.listen_on("/ip6/::/udp/4001/quic-v1".parse()?)?;
```

Same caveats as in go-libp2p: IPv6 removes NAT mapping but not
firewalls. Confirmed reachability is per-address — AutoNAT v2 tests
v4 and v6 separately.

### 4. Other specifics

- **Startup race:** dialing outbound before the `NewListenAddr` event
  causes TCP port-reuse to silently fall back to an ephemeral port,
  making AutoNAT report UNREACHABLE for TCP. See
  [Finding 5: rust-libp2p TCP port-reuse incorrect metadata](final-report.md#finding-5-rust-libp2p-tcp-port-reuse-incorrect-metadata).
  Wait for `NewListenAddr` before outbound dials.
- **Older versions:** a QUIC false-positive bug in pre-[#4568](https://github.com/libp2p/rust-libp2p/pull/4568)
  builds caused AutoNAT to wrongly report reachable on reused
  connections. Upgrade past that PR.

### 5. Wire DHT (only if your app uses DHT)

> Skip this step if your application does not use the Kademlia DHT.
>
> When it does, the DHT needs a reachability signal to pick server vs client mode — the options below cover how to feed it.
Kademlia (`libp2p::kad::Behaviour`) consumes
`FromSwarm::ExternalAddrConfirmed` and `FromSwarm::ExternalAddrExpired`
from the swarm — events that AutoNAT v2 causes. No v1 intermediary;
DHT mode switches as soon as v2 confirms. Usually no manual wiring
needed. To force mode:

```rust
kademlia.set_mode(Some(kad::Mode::Server));
// or via config:
kad::Config::default().set_mode(kad::Mode::Server)
```
## js-libp2p

### 1. Enable AutoNAT v2

```typescript
import { autoNATv2 } from '@libp2p/autonat-v2'

const node = await createLibp2p({
  services: { autonat: autoNATv2() }
})
```

js-libp2p ships v1 and v2 as separate plugins; register the one(s)
you want — neither runs ambiently. The v2 package is
`@libp2p/autonat-v2`; v1 lives in `@libp2p/autonat` (the older
package, still actively used — Helia registers v1 only today). To
run both:

```typescript
import { autoNATv2 } from '@libp2p/autonat-v2'
import { autoNAT } from '@libp2p/autonat'

const node = await createLibp2p({
  services: {
    autonatV1: autoNAT(),
    autonatV2: autoNATv2(),
  }
})
```

Running both buys little until v2 emits reachability events — see
step 2.

### 2. Know what's missing

`@libp2p/autonat-v2` can be enabled without also running v1 — both
are separate plugins and you choose which to register. But there are
two limitations to know:

- **v2 emits no reachability events apps can subscribe to** today
  ([Finding 4: Symmetric NAT missing signal](final-report.md#finding-4-symmetric-nat-missing-signal)).
  Running v2-only means you have no reactive signal to act on at the
  app level. If you need reactive reachability in js, you are on v1
  today. Helia is v1-only for this reason.
- **Identify unconditionally drops all TCP observed addresses**
  ([js-libp2p#2620](https://github.com/libp2p/js-libp2p/issues/2620)),
  a Node.js platform limitation — Node.js TCP sockets lack
  `SO_REUSEPORT`, so every outbound TCP dial uses an ephemeral port
  and produces many unique observed addresses. To avoid noise,
  `maybeAddObservedAddress()` returns early for any TCP address. The
  consequence: AutoNAT v2 cannot probe TCP addresses in js-libp2p —
  only QUIC addresses reach the v2 candidate list. Prefer QUIC in
  your listen configuration.

### 3. Detect NAT mapping type and pick a fallback

js-libp2p has **no NAT-type detection event** (no go-libp2p-style
`EvtNATDeviceTypeChanged`), and v2 emits no reachability events
either. Dynamic branching by NAT type is not implementable in js-libp2p
today without extending the library — the best you can do is enable
fallbacks statically.

**Try UPnP first (Node.js only).** `@libp2p/upnp-nat` plugin:

```typescript
import { upnpNAT } from '@libp2p/upnp-nat'

services: { upnpNAT: upnpNAT() }
```

Best-effort — the underlying `@achingbrain/nat-port-mapper` library
fails on some router models (we observed "Service not found" on a
Telefónica router in testing).

**Reserve on a relay (AutoRelay).** `@libp2p/auto-relay` plugin,
Node.js runtime only — browsers cannot host listen sockets:

```typescript
import { autoRelay } from '@libp2p/auto-relay'

services: { autoRelay: autoRelay({ bootstrapRelays: [...] }) }
```

**DCUtR on top of the relay.** `@libp2p/dcutr` plugin:

```typescript
import { dcutr } from '@libp2p/dcutr'

services: { dcutr: dcutr() }
```

Paired with `autoRelay`, this gives the same direct-upgrade path as
go-libp2p — works on cone × cone, fails on symmetric.

**IPv6 listen addresses (Node.js only; browsers can't listen).**

```typescript
addresses: {
  listen: [
    '/ip4/0.0.0.0/tcp/4001',
    '/ip6/::/tcp/4001',
    '/ip4/0.0.0.0/udp/4001/quic-v1',
    '/ip6/::/udp/4001/quic-v1',
  ]
}
```

Same v6-reachability caveats as go-libp2p.

### 4. Other specifics

- **v2 emits nothing actionable** today — can't subscribe to AutoNAT
  v2 results from app code.
- **Browser context:** AutoNAT doesn't apply — browsers can't accept
  inbound connections anyway, so UPnP, DCUtR, and IPv6 listeners are
  all Node.js-only.

### 5. Wire DHT (only if your app uses DHT)

> Skip this step if your application does not use the Kademlia DHT.
>
> When it does, the DHT needs a reachability signal to pick server vs client mode — the options below cover how to feed it.
`@libp2p/kad-dht` reacts to `self:peer:update` from the address
manager. In js-libp2p that event is currently driven by v1
confirmation, not v2. Force initial mode via config:

```typescript
kadDHT({ clientMode: true })   // never serve
kadDHT({ clientMode: false })  // always serve
```
