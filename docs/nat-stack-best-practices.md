# libp2p NAT Detection Stack — Best Practices for Application Developers

This document collects considerations and trade-offs for developers
embedding libp2p into an application that needs to work for users
behind NAT. It draws on the findings from this project's
[AutoNAT v2 cross-implementation study](final-report.md), the
[Trautwein et al. DCUtR measurement paper](https://arxiv.org/abs/2604.12484),
and observations from production deployments (notably Obol's Charon
and Avail's avail-light).

It is aimed at **application developers**, not at libp2p protocol
maintainers. The goal is to help you choose sensibly among the
options the stack gives you, not to prescribe a single right answer —
the right choice depends on your application's connectivity profile,
peer population, and operational constraints.

## Table of Contents

1. [Scope: what "the NAT detection stack" means here](#scope)
2. [The pieces and how they compose](#the-pieces-and-how-they-compose)
3. [Decisions that apply regardless of implementation](#decisions-that-apply-regardless-of-implementation)
4. [Per-implementation considerations](#per-implementation-considerations)
    - [go-libp2p](#go-libp2p)
    - [rust-libp2p](#rust-libp2p)
    - [js-libp2p](#js-libp2p)
5. [Common failure patterns from production deployments](#common-failure-patterns-from-production-deployments)
6. [When to skip AutoNAT entirely](#when-to-skip-autonat-entirely)
7. [Further reading](#further-reading)

---

## Scope

The "NAT detection stack" in this document refers to the five
protocols an application typically composes to get libp2p working
through NAT:

- **Identify** — peers exchange their observed addresses so each side
  learns its externally-visible `IP:port`.
- **AutoNAT** (v1 and/or v2) — tests whether those observed addresses
  are actually dialable from outside.
- **UPnP / NAT-PMP** — asks the local router to open a port mapping
  so inbound traffic can reach the node directly.
- **DCUtR** — hole-punching protocol for two NATed peers to establish
  a direct connection through a relay.
- **Circuit Relay v2** — signaling relay used by DCUtR, and a
  last-resort transport when hole-punching fails.

Each protocol solves a narrow problem, and they compose: Identify
supplies candidate addresses; AutoNAT validates them; UPnP sometimes
makes them reachable even when they weren't; DCUtR provides a path
between two NATed peers; Relay falls back when nothing else works.
Most applications need more than one of these, and the interesting
design decisions are in how they interact.

This document does **not** cover how to implement any of these
protocols. The specs linked from [libp2p/specs](https://github.com/libp2p/specs)
are authoritative. It also does not cover TURN, ICE, or WebRTC —
those are separate stacks with their own trade-offs.

---

## The pieces and how they compose

A rough dataflow for an app that wants to be reachable and to reach
other NATed peers:

```
        ┌─────────────┐
        │   Identify  │  exchanges observed addrs between peers
        └──────┬──────┘
               │  observed addrs
               ▼
        ┌─────────────┐
        │   AutoNAT   │  validates: are those addrs reachable?
        │   (v1, v2)  │  outputs: per-address reachability + global
        └──────┬──────┘    reachability flag (differ per impl)
               │
               ▼
     ┌─────────────────┐
     │ reachability    │  your app reads this to decide:
     │ state consumer  │  - advertise as DHT server?
     │ (DHT, relay,    │  - reserve relay slots?
     │  address mgr)   │  - bother trying to hole-punch?
     └─────────────────┘

  Optional, in parallel:

  ┌──────────────┐  ┌────────────────┐  ┌────────────────────┐
  │  UPnP / PMP  │  │   DCUtR        │  │ Circuit Relay v2   │
  │ port mapping │  │ hole-punching  │  │ fallback / signal  │
  └──────────────┘  └────────────────┘  └────────────────────┘
```

Each piece has a different cost and guarantee:

| Protocol | Needs | Gives you | Costs |
|---|---|---|---|
| Identify | Any connection | Observed addrs | Free (runs by default) |
| AutoNAT v1 | A majority of responsive public peers | Global reachability verdict | Stream per probe; sensitive to peer quality |
| AutoNAT v2 | Responsive public peers supporting v2 | Per-address reachability + nonce verification | Similar to v1 but more streams per probe |
| UPnP / PMP | A supporting router | Direct reachability without AutoNAT's help | Optional; often unreliable in the wild |
| DCUtR | Both peers support it + a reachable relay | Direct peer connection through NATs | ~500 bytes of signaling + a few RTTs |
| Circuit Relay v2 | A public relay | Bidirectional signaling path | Relay CPU + bandwidth (low for signaling) |

What AutoNAT by itself cannot do: make you reachable. It only
reports whether you are. If the answer is "no", your path to
reachability is UPnP, a manual port forward, IPv6, DCUtR, or giving
up and relying on relay.

---

## Decisions that apply regardless of implementation

These are choices your application has to make whether you're on
go-, rust-, or js-libp2p. Each has a trade-off rather than a clear
best answer.

### Which AutoNAT protocol(s) to enable

Three positions, all reasonable:

- **v2 only.** Produces per-address reachability, handles nonce
  verification, less sensitive to peer pool quality. The main cost:
  in rust- and js-libp2p today, v2 has less production-validated
  tooling around it, and in js-libp2p the client exists but doesn't
  emit reachability events your app can consume. Works well in
  go-libp2p, where `EnableAutoNATv2()` is a first-class option.
- **v1 only.** Simpler, older, battle-tested. The cost: global
  verdict only, sensitive to unreliable peers in the public pool
  (60% oscillation in controlled testbed runs with unreliable
  servers). If your peer population is known-good, this may be fine.
- **Both, with a reduction rule.** The Trautwein paper runs both.
  Necessary to handle peers that only speak one or the other, but
  forces you to decide what to do when they disagree. No spec
  defines this reduction; in go-libp2p today DHT listens to v1 only
  regardless of v2's per-address output — see finding 1 in the
  [report](final-report.md#finding-1) for why this matters.

If your application is new and targets go-libp2p, starting with v2
only is often the simplest choice. If you also need to interop with
older peers, you'll end up running both and must design the
reduction.

### How to consume the reachability signal

AutoNAT emits both a global flag (`Public`/`Private`/`Unknown`) and,
in v2, per-address information. Applications typically consume one,
not both, and the choice shapes downstream behaviour.

- **Global flag** — simpler, one state per host. Matches what DHTs
  and AutoRelay subscribe to by default in go-libp2p. Oscillates more
  readily (in go-libp2p, because v1 feeds it).
- **Per-address** — richer: some addrs reachable, others not. Useful
  when your node listens on both TCP and QUIC and wants to know
  which is actually working. The cost: your app has to implement its
  own reduction if it wants a single "am I reachable?" answer.

When your app subscribes to a flag, decide explicitly how long an
`Unknown` state should be treated as `Private`. Some apps treat
`Unknown` as `Public` optimistically (advertise the address anyway);
others treat it as `Private` and wait. The paper's Nebula analysis
found DHT-mode flipping in 2–12% of live Kubo nodes per version —
most of it legitimate disconnection churn, but some of it is the
protocol bouncing between states on unreliable peer pools.

### UPnP / NAT-PMP: belt-and-suspenders or noise?

UPnP and PMP are **port-mapping** protocols; AutoNAT is a
**reachability-validation** protocol. They're complementary.
However, in practice:

- Consumer router UPnP support varies from rock-solid to broken.
- Some routers advertise UPnP but silently drop the inbound traffic.
- Corporate networks usually disable it.
- The `libp2p` port mappers are advisory — they don't guarantee the
  mapping is actually live.

A reasonable pattern is to attempt port mapping at startup, rely on
AutoNAT to *confirm* the mapping actually works (or doesn't), and
emit both signals to your app so it can prefer the confirmed one. If
AutoNAT says you're reachable whether or not UPnP succeeded, the
mapping is irrelevant either way.

### Planning for the symmetric-NAT floor

Symmetric NATs (different external port per destination) cannot be
hole-punched with today's DCUtR. The paper quantifies this: DCUtR
succeeds ~70% overall but essentially fails when both peers are
behind symmetric NAT. Our finding 4 notes that AutoNAT v2 doesn't
emit an explicit signal identifying symmetric NAT either — the node
simply looks `Private`.

If your application's user population includes a meaningful fraction
of mobile networks or CGNAT deployments (both commonly symmetric),
plan for a relay fallback. The choice is between:

- Using public libp2p relays (low-cost for signaling, higher latency
  for sustained traffic).
- Running your own relays (more predictable, more operational cost).
- Accepting that some users can't host / be reached, and designing
  the app around that (client-only mode, no DHT server, etc.).

### Static external address vs autodetection

If you know your external address at deploy time (cloud server,
manual port forward, static DNS) announcing it directly is more
reliable than letting AutoNAT figure it out. Most libp2p
implementations expose an `AddrsFactory` / `externalAddresses` /
`--external-address` hook.

Avail's avail-light took this route in v1.13.2 after hitting the
QUIC autonat bug — disabled AutoNAT entirely and required operators
to set `--external-address`. The trade-off is that operators have to
understand their network, but the mode is fully deterministic once
configured.

This is often the right call for production infrastructure. For
end-user applications (desktop, mobile), you usually can't rely on
users configuring an address, so AutoNAT-driven discovery is
unavoidable.

### IPv6 dual-stack

The simplest "NAT traversal" is often just having an IPv6 address.
Most ISPs deploying CGNAT also roll out native IPv6 specifically to
offload from the shared v4 pool. If your users' connections have
working v6 and your peers announce v6 addresses, a lot of NAT
traversal difficulty disappears.

AutoNAT v2 tests v4 and v6 addresses separately — the per-address
signal distinguishes them — so your app can prefer v6 when it works.
Worth considering explicitly in address selection logic rather than
leaving it to the defaults.

### Whether to enable DCUtR and AutoRelay

For peer-to-peer applications where users connect to each other
(not just consume DHT content), DCUtR and AutoRelay are usually
worth enabling. The Trautwein paper reports ~70% conditional success
rate across a diverse real-world peer population, which is high
enough to be useful but low enough that you should design around
partial success.

If your application is purely a client of the libp2p public
infrastructure (Kubo gateway, DHT content lookup) and never needs to
be connected to by random peers, DCUtR adds complexity for no
benefit. Disabling it is reasonable.

---

## Per-implementation considerations

The three implementations differ in API shape, event plumbing, and
which parts of the stack have production maturity. The sections
below are considerations, not checklists — specifics vary between
versions.

### go-libp2p

Used by Kubo, Lotus, Boxo, Charon, and most Filecoin tooling.

- **Enabling v2**: `libp2p.EnableAutoNATv2()` attaches both the
  client (sends probes) and the server (responds to them). v1 is
  enabled by default via `libp2p.EnableNATService()`. To run v2
  only, omit the v1 option.
- **Event wiring**: two events matter.
  - `EvtLocalReachabilityChanged` — the **global** flag, written
    from v1 data. This is what DHT and AutoRelay currently subscribe
    to.
  - `EvtHostReachableAddrsChanged` — **per-address** reachability
    from v2. Your application can subscribe to this directly if it
    wants the richer signal.
  - This is finding 1: DHT and AutoRelay ignore v2 today. If your
    application also makes DHT-server-vs-client decisions, you may
    want to bypass the global flag and decide from v2's per-address
    data.
- **UDP black hole detector**: go-libp2p's
  `UDPBlackHoleSuccessCounter` defaults to an aggressive "probing"
  state that blocks QUIC dial-backs on fresh nodes with no prior
  UDP traffic. For servers it's usually fine; for clients doing
  AutoNAT over QUIC, setting `libp2p.UDPBlackHoleSuccessCounter(nil)`
  avoids the issue (finding 2). Don't override this unless you know
  your traffic pattern.
- **Forced reachability**: `libp2p.ForceReachabilityPublic()` and
  `ForceReachabilityPrivate()` skip AutoNAT entirely and fix the
  reachability state. Convenient for servers with static public IPs
  and for NATed nodes that always need a relay — saves the AutoNAT
  traffic and eliminates oscillation.
- **Port mapping**: `libp2p.NATPortMap()` enables UPnP/PMP via the
  `go-nat` library. Advisory; the mapping may fail silently.

### rust-libp2p

Used by Avail (until v1.13.2), iroh, historically Substrate.

- **Enabling v2**: the `libp2p-autonat` crate exposes both v1 and v2
  as separate behaviours. Both can be composed into your behaviour
  struct. Newer applications default to v2.
- **Event consumption**: rust-libp2p uses the behaviour / `ToSwarm`
  model rather than a pub-sub event bus. Reachability changes arrive
  as `autonat::Event` variants you match in your swarm loop. More
  verbose than go's event subscriptions but gives you finer control.
- **TCP port reuse**: rust-libp2p's AutoNAT v2 relies on the TCP
  transport's port-reuse feature to dial-back from the same local
  port the listener uses. If port reuse fails (conflicting
  transports, custom swarm config), AutoNAT doesn't fall back to a
  different local port — it just doesn't work (finding 5). Worth
  checking your transport config if v2 behaves oddly.
- **Historical QUIC bug**: the QUIC connection-reuse false-positive
  bug that affected avail-light was fixed in
  [rust-libp2p#4568](https://github.com/libp2p/rust-libp2p/pull/4568).
  If you're on an older version and see unexplained AutoNAT
  positives on QUIC, check your changelog.
- **UPnP**: separate crate `libp2p-upnp`. Not enabled by default.
- **Avail's choice**: disabling AutoNAT entirely and requiring a
  static `--external-address` was a pragmatic workaround when the
  upstream fix was taking time to ship. Worth considering if your
  operator base is technical and you can document the override.

### js-libp2p

Used by Helia, js-ipfs, browser apps.

- **v2 client exists, events don't (yet)**: the
  `@libp2p/autonat-v2` package implements the client side, but as
  of this writing does not emit host reachability events that apps
  can subscribe to (finding 4). This means apps can't react
  programmatically to v2 verdicts in js-libp2p the way they can in
  go. If your js-libp2p app needs reachability-driven behaviour,
  plan for either (a) AutoNAT v1 (which does emit events), (b)
  manual config, or (c) waiting for the js-libp2p v2 events to
  land upstream.
- **Helia is v1-only today**: worth knowing because it's the most
  prominent js-libp2p application. If you're building on Helia, its
  reachability signal comes from v1, with the oscillation
  characteristics that implies.
- **Browser vs Node.js**: in the browser, libp2p can't listen for
  inbound connections at all (outside WebRTC/WebTransport contexts),
  so AutoNAT is mostly irrelevant — you're always dialing-only. In
  Node.js, the full stack applies.
- **UPnP**: `@libp2p/upnp-nat` plugin for Node; not available in
  the browser.

---

## Common failure patterns from production deployments

The patterns below are observed from this project's research and
from issue trackers of real libp2p deployments. None are bugs per
se — they're gaps between what the protocol guarantees and what an
application needs.

### AutoNAT is necessary, not sufficient (the Obol / peer-to-peer pattern)

Obol's Charon operators reported "NAT connectivity issues" that on
investigation were not AutoNAT bugs — AutoNAT was correctly
reporting `Private`. The issue was that Charon also needed
peer-to-peer connectivity between validator nodes, and without DCUtR
and relay infrastructure in place, knowing you're private doesn't
help you connect.

The takeaway: if your app needs peers to be able to reach each
other, AutoNAT tells you the problem but doesn't solve it. You need
DCUtR + AutoRelay + public relays (or your own) to get past the
floor that AutoNAT identifies.

### Upstream-bug-to-opt-out path (the Avail pattern)

Avail's avail-light hit a QUIC-related AutoNAT false-positive, the
upstream fix took months to land, and the Avail team eventually
disabled AutoNAT entirely and shifted to static `--external-address`
configuration. Short-term pragmatic; long-term, operators now had to
understand networking to run a node.

If your application depends on a protocol that's still maturing
(AutoNAT v2 in rust and js falls into this category), consider:

- Make the protocol easy to disable by config without redeploys.
- Provide a well-documented manual override that operators can reach
  for if they need to.
- Watch the upstream changelog; plan for the possibility that a fix
  to your problem shows up in a point release you weren't tracking.

### The unreliable-peer-pool oscillation (the go DHT pattern)

Nebula analysis of the Kubo-based DHT found that 2–12% of live nodes
per version flip DHT mode (server ↔ client) over the crawl window.
Most of this is legitimate — nodes restart, disconnect, come back
with different reachability. But some is genuine oscillation: v1's
majority vote over an unreliable peer pool disagrees with v2's more
stable per-address signal, and v1 wins because DHT subscribes to it.

If your app runs a DHT and exposes reachability-driven decisions
(server mode, relay reservation, content announcement), the
ground-truth you might assume from `EvtLocalReachabilityChanged`
has some noise in it. Debouncing or consuming v2 directly are both
reasonable responses.

### Client-mode users who should have been server-mode

The reverse failure: a node that *is* reachable but AutoNAT happens
to disagree (transient server outage, flaky probe path) and so the
app treats it as client-only, refusing to accept DHT queries it
could have served. Aggregated, this reduces the DHT's server pool.
Nebula's 2–12% flipping quantifies this in the wild.

The mitigations here overlap with the oscillation pattern above:
avoid reacting to single `Private` verdicts, use v2's per-address
info as a sanity check, or let operators override.

---

## When to skip AutoNAT entirely

AutoNAT earns its complexity by detecting reachability when you
don't know it. When you do know it, skipping AutoNAT is often
cleaner.

Cases where the stack works better without AutoNAT:

- **Static public IP servers**: cloud VMs, bare metal with known
  addresses, nodes behind a DMZ. Announce the public address
  directly (`AddrsFactory` / `external-address`) and force public
  reachability.
- **Always-relayed nodes**: if the node will always dial out via a
  relay and never try to be directly reachable, AutoNAT adds probing
  traffic and the possibility of a rare `Public` verdict that
  disturbs the app's assumptions. Force private reachability.
- **Test / CI environments**: deterministic reachability state is
  usually preferable to live probing; force it.
- **Container orchestrators with explicit port mapping** (Kubernetes,
  fly.io, Nomad): the orchestrator already knows the public endpoint;
  AutoNAT's discovery is redundant.

The cost of skipping is that if the network changes underneath you
(IP migration, failover, static config drift), your node won't
adapt. For static infrastructure this is usually fine. For mobile
clients or residential users it usually isn't.

---

## Further reading

- [Final report](final-report.md) — the five findings referenced
  throughout this document and the evidence for each.
- [Cross-implementation comparison](cross-implementation-comparison.md)
  — feature matrix, adoption status, how each finding manifests per
  implementation.
- [AutoNAT v2 spec](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
  — authoritative protocol description.
- [DCUtR spec](https://github.com/libp2p/specs/blob/master/relay/DCUtR.md)
  — hole-punching protocol used with circuit-v2 relays.
- [Circuit Relay v2 spec](https://github.com/libp2p/specs/blob/master/relay/circuit-v2.md)
  — relay protocol DCUtR is built on.
- [Trautwein et al., IMC '26](https://arxiv.org/abs/2604.12484) —
  large-scale DCUtR measurement in IPFS, the best current data on
  what hole-punching actually achieves in the wild.
- [Nebula analysis](nebula-autonat-analysis.md) — DHT-mode flipping
  data from the IPFS Amino DHT, useful as ground truth for the
  oscillation patterns described above.
