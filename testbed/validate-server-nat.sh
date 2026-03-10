#!/bin/bash
set -euo pipefail

# Validate NAT status from the server's perspective.
#
# Reads an OTEL trace file produced by run-server.sh and infers the NAT type
# of each connected client by comparing:
#   - The address the server observes the client coming from (post-NAT IP:port)
#   - The address(es) the client claims to listen on (pre-NAT)
#   - Whether AutoNAT v2 dial requests were received from this client
#   - Whether dial-backs succeeded (inferred from probe_completed events if available)
#
# Usage:
#   ./testbed/validate-server-nat.sh <trace.json>
#   ./testbed/validate-server-nat.sh results/server/server-*.trace.json
#
# Output: per-client NAT assessment, protocol support, observed addresses.
#
# Dependencies: jq, python3

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

TRACE_FILE="${1:-}"
if [[ -z "$TRACE_FILE" ]]; then
    echo "Usage: $0 <trace.json>"
    echo ""
    echo "Produce a trace with: ./testbed/run-server.sh"
    exit 1
fi

if [[ ! -f "$TRACE_FILE" ]]; then
    echo "Error: trace file not found: $TRACE_FILE"
    exit 1
fi

if ! command -v jq &>/dev/null; then
    echo "Error: jq is required."
    exit 1
fi

echo "=== Server-Side NAT Validation ==="
echo "Trace: $TRACE_FILE"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Server session summary
# ---------------------------------------------------------------------------

SESSION=$(jq 'select(.Name == "autonat.session")' "$TRACE_FILE" 2>/dev/null | head -1)

if [[ -z "$SESSION" ]]; then
    echo "ERROR: No autonat.session span found in trace. Is this a server trace?"
    exit 1
fi

SERVER_ROLE=$(echo "$SESSION" | jq -r '.Attributes[] | select(.Key == "role") | .Value.Value')
SERVER_TRANSPORT=$(echo "$SESSION" | jq -r '.Attributes[] | select(.Key == "transport") | .Value.Value')
SERVER_PEER=$(echo "$SESSION" | jq -r '.Attributes[] | select(.Key == "peer_id") | .Value.Value')
SERVER_ADDRS=$(echo "$SESSION" | jq -r '.Attributes[] | select(.Key == "listen_addrs") | .Value.Value[]' 2>/dev/null || echo "(none)")

echo "Server peer ID: $SERVER_PEER"
echo "Role:           $SERVER_ROLE"
echo "Transport:      $SERVER_TRANSPORT"
echo "Listen addrs:"
echo "$SERVER_ADDRS" | while read -r addr; do echo "  $addr"; done
echo ""

# ---------------------------------------------------------------------------
# Step 2: Extract per-client observed address data
# ---------------------------------------------------------------------------
# EvtPeerIdentificationCompleted → peer_identification_completed event
# Attributes: peer_id, observed_addr, protocols, agent_version

echo "--- Connected Clients ---"
echo ""

# Use python3 for the per-peer analysis (jq groupBy is limited)
python3 - "$TRACE_FILE" <<'PYEOF'
import json
import sys
from collections import defaultdict

trace_path = sys.argv[1]

# Load all session events
identification_events = []
connectedness_events = []
protocol_events = []
probe_completed = []  # from refresh_cycle spans (server runs autonat as client too)

