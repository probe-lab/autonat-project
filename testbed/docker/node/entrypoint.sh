#!/bin/bash
set -euo pipefail

# Node entrypoint for both server and client containers.
#
# For CLIENT containers behind NAT: sets default gateway to the router
# so all traffic goes through NAT.
#
# For SERVER containers: adds a route to the private network through the
# router, so dial-backs can reach the client (for no-NAT control case).
# For NAT modes, traffic goes through NAT anyway, so the route doesn't
# change behavior.

ROLE="${ROLE:-server}"
GATEWAY="${GATEWAY:-}"
PRIVATE_NET="${PRIVATE_NET:-10.0.1.0/24}"

# Resolve router IP via Docker DNS
ROUTER_IP=$(getent hosts router 2>/dev/null | awk '{print $1}' || true)

if [[ "$GATEWAY" == "auto" ]]; then
    # Client mode: set default gateway to router so all traffic goes through NAT
    if [[ -n "$ROUTER_IP" ]]; then
        echo "Client: setting default gateway to router ($ROUTER_IP)"
        ip route del default 2>/dev/null || true
        ip route add default via "$ROUTER_IP"
    else
        echo "WARNING: Could not resolve router IP. Keeping Docker's default gateway."
    fi
elif [[ -n "$GATEWAY" ]]; then
    echo "Setting default gateway to $GATEWAY"
    ip route del default 2>/dev/null || true
    ip route add default via "$GATEWAY"
else
    # Server mode: add route to private network through router
    # This enables the server's dialerHost to reach the client for dial-backs.
    if [[ -n "$ROUTER_IP" ]]; then
        echo "Server: adding route to private-net ($PRIVATE_NET) via router ($ROUTER_IP)"
        ip route add "$PRIVATE_NET" via "$ROUTER_IP" 2>/dev/null || echo "Note: route already exists or failed"
    fi
fi

echo "Route table:"
ip route
echo ""

# Optional: block dial-back to a specific IP (for unreliable server simulation).
# The server can still RECEIVE connections from the client, but cannot initiate
# NEW outbound connections to the client's public IP — causing v1 and v2 dial-back
# failures. This simulates a DHT peer behind its own restrictive NAT.
BLOCK_DIALBACK_TO="${BLOCK_DIALBACK_TO:-}"
if [[ -n "$BLOCK_DIALBACK_TO" ]]; then
    echo "Unreliable server: blocking dial-back to $BLOCK_DIALBACK_TO (port 4001 TCP+UDP)"
    iptables -A OUTPUT -d "$BLOCK_DIALBACK_TO" -p tcp --dport 4001 -m conntrack --ctstate NEW -j DROP
    iptables -A OUTPUT -d "$BLOCK_DIALBACK_TO" -p udp --dport 4001 -m conntrack --ctstate NEW -j DROP
    echo "  iptables rules added — dial-back will fail"
fi

echo "Starting autonat-node (role=${ROLE})..."
exec autonat-node "$@"
