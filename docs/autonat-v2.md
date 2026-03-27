# AutoNAT v2 Protocol Walkthrough

A step-by-step guide to the AutoNAT v2 protocol from the **client's
perspective**. Each step explains what the client does, what messages it sends
and receives, and how the outcome varies depending on the client's NAT type.

For go-libp2p implementation details (constants, structs, confidence system,
observed address manager), see
[go-libp2p-autonat-implementation.md](go-libp2p-autonat-implementation.md).

---

## What AutoNAT v2 Does

AutoNAT v2 lets a node determine whether each of its **individual addresses**
is publicly reachable. A node behind a NAT or firewall cannot know this on its
own — it needs an external peer to dial back and confirm.

| Aspect | v1 | v2 |
|--------|----|----|
| Granularity | Tests the node as a whole | Tests individual addresses |
| Verification | Trusts server's report | Nonce-based dial-back proof |
| Cross-IP testing | Forbidden | Allowed (with amplification cost) |
| Protocol IDs | `/libp2p/autonat/1.0.0` | `/libp2p/autonat/2/dial-request` + `/libp2p/autonat/2/dial-back` |

### Comparison with Traditional NAT Traversal (STUN/ICE)

| Step | Traditional (STUN/ICE) | libp2p |
|------|----------------------|--------|
| Discover external address | STUN binding request | Identify protocol (ObservedAddr) |
| Test reachability | STUN from **multiple IPs** (RFC 5780) | AutoNAT from **same IP** |
| Direct connection | ICE candidate exchange | DCUtR via relay |
| Fallback relay | TURN server | Circuit Relay v2 |

The key difference at step 2: STUN tests from multiple IPs, which
distinguishes full-cone from address-restricted. AutoNAT v2 tests from
the same IP the client already contacted, making these indistinguishable
(see Finding 3 in the [final report](final-report.md)).

---

## NAT Types Quick Reference

The protocol outcome depends on the client's NAT type, which has two
independent dimensions: **mapping behavior** (how the NAT assigns external
ports) and **filtering behavior** (which inbound packets the NAT allows).

### Terminology