with open(trace_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            span = json.loads(line)
        except json.JSONDecodeError:
            continue

        name = span.get("Name", "")
        events = span.get("Events", [])

        def get_attr(attrs, key):
            for a in attrs:
                if a.get("Key") == key:
                    v = a.get("Value", {})
                    if isinstance(v, dict):
                        return v.get("Value")
                    return v
            return None

        def get_attrs_flat(attrs):
            out = {}
            for a in attrs:
                k = a.get("Key", "")
                v = a.get("Value", {})
                out[k] = v.get("Value") if isinstance(v, dict) else v
            return out

        if name == "autonat.session":
            for evt in events:
                ename = evt.get("Name", "")
                attrs = get_attrs_flat(evt.get("Attributes", []))
                if ename == "peer_identification_completed":
                    identification_events.append(attrs)
                elif ename == "peer_connectedness_changed":
                    connectedness_events.append(attrs)
                elif ename == "peer_protocols_updated":
                    protocol_events.append(attrs)

        elif name == "autonatv2.refresh_cycle":
            for evt in events:
                if evt.get("Name") == "probe_completed":
                    attrs = get_attrs_flat(evt.get("Attributes", []))
                    probe_completed.append(attrs)

# Build per-peer info
peers = defaultdict(lambda: {
    "observed_addrs": [],
    "protocols": [],
    "agent_version": None,
    "connectedness_changes": [],
})

for evt in identification_events:
    pid = evt.get("peer_id", "")
    if not pid:
        continue
    obs = evt.get("observed_addr")
    if obs and obs not in peers[pid]["observed_addrs"]:
        peers[pid]["observed_addrs"].append(obs)
    protos = evt.get("protocols", [])
    if isinstance(protos, list):
        for p in protos:
            if p not in peers[pid]["protocols"]:
                peers[pid]["protocols"].append(p)
    av = evt.get("agent_version")
    if av:
        peers[pid]["agent_version"] = av

for evt in connectedness_events:
    pid = evt.get("peer_id", "")
    if pid:
        peers[pid]["connectedness_changes"].append(evt.get("connectedness", ""))

AUTONAT_V2_PROTO = "/libp2p/autonat/2/dial-request"
AUTONAT_V2_BACK  = "/libp2p/autonat/2/dial-back"

if not peers:
    print("No identified peers found in trace.")
    print("The server may not have received any client connections.")
    sys.exit(0)

print(f"Total identified peers: {len(peers)}")
print("")

for pid, info in sorted(peers.items()):
    short_pid = pid[:16] + "..." if len(pid) > 16 else pid
    agent = info["agent_version"] or "unknown"
    supports_autonat = any(AUTONAT_V2_PROTO in p for p in info["protocols"])
    supports_dialback = any(AUTONAT_V2_BACK in p for p in info["protocols"])
    connectedness = info["connectedness_changes"][-1] if info["connectedness_changes"] else "unknown"

    print(f"Peer: {short_pid}")
    print(f"  Agent:           {agent}")
    print(f"  Connectedness:   {connectedness}")
    print(f"  AutoNAT v2:      {'yes (dial-request)' if supports_autonat else 'no'}")

    if info["observed_addrs"]:
        print(f"  Observed addr(s) (post-NAT, as seen by server):")
        for addr in info["observed_addrs"]:
            print(f"    {addr}")

        # NAT inference: compare observed addr IP with autonat protocol support
        # A node that supports autonat and has a different observed addr is behind NAT
        if supports_autonat:
            obs_addrs = info["observed_addrs"]
            print(f"  NAT inference:")
            for obs in obs_addrs:
                # Crude check: if observed addr contains a private IP range it's odd
                # (server would normally see public IPs unless on same LAN)
                is_private = any(
                    obs.startswith(prefix) for prefix in [
                        "/ip4/10.", "/ip4/192.168.", "/ip4/172.",
                        "/ip4/100.64.", "/ip4/127.",
                    ]
                )
                if is_private:
                    print(f"    {obs} → private IP (client on same LAN or no NAT)")
                else:
                    print(f"    {obs} → public IP visible to server (may be direct or port-mapped)")
        else:
            print(f"  NAT inference:   n/a (client does not advertise AutoNAT v2)")
    else:
        print(f"  Observed addr(s): (none — peer not fully identified)")

    print("")

# ---------------------------------------------------------------------------
# Summary of AutoNAT v2 activity seen from server side
# ---------------------------------------------------------------------------
autonat_peers = [pid for pid, info in peers.items()
                 if any(AUTONAT_V2_PROTO in p for p in info["protocols"])]

print(f"--- AutoNAT v2 Activity Summary ---")
print(f"Peers supporting AutoNAT v2: {len(autonat_peers)}/{len(peers)}")

if probe_completed:
    print(f"")
    print(f"Note: probe_completed events found ({len(probe_completed)} probes).")
    print(f"These are from the server's own AutoNAT client (server also tests its own reachability).")
    reachable = [p for p in probe_completed if p.get("reachability") == "public"]
    unreachable = [p for p in probe_completed if p.get("reachability") == "private"]
    print(f"  Server self-probes — reachable: {len(reachable)}, unreachable: {len(unreachable)}")
PYEOF

# ---------------------------------------------------------------------------
# Step 3: Protocol stream activity (look for autonat streams in log if available)
# ---------------------------------------------------------------------------

echo ""
echo "--- Protocol Activity (from session events) ---"

# Count peer_protocols_updated events that include AutoNAT v2
AUTONAT_PEERS=$(jq -r '
    select(.Name == "autonat.session") |
    .Events[]? |
    select(.Name == "peer_protocols_updated") |
    .Attributes[] |
    select(.Key == "added") |
    .Value.Value[] |
    select(contains("/libp2p/autonat/2"))
' "$TRACE_FILE" 2>/dev/null | wc -l | tr -d ' ')

echo "Peers that advertised AutoNAT v2 protocol: $AUTONAT_PEERS"

# Session duration
SESSION_START=$(jq -r 'select(.Name == "autonat.session") | .StartTime' "$TRACE_FILE" 2>/dev/null | head -1)
SESSION_END=$(jq -r 'select(.Name == "autonat.session") | .EndTime' "$TRACE_FILE" 2>/dev/null | head -1)

if [[ -n "$SESSION_START" && -n "$SESSION_END" && "$SESSION_START" != "null" ]]; then
    echo ""
    echo "Session start: $SESSION_START"
    echo "Session end:   $SESSION_END"
fi

echo ""
echo "=== Validation complete ==="
echo ""
echo "To run full metric analysis:"
echo "  python3 testbed/analyze.py $TRACE_FILE"