NAT types are classified using [RFC 4787](https://www.rfc-editor.org/rfc/rfc4787)
terminology:

- **EIM** — Endpoint-Independent Mapping. The NAT assigns the same external
  IP:port regardless of which destination the client connects to.
- **EIF** — Endpoint-Independent Filtering. The NAT allows inbound packets
  from any source IP:port. Combined with EIM, this is a **full cone** NAT.
- **ADF** — Address-Dependent Filtering. The NAT only allows inbound packets
  from IPs the client has previously sent packets to.
  Combined with EIM, this is an **address-restricted cone** NAT.
- **APDF** — Address and Port-Dependent Filtering. The NAT only allows
  inbound packets from exact IP:port pairs the client has previously sent
  packets to. Combined with EIM, this is a **port-restricted cone** NAT.
- **ADPM** — Address and Port-Dependent Mapping. The NAT assigns a different
  external port for each destination. Combined with APDF filtering, this is
  a **symmetric** NAT.

### Summary Table

| NAT Type | Mapping | Filtering | Dial-back outcome |
|----------|---------|-----------|-------------------|
| **No NAT** | N/A | None | Succeeds |
| **Full cone** (EIM + EIF) | Same external port for all destinations | Any source IP:port allowed | Succeeds |
| **Address-restricted** (EIM + ADF) | Same external port for all destinations | Only IPs previously contacted | Succeeds (false positive) |
| **Port-restricted** (EIM + APDF) | Same external port for all destinations | Only IP:port pairs previously contacted | Fails — new source port blocked |
| **Symmetric** (ADPM + APDF) | Different external port per destination | Only IP:port pairs previously contacted | Fails — new source port blocked (but typically never reached; see Step 4) |

---

## Protocol Overview

### Roles

- **Client** — the node that wants to learn whether its addresses are
  publicly reachable. Initiates the protocol.
- **Server** — a peer that performs the reachability test on behalf of the
  client by dialing back to the client's address.

Any libp2p node can act as both client and server simultaneously.

### Protocol IDs and Streams

The protocol uses two separate streams, each identified by its own protocol
ID:

| Stream | Protocol ID | Direction | Purpose |
|--------|-------------|-----------|---------|
| **Dial-request** | `/libp2p/autonat/2/dial-request` | Client → Server (existing connection) | Client sends addresses + nonce; server responds with result |
| **Dial-back** | `/libp2p/autonat/2/dial-back` | Server → Client (new connection) | Server proves it reached the client by echoing the nonce |

The dial-request stream runs over an **existing** connection between client
and server. The dial-back stream runs over a **new** connection that the
server opens to the address being tested, using a different peer ID and
source port.

### Messages

All messages on the dial-request stream are wrapped in a `Message` envelope
(protobuf `oneof`). Messages on the dial-back stream are sent directly.

**Dial-request stream messages:**

| Message | Sender | Purpose |
|---------|--------|---------|
| `DialRequest` | Client | Carries the addresses to test and a random 64-bit nonce |
| `DialDataRequest` | Server | Requests amplification-prevention data from the client (address index + byte count) |
| `DialDataResponse` | Client | Sends data chunks (up to 4096 bytes each) to satisfy the server's request |
| `DialResponse` | Server | Final result: whether the server could reach the address and complete the dial-back |

**Dial-back stream messages:**

| Message | Sender | Purpose |
|---------|--------|---------|
| `DialBack` | Server | Echoes the nonce from the DialRequest, proving the server reached the address |
| `DialBackResponse` | Client | Acknowledges receipt of the nonce |

### Response Statuses

The `DialResponse` carries two status fields:

**`ResponseStatus`** — whether the server processed the request at all:

| Value | Meaning |
|-------|---------|
| `OK` (200) | Request processed; check `dialStatus` for the dial-back outcome |
| `E_REQUEST_REJECTED` (100) | Server refused (rate limit, resource exhaustion) |
| `E_DIAL_REFUSED` (101) | Server found no dialable address in the request |
| `E_INTERNAL_ERROR` (0) | Unexpected server error |

**`DialStatus`** — outcome of the dial-back attempt (only meaningful when
`ResponseStatus` is `OK`):

| Value | Meaning |
|-------|---------|
| `OK` (200) | Server connected to the address and completed the nonce exchange |
| `E_DIAL_ERROR` (100) | Server could not connect to the address (unreachable) |
| `E_DIAL_BACK_ERROR` (101) | Server connected but the stream-level nonce exchange failed |

### Nonce Verification

The nonce is the core anti-spoofing mechanism. Without it, a malicious server
could claim any address is reachable (or unreachable) without actually dialing
it.

1. Client generates a random 64-bit nonce and includes it in the `DialRequest`
2. Server must echo that nonce in a `DialBack` message on the **new**
   dial-back connection
3. Client verifies the nonce matches before accepting the result

The client does **not** verify the peer ID of the dial-back connection — the
server is expected to use a separate identity for dial-backs.

### Amplification Prevention

When the address the server would dial has a different IP than the client's
observed connection IP, the server requires the client to send 30,000–100,000
bytes of data before dialing. This prevents a malicious client from using the
server as an amplification reflector against a victim's IP address.

---

## Step-by-Step Walkthrough

### Step 1: Node Starts and Listens

The client starts listening on its configured transports:

```
Listen addresses:
  /ip4/0.0.0.0/tcp/4001
  /ip4/0.0.0.0/udp/4001/quic-v1
```

At this point, the client only knows its **local** addresses (e.g.,
`/ip4/192.168.1.10/tcp/4001`). It has no knowledge of whether it is behind a
NAT, what its public IP is, or whether any address is reachable.

### Step 2: Connect to Peers

The client connects to peers — bootstrap nodes, DHT peers, or preconfigured
addresses. Each outbound connection through a NAT router creates an external
IP:port mapping.

**What the NAT does:**

| NAT Type | Mapping created |
|----------|-----------------|
| No NAT | No mapping — client's IP is already public |
| EIM NATs (full cone, addr-restricted, port-restricted) | `192.168.1.10:4001` → `203.0.113.50:4001` — same external IP:port for all destinations |
| Symmetric (ADPM) | `192.168.1.10:4001` → `203.0.113.50:54321` to peer A, `203.0.113.50:54322` to peer B, etc. — different external port per destination |

### Step 3: Identify Exchange

On every new connection, the [Identify protocol](https://github.com/libp2p/specs/blob/master/identify/README.md)
runs automatically. Each remote peer reports two things:

1. **Observed address** — the IP:port the remote peer sees for the client
   (i.e., the NAT's external mapping)
2. **Supported protocols** — including whether it supports
   `/libp2p/autonat/2/dial-request`

The client uses these to:
- **Discover AutoNAT servers** — peers supporting the dial-request protocol
  become eligible probing targets
- **Collect observed addresses** — external addresses reported by peers become
  candidates for reachability verification

**What the client learns per NAT type:**

| NAT Type | Observed address from peer A | From peer B | From peer C |
|----------|------------------------------|-------------|-------------|
| No NAT | `/ip4/203.0.113.50/tcp/4001` | Same | Same |
| Full cone | `/ip4/203.0.113.50/tcp/4001` | Same | Same |
| Addr-restricted | `/ip4/203.0.113.50/tcp/4001` | Same | Same |
| Port-restricted | `/ip4/203.0.113.50/tcp/4001` | Same | Same |
| Symmetric | `/ip4/203.0.113.50/tcp/54321` | `/ip4/203.0.113.50/tcp/54322` | `/ip4/203.0.113.50/tcp/54323` |

### Step 4: Observed Address Activation (Gate)

The client does **not** immediately trust a single peer's observation. It
requires multiple independent peers to report the **same** observed address
before "activating" it (adding it to the host's advertised address list and
making it a candidate for AutoNAT v2 probing).

**Activation threshold**: 4 distinct observers must report the same address.
For IPv4, each distinct IP counts as a separate observer. For IPv6, all IPs
within the same `/56` prefix count as one observer.

This is the **critical branching point** between EIM and ADPM NATs:

#### EIM NATs (full cone, address-restricted, port-restricted)

All peers see the same external IP:port (e.g., `/ip4/203.0.113.50/tcp/4001`).
After connecting to 4 peers, the address reaches the activation threshold and
is added to the host's address list.

```
Peer A reports: /ip4/203.0.113.50/tcp/4001  → 1 observation
Peer B reports: /ip4/203.0.113.50/tcp/4001  → 2 observations
Peer C reports: /ip4/203.0.113.50/tcp/4001  → 3 observations
Peer D reports: /ip4/203.0.113.50/tcp/4001  → 4 observations → ACTIVATED
```

**Result**: Address activated. AutoNAT v2 probing proceeds to Step 5.

#### ADPM NAT (symmetric)

Each peer sees a different external port. No single address ever reaches the
activation threshold.

```
Peer A reports: /ip4/203.0.113.50/tcp/54321  → 1 observation
Peer B reports: /ip4/203.0.113.50/tcp/54322  → 1 observation
Peer C reports: /ip4/203.0.113.50/tcp/54323  → 1 observation
Peer D reports: /ip4/203.0.113.50/tcp/54324  → 1 observation
(... no address ever reaches 4 observations)
```

**Result**: No address activates. AutoNAT v2 **never runs**. The client
relies on AutoNAT v1 for a coarse reachability verdict (which correctly
reports "private"). This is the correct outcome — symmetric NAT addresses
are genuinely unreachable since the external port changes per connection and
is unpredictable by a third party.

### Step 5: Send DialRequest

Once a public address is activated, the client initiates a probe:

1. **Select a server** — pick a random connected peer that supports
   `/libp2p/autonat/2/dial-request` and hasn't been used recently (2-minute
   cooldown per server to distribute load)
2. **Generate a nonce** — random 64-bit value for dial-back verification
3. **Open a stream** — on the existing connection to the selected server,
   open a new stream on `/libp2p/autonat/2/dial-request`
4. **Send DialRequest** — include the nonce and the address(es) to test

```
Client → Server:
  DialRequest {
    addrs: ["/ip4/203.0.113.50/tcp/4001", "/ip4/203.0.113.50/udp/4001/quic-v1"],
    nonce: 0x1a2b3c4d5e6f7890
  }
```

Addresses are ordered by descending priority. The server picks the **first**
address it can dial.

**If no eligible server is available** (all throttled or none connected), the
probe fails with "no peers" and is retried later.

### Step 6: Server Processes the Request

The server receives the DialRequest and processes it through several checks.
From the client's perspective, this results in one of three message types
coming back on the stream. Here are the possibilities:

#### 6a. Server rejects the request → `E_REQUEST_REJECTED`

The server has exceeded its rate limit (global: 60 requests/min, per-peer:
12 requests/min, or max concurrent requests from this peer).

```
Server → Client:
  DialResponse {
    status: E_REQUEST_REJECTED  (100)
  }
```

**Client action**: Discard result, try a different server. This does not
count as a success or failure in the confidence system.

#### 6b. Server can't dial any address → `E_DIAL_REFUSED`

The server iterated through all addresses and found none it can dial (e.g.,
all are private addresses, or the server lacks the transport).

```
Server → Client:
  DialResponse {
    status: E_DIAL_REFUSED  (101)
  }
```

**Client action**: Discard result, try a different server. After 5
consecutive refusals for the same address, pause probing for 10 minutes.

#### 6c. Server selects an address and checks amplification

The server found a dialable address. Before dialing back, it checks whether
the address IP matches the client's **observed** connection IP:

- **Same IP** → The client is likely at the address it claims. The server
  proceeds directly to dial-back (Step 8). No additional messages.

- **Different IP** → The client might be requesting a dial to a victim's
  address (amplification attack). The server requires the client to prove
  effort by sending data first (Step 7).

### Step 7: Amplification Prevention (If Required)

When the server requests amplification data, the client receives a
`DialDataRequest`:

```
Server → Client:
  DialDataRequest {
    addrIdx: 0,                    // which address was selected
    numBytes: 57342                // random value in [30,000 – 100,000)
  }
```

The client sends the requested data in 4096-byte chunks:

```
Client → Server:
  DialDataResponse { data: [4096 bytes] }
  DialDataResponse { data: [4096 bytes] }
  ... (until >= numBytes sent)
```

**Client decision**: the client can **refuse** to send dial data for
low-priority addresses by closing the stream. Each address in the request
has a `SendDialData` flag controlling this. Refusing means the probe produces
no result for that address.

After receiving the data, the server waits a random `[0, 3s]` delay (to
prevent coordinated amplification attacks) before proceeding to dial-back.

### Step 8: Server Dials Back

The server attempts to connect to the selected address using a **separate
host** — a dedicated libp2p instance with its own private key, different peer
ID, and a different source port. This is critical: the dial-back comes from
a **new IP:port** that the client's NAT has never seen before.

This is where NAT filtering determines the outcome:

#### No NAT

```
Server dials 203.0.113.50:4001
  → No NAT to block it
  → Connection succeeds
  → Server opens /libp2p/autonat/2/dial-back stream
  → Sends DialBack{nonce}
```

**Result**: Dial-back succeeds.

#### Full Cone NAT (EIM + EIF)

```
NAT rule: Allow ANY source IP:port to reach 203.0.113.50:4001
  → Server's dial-back from new port is allowed through
  → Connection succeeds
  → Server sends DialBack{nonce}
```

**Result**: Dial-back succeeds. Address is genuinely reachable by anyone.

#### Address-Restricted NAT (EIM + ADF)

```
NAT rule: Allow inbound from any port of an IP the client has previously contacted
  → Client already has a connection to server's IP (from Step 2)
  → Server's dial-back from same IP (but different port) is allowed through
  → Connection succeeds
  → Server sends DialBack{nonce}
```

**Result**: Dial-back succeeds. **This is a false positive.** The address
appears reachable, but only because the client already contacted the server.
A completely new peer from an unknown IP would be blocked. AutoNAT v2 cannot
detect this — it would require a completely independent third party (one the
client has never communicated with) to dial.

#### Port-Restricted NAT (EIM + APDF)

```
NAT rule: Allow inbound only from exact IP:port pairs the client has previously contacted
  → Server's dial-back comes from a NEW source port (different from the existing connection)
  → NAT drops the incoming packet — no matching IP:port entry exists
  → Connection times out (10s dial timeout)
```

**Result**: Dial-back fails. Server reports `E_DIAL_ERROR`.

#### Symmetric NAT (ADPM + APDF)

In practice, this step is **never reached** because the address never
activates at Step 4 (each peer sees a different external port, so no address
reaches the activation threshold). However, if it were reached (e.g., with a
lowered activation threshold), the dial-back would fail for two reasons:

1. **Filtering**: symmetric NAT uses APDF filtering (same as port-restricted),
   so the server's dial-back from a new source port is blocked.
2. **Mapping**: even if filtering were relaxed, the external port the server
   is dialing was assigned for a *different* destination — the NAT may not
   even route the packet to the client's internal socket.

```
NAT rule: Different external port per destination + APDF filtering
  → Server dials the external IP:port that was assigned for a different peer
  → NAT has no matching mapping for inbound from this source IP:port
  → Connection times out or is rejected
```

**Result**: Dial-back fails. Server would report `E_DIAL_ERROR`.

### Step 9: Client Receives DialResponse

After the dial-back attempt (successful or not), the server sends a
`DialResponse` on the original dial-request stream:

```
Server → Client:
  DialResponse {
    status:     OK (200),           // or E_REQUEST_REJECTED, E_DIAL_REFUSED, E_INTERNAL_ERROR
    addrIdx:    0,                  // which address was tested (only meaningful when status=OK)
    dialStatus: OK (200)            // or E_DIAL_ERROR, E_DIAL_BACK_ERROR (only meaningful when status=OK)
  }
```

The client interprets the response:

| `status` | `dialStatus` | Meaning | Client interpretation |
|----------|-------------|---------|----------------------|
| `OK` | `OK` | Server connected and completed dial-back | Proceed to Step 10 (verify nonce) |
| `OK` | `E_DIAL_BACK_ERROR` | Server connected but stream exchange failed | **Public** — network-level reachability confirmed |
| `OK` | `E_DIAL_ERROR` | Server could not connect to the address | **Private** — address is unreachable |
| `E_REQUEST_REJECTED` | — | Server rate-limited | No result — try another server |
| `E_DIAL_REFUSED` | — | Server couldn't dial any address | No result — try another server |
| `E_INTERNAL_ERROR` | — | Server internal failure | No result — try another server |

**Why `E_DIAL_BACK_ERROR` = Public**: The server successfully established a
network connection to the address (TCP handshake completed or QUIC connection
opened). The failure was at the protocol level (stream negotiation, nonce
exchange). Network reachability is confirmed regardless of stream-level
issues.

### Step 10: Dial-Back Verification

When `dialStatus` is `OK`, the client expects a **dial-back connection** on
a separate stream. Concurrently with Step 9, the client's dial-back handler
is waiting for an incoming connection:

```
Server → Client (new connection, different peer ID):
  Opens stream: /libp2p/autonat/2/dial-back
  DialBack { nonce: 0x1a2b3c4d5e6f7890 }

Client verifies:
  1. Nonce matches the one sent in DialRequest? → YES
  2. Connection's local address consistent with the tested address? → YES

Client → Server (on dial-back stream):
  DialBackResponse { status: OK }
```

**Verification checks:**

1. **Nonce match** — The nonce on the dial-back stream must match the one
   sent in the DialRequest. This proves the server actually dialed the
   client (not some other peer relaying the nonce).

2. **Address consistency** — The local address of the dial-back connection
   must match the address being tested. This accounts for DNS→IP resolution,
   transport normalization (`/wss` → `/tls/ws`), and protocol component
   stripping (`/p2p/...`, `/certhash/...`).

The client does **NOT** verify the peer ID of the dial-back connection — the
server is explicitly expected to use a different peer ID (its dedicated dialer
host).

**If verification fails** (wrong nonce or address mismatch), the dial-back
is rejected and the probe counts as if no dial-back was received (private).

**If no dial-back arrives** within 5 seconds of receiving the DialResponse,
the probe times out. Combined with a `dialStatus: OK` from the server, this
is ambiguous — the server claims it connected, but the client never saw it.

### Step 11: Confidence Accumulation

A single probe is not enough. The client repeats the probe with **different
servers** and accumulates results. Each probe outcome is either a success
(public) or failure (private). Rejected and refused results are discarded.

The spec recommends:

> Consider an address reachable if more than 3 servers report a successful
> dial and unreachable if more than 3 servers report unsuccessful dials.
> Implementations are free to use different heuristics.

**Example under no NAT / full cone** (address is reachable):

| Probe | Server | Result | Successes | Failures | Status |
|-------|--------|--------|-----------|----------|--------|
| 1 | Server A | Public | 1 | 0 | Unknown |
| 2 | Server B | Public | 2 | 0 | Unknown |
| 3 | Server C | Public | 3 | 0 | **Public** |

**Example under port-restricted NAT** (address is unreachable):

| Probe | Server | Result | Successes | Failures | Status |
|-------|--------|--------|-----------|----------|--------|
| 1 | Server A | Private | 0 | 1 | Unknown |
| 2 | Server B | Private | 0 | 2 | Unknown |
| 3 | Server C | Private | 0 | 3 | **Private** |

**Example under address-restricted NAT** (false positive):

| Probe | Server | Result | Successes | Failures | Status |
|-------|--------|--------|-----------|----------|--------|
| 1 | Server A | Public | 1 | 0 | Unknown |
| 2 | Server B | Public | 2 | 0 | Unknown |
| 3 | Server C | Public | 3 | 0 | **Public** (false positive) |

The false positive occurs because the client already has connections to the
servers, and address-restricted NAT allows any port from a known IP.

### Step 12: Reachability Event

Once confidence is reached, the client determines its overall reachability:

- **At least one address is Public** → Node is reachable. Stop using relays,
  advertise direct addresses to the network.
- **All addresses are Private** → Node is behind NAT. Connect to relay
  servers, advertise relay addresses instead.

The client should **re-probe periodically** to detect network changes (e.g.,
NAT type change, IP change, firewall rule change). The spec does not
prescribe specific intervals — see
[go-libp2p implementation](go-libp2p-autonat-implementation.md#confidence-system)
for the concrete re-probe schedule and sliding window mechanics used in
go-libp2p.

---

## Complete Protocol Diagrams

### Happy Path (Same IP, No Amplification)

```
Client                                          Server
  |                                                |
  |  [1] Open stream: /libp2p/autonat/2/dial-request
  |---- DialRequest{nonce, addrs[]} -------------->|
  |                                                |
  |        Server selects first dialable addr (i)  |
  |        addr[i] IP == client's observed IP      |
  |        → skip amplification prevention         |
  |                                                |
  |  [2] Server opens NEW connection to addr[i]    |
  |      using separate host (different peer ID,   |
  |      different source port)                    |
  |                                                |
  |<--- [new conn] /libp2p/autonat/2/dial-back ----|
  |     DialBack{nonce} ---------------------------|
  |                                                |
  |  [3] Client verifies nonce + address           |
  |     DialBackResponse{OK} --------------------->|
  |     (client closes dial-back stream)           |
  |                                                |
  |  [4] Server reports result on original stream  |
  |<--- DialResponse{OK, addrIdx=i, dialStatus=OK} |
  |     (server closes dial-request stream)        |
```

### With Amplification Prevention (Different IP)

```
Client                                          Server
  |                                                |
  |---- DialRequest{nonce, addrs[]} -------------->|
  |                                                |
  |        addr[i] IP != client's observed IP      |
  |        → require dial data first               |
  |                                                |
  |<--- DialDataRequest{addrIdx=i, numBytes=N} ----|
  |                                                |
  |---- DialDataResponse{data: 4096B} ------------>|
  |---- DialDataResponse{data: 4096B} ------------>|
  |---- ... (until >= N bytes sent) -------------->|
  |                                                |
  |        Server waits random [0, 3s]             |
  |        Server dials addr[i]                    |
  |                                                |
  |<--- [new conn] DialBack{nonce} ----------------|
  |---- DialBackResponse{OK} --------------------->|
  |                                                |
  |<--- DialResponse{OK, addrIdx=i, dialStatus=OK} |
```

### Dial-Back Blocked by NAT

```
Client                                          Server
  |                                                |
  |---- DialRequest{nonce, addrs[]} -------------->|
  |                                                |
  |        Server dials addr[i]...                 |
  |        NAT drops incoming packet               |
  |        Connection times out (10s)              |
  |                                                |
  |<--- DialResponse{OK, addrIdx=i,               |
  |      dialStatus=E_DIAL_ERROR} -----------------|
  |                                                |
  |  Client records: PRIVATE                       |
```

### Server Rejects or Refuses

```
Client                                          Server
  |                                                |
  |---- DialRequest{nonce, addrs[]} -------------->|
  |                                                |
  |  Case A: Rate limited                          |
  |<--- DialResponse{E_REQUEST_REJECTED} ----------|
  |                                                |
  |  Case B: No dialable address                   |
  |<--- DialResponse{E_DIAL_REFUSED} --------------|
  |                                                |
  |  Client: discard, try another server           |
```

---

## End-to-End Outcomes by NAT Type

### No NAT

```
Step 1-3:  Peers report client's real public IP
Step 4:    Address activates after 4 peers confirm it
Step 5-8:  Server dials back successfully (no NAT to block)
Step 9-10: DialResponse OK + nonce verified → Public
Step 11:   3 successes → Public
Step 12:   Address is Public ✓ (correct)
```

### Full Cone NAT (EIM + EIF)

```
Step 1-3:  All peers see same external IP:port (EIM)
Step 4:    Address activates after 4 peers confirm it
Step 5-8:  NAT allows any inbound (EIF) → dial-back succeeds
Step 9-10: DialResponse OK + nonce verified → Public
Step 11:   3 successes → Public
Step 12:   Address is Public ✓ (correct — genuinely reachable by anyone)
```

### Address-Restricted NAT (EIM + ADF)

```
Step 1-3:  All peers see same external IP:port (EIM)
Step 4:    Address activates after 4 peers confirm it
Step 5-8:  NAT allows server's IP (already contacted, ADF) → dial-back succeeds
Step 9-10: DialResponse OK + nonce verified → Public
Step 11:   3 successes → Public
Step 12:   Address is Public ⚠ (FALSE POSITIVE — only reachable from known IPs)
```

**Why this is a false positive**: ADF filtering allows inbound from any port
of an IP the client has previously contacted. Since the client already has a
connection to the server's IP, the dial-back from a different port passes the
filter. A brand new peer from an unknown IP would be blocked. AutoNAT v2
cannot detect this — it would require a completely independent third party
(one the client has never communicated with) to perform the dial-back.

### Port-Restricted NAT (EIM + APDF)

```
Step 1-3:  All peers see same external IP:port (EIM)
Step 4:    Address activates after 4 peers confirm it
Step 5-8:  NAT requires exact IP:port match (APDF) → dial-back from new port blocked
Step 9-10: DialResponse OK + dialStatus E_DIAL_ERROR → Private
Step 11:   3 failures → Private
Step 12:   Address is Private ✓ (correct — unreachable from new source ports)
```

### Symmetric NAT (ADPM + APDF)

```
Step 1-3:  Each peer sees different external port (ADPM)
Step 4:    No address reaches activation threshold (4 observations)
           → Address never activates → AutoNAT v2 never runs for this address
```

If the address *were* to be probed (e.g., with a lowered activation
threshold):

```
Step 5-8:  NAT blocks dial-back (APDF filtering + wrong mapping)
Step 9-10: DialResponse OK + dialStatus E_DIAL_ERROR → Private
Step 11:   3 failures → Private
Step 12:   Address is Private ✓ (correct)
```

In default go-libp2p, the client falls back to AutoNAT v1 which provides a
coarse "private" verdict. The outcome is correct either way — symmetric NAT
addresses are genuinely unreachable since the external port is unpredictable.

---

## Protobuf Wire Format

All messages on the dial-request stream are wrapped in a `Message` envelope:

```protobuf
message Message {
    oneof msg {
        DialRequest      dialRequest      = 1;
        DialResponse     dialResponse     = 2;
        DialDataRequest  dialDataRequest  = 3;
        DialDataResponse dialDataResponse = 4;
    }
}
```

Messages on the dial-back stream are sent directly (no wrapper).

### DialRequest

```protobuf
message DialRequest {
    repeated bytes addrs = 1;  // multiaddrs, priority descending
    fixed64 nonce = 2;         // random 64-bit verification token
}
```

### DialDataRequest

```protobuf
message DialDataRequest {
    uint32 addrIdx  = 1;  // zero-based index of selected address
    uint64 numBytes = 2;  // bytes the client must send [30,000 – 100,000)
}
```

### DialDataResponse

```protobuf
message DialDataResponse {
    bytes data = 1;  // max 4096 bytes per message, min 100 bytes
}
```

### DialBack (on dial-back stream)

```protobuf
message DialBack {
    fixed64 nonce = 1;
}
```

### DialBackResponse (on dial-back stream)

```protobuf
message DialBackResponse {
    enum DialBackStatus { OK = 0; }
    DialBackStatus status = 1;
}
```

### DialResponse

```protobuf
message DialResponse {
    ResponseStatus status = 1;
    uint32 addrIdx        = 2;
    DialStatus dialStatus = 3;
}

enum ResponseStatus {
    E_INTERNAL_ERROR   = 0;
    E_REQUEST_REJECTED = 100;
    E_DIAL_REFUSED     = 101;
    OK                 = 200;
}

enum DialStatus {
    UNUSED            = 0;
    E_DIAL_ERROR      = 100;
    E_DIAL_BACK_ERROR = 101;
    OK                = 200;
}
```

`addrIdx` and `dialStatus` are only meaningful when `status == OK`.

---

## Timeouts

| Timeout | Value | Context |
|---------|-------|---------|
| Dial-request stream deadline | 15s | Entire request-response exchange |
| Dial-back stream deadline | 5s | Client waiting for nonce on dial-back stream |
| Dial-back dial timeout | 10s | Server's connection attempt to client |
| Amplification delay | 0-3s (random) | Server waits after receiving dial data |
| Dial-back confirmation read | 5s | Server waits for client to ACK nonce |

---

## References

- [AutoNAT v2 Specification](https://github.com/libp2p/specs/blob/master/autonat/autonat-v2.md)
- [Identify Specification](https://github.com/libp2p/specs/blob/master/identify/README.md)
- [go-libp2p AutoNAT v2 Implementation Details](go-libp2p-autonat-implementation.md)
- [NAT Type Classification](https://www.rfc-editor.org/rfc/rfc4787)
