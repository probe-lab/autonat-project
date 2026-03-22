#!/bin/bash
set -euo pipefail

# NAT Router Entrypoint
#
# Configures iptables NAT rules based on $NAT_TYPE environment variable.
# The router has two interfaces:
#   - Public side ($PUB_IFACE): connected to public-net
#   - Private side ($PRIV_IFACE): connected to private-net
#
# See docs/testbed.md for detailed explanation of each NAT type.

NAT_TYPE="${NAT_TYPE:-none}"

# Network degradation options (for tc netem experiments)
PACKET_LOSS="${PACKET_LOSS:-0}"
LATENCY_MS="${LATENCY_MS:-10}"

# Selective TCP port blocking (e.g., TCP_BLOCK_PORT=4001 blocks outbound TCP to port 4001)
TCP_BLOCK_PORT="${TCP_BLOCK_PORT:-}"

# Port remapping (e.g., PORT_REMAP=4001:29538 remaps source port 4001→29538)
# Simulates NATs that use EIM but don't preserve the listen port (hotel WiFi behavior).
PORT_REMAP="${PORT_REMAP:-}"

# Static port forwarding (PORT_FORWARD=true)
# Adds DNAT rules to expose the client's listen port through the router's public IP.
# Most useful combined with port-restricted or symmetric NAT.
PORT_FORWARD="${PORT_FORWARD:-}"
PORT_FORWARD_PORT="${PORT_FORWARD_PORT:-4001}"

# UPnP/NAT-PMP emulation (UPNP=true)
# Runs miniupnpd on the private-net interface so the client can request
# port mappings dynamically via libp2p's NATPortMap() (go-nat library).
UPNP="${UPNP:-}"

echo "=== NAT Router ==="
echo "NAT type:          $NAT_TYPE"
echo "Packet loss:       ${PACKET_LOSS}%"
echo "Latency:           ${LATENCY_MS}ms"
echo "TCP block port:    ${TCP_BLOCK_PORT:-none}"
echo "Port remap:        ${PORT_REMAP:-none}"
echo "Port forward:      ${PORT_FORWARD:-disabled} (port=${PORT_FORWARD_PORT})"
echo "UPnP:              ${UPNP:-disabled}"

# Detect interface names and IPs dynamically.
# The router is connected to two Docker networks:
#   public-net  (73.0.0.0/24)  — servers, "internet" side
#   private-net (10.0.1.0/24)  — client, behind NAT
PUB_IFACE=""
PRIV_IFACE=""
ROUTER_PUBLIC_IP=""
ROUTER_PRIVATE_IP=""

for iface in $(ls /sys/class/net/ | grep -v lo); do
    ip_addr=$(ip -4 addr show "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' || true)
    if [[ "$ip_addr" == 73.0.0.* ]]; then
        PUB_IFACE="$iface"
        ROUTER_PUBLIC_IP="$ip_addr"
        echo "Public interface:  $PUB_IFACE ($ip_addr)"
    elif [[ "$ip_addr" == 10.0.1.* ]]; then
        PRIV_IFACE="$iface"
        ROUTER_PRIVATE_IP="$ip_addr"
        echo "Private interface: $PRIV_IFACE ($ip_addr)"
    fi
done

echo "Router public IP:  $ROUTER_PUBLIC_IP"
echo "Router private IP: $ROUTER_PRIVATE_IP"

if [[ -z "$PUB_IFACE" || -z "$PRIV_IFACE" ]]; then
    echo "ERROR: Could not detect both interfaces. Found PUB=$PUB_IFACE PRIV=$PRIV_IFACE"
    echo "Available interfaces:"
    ip addr
    exit 1
fi

# Enable IP forwarding (may fail in Docker Desktop — the sysctls directive
# in docker-compose.yml handles this at container creation time)
sysctl -w net.ipv4.ip_forward=1 2>/dev/null || echo "Note: sysctl failed (expected in Docker Desktop, forwarding set via compose sysctls)"

# Flush existing rules
iptables -F
iptables -t nat -F
iptables -F FORWARD

echo "Configuring NAT type: $NAT_TYPE"

case "$NAT_TYPE" in
    none)
        # No NAT — pure routing. Client is directly reachable.
        echo "No NAT rules. Direct forwarding enabled."
        iptables -A FORWARD -j ACCEPT
        ;;

    full-cone)
        # Full Cone NAT (EIM + EIF)
        # Static SNAT for outbound, static DNAT for all inbound.
        # Any external host can reach the client through the mapped address.
        if [[ -z "${CLIENT_PRIVATE_IP:-}" ]]; then
            echo "Full Cone: waiting for client to appear on private-net..."
            CLIENT_PRIVATE_IP=""
            for i in $(seq 1 30); do
                ping -c1 -W1 -b 10.0.1.255 2>/dev/null || true
                CLIENT_PRIVATE_IP=$(ip neigh show dev "$PRIV_IFACE" 2>/dev/null | grep -oE '10\.0\.1\.[0-9]+' | head -1 || true)
                if [[ -n "$CLIENT_PRIVATE_IP" ]]; then
                    break
                fi
                sleep 1
            done
            if [[ -z "$CLIENT_PRIVATE_IP" ]]; then
                echo "WARNING: Could not detect client IP. Using .10 as fallback."
                CLIENT_PRIVATE_IP="10.0.1.10"
            fi
        fi
        echo "Full Cone: SNAT + DNAT (client=$CLIENT_PRIVATE_IP)"
        iptables -t nat -A POSTROUTING -o "$PUB_IFACE" -j SNAT --to-source "$ROUTER_PUBLIC_IP"
        iptables -t nat -A PREROUTING -i "$PUB_IFACE" -j DNAT --to-destination "$CLIENT_PRIVATE_IP"
        iptables -A FORWARD -j ACCEPT
        ;;

    address-restricted)
        # Address-Restricted Cone NAT (EIM + ADF)
        # Inbound allowed from any IP the client previously contacted,
        # on ANY port (not just the original port). This requires:
        # 1. Client discovery (like full-cone) for DNAT target
        # 2. The "recent" module to track contacted IPs
        # 3. DNAT in PREROUTING restricted to contacted IPs only
        #
        # Linux conntrack is naturally port-restricted, so we can't rely
        # on RELATED,ESTABLISHED alone — we need explicit DNAT for new
        # inbound connections from contacted IPs.
        if [[ -z "${CLIENT_PRIVATE_IP:-}" ]]; then
            echo "Address-Restricted: waiting for client to appear on private-net..."
            for i in $(seq 1 30); do
                ping -c1 -W1 -b 10.0.1.255 2>/dev/null || true
                CLIENT_PRIVATE_IP=$(ip neigh show dev "$PRIV_IFACE" 2>/dev/null | grep -oE '10\.0\.1\.[0-9]+' | head -1 || true)
                if [[ -n "$CLIENT_PRIVATE_IP" ]]; then
                    break
                fi
                sleep 1
            done
            if [[ -z "$CLIENT_PRIVATE_IP" ]]; then
                echo "WARNING: Could not detect client IP. Using .10 as fallback."
                CLIENT_PRIVATE_IP="10.0.1.10"
            fi
        fi
        echo "Address-Restricted: MASQUERADE + recent DNAT (client=$CLIENT_PRIVATE_IP)"
        iptables -t nat -A POSTROUTING -o "$PUB_IFACE" -j MASQUERADE
        # Track outbound destinations in FORWARD (populates "contacted" list)
        iptables -A FORWARD -i "$PRIV_IFACE" -o "$PUB_IFACE" -m recent --set --name contacted --rdest -j ACCEPT
        # DNAT inbound from contacted IPs to the client (any port)
        iptables -t nat -A PREROUTING -i "$PUB_IFACE" -m recent --rcheck --seconds 300 --name contacted --rsource -j DNAT --to-destination "$CLIENT_PRIVATE_IP"
        # Allow established connections and DNATted traffic
        iptables -A FORWARD -i "$PUB_IFACE" -o "$PRIV_IFACE" -j ACCEPT
        iptables -A FORWARD -j DROP
        ;;

    port-restricted)
        # Port-Restricted Cone NAT (EIM + APDF)
        # MASQUERADE for outbound. Inbound only from the exact IP:port pair
        # the client contacted. For TCP, conntrack is naturally port-restricted.
        # For UDP, we use strict conntrack matching.
        echo "Port-Restricted: MASQUERADE + strict conntrack (address+port filtering)"
        iptables -t nat -A POSTROUTING -o "$PUB_IFACE" -j MASQUERADE
        iptables -A FORWARD -i "$PUB_IFACE" -o "$PRIV_IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
        iptables -A FORWARD -i "$PRIV_IFACE" -o "$PUB_IFACE" -j ACCEPT
        iptables -A FORWARD -j DROP
        ;;

    symmetric)
        # Symmetric NAT (ADPM + APDF)
        # MASQUERADE with --random forces a different source port for each
        # destination, simulating endpoint-dependent mapping.
        echo "Symmetric: MASQUERADE --random + strict conntrack"
        iptables -t nat -A POSTROUTING -o "$PUB_IFACE" -j MASQUERADE --random
        iptables -A FORWARD -i "$PUB_IFACE" -o "$PRIV_IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
        iptables -A FORWARD -i "$PRIV_IFACE" -o "$PUB_IFACE" -j ACCEPT
        iptables -A FORWARD -j DROP
        ;;

    *)
        echo "ERROR: Unknown NAT type: $NAT_TYPE"
        echo "Valid types: none, full-cone, address-restricted, port-restricted, symmetric"
        exit 1
        ;;
esac

# Block outbound TCP on a specific port if configured.
# This reproduces hotel/corporate WiFi behavior where TCP on non-standard
# ports is blocked but UDP (QUIC) is allowed.
if [[ -n "$TCP_BLOCK_PORT" ]]; then
    echo "Blocking outbound TCP to port $TCP_BLOCK_PORT (FORWARD DROP)"
    iptables -I FORWARD -i "$PRIV_IFACE" -o "$PUB_IFACE" -p tcp --dport "$TCP_BLOCK_PORT" -j DROP
fi

# Remap source port if configured.
# Inserts SNAT rules before MASQUERADE to force a consistent port remapping
# while preserving EIM (same external port for all destinations).
# Format: internal_port:external_port (e.g., 4001:29538)
if [[ -n "$PORT_REMAP" ]]; then
    REMAP_INT="${PORT_REMAP%%:*}"
    REMAP_EXT="${PORT_REMAP##*:}"
    echo "Port remapping: source port $REMAP_INT → $REMAP_EXT (SNAT)"
    # Insert before MASQUERADE so these match first
    iptables -t nat -I POSTROUTING -o "$PUB_IFACE" -p tcp --sport "$REMAP_INT" -j SNAT --to-source "$ROUTER_PUBLIC_IP:$REMAP_EXT"
    iptables -t nat -I POSTROUTING -o "$PUB_IFACE" -p udp --sport "$REMAP_INT" -j SNAT --to-source "$ROUTER_PUBLIC_IP:$REMAP_EXT"
fi

# Static port forwarding: expose CLIENT_PRIVATE_IP:PORT through the router's public IP.
# Inserts DNAT rules in PREROUTING so inbound traffic on the forwarded port reaches the
# client, bypassing any conntrack filtering from the NAT type configuration above.
# Also inserts FORWARD ACCEPT rules so forwarded packets are not dropped.
if [[ -n "$PORT_FORWARD" && "$PORT_FORWARD" != "false" ]]; then
    if [[ -z "${CLIENT_PRIVATE_IP:-}" ]]; then
        echo "PORT_FORWARD: CLIENT_PRIVATE_IP not set, skipping port forward"
    else
        echo "Port forwarding: ${ROUTER_PUBLIC_IP}:${PORT_FORWARD_PORT} → ${CLIENT_PRIVATE_IP}:${PORT_FORWARD_PORT} (TCP+UDP)"
        iptables -t nat -I PREROUTING -i "$PUB_IFACE" -p tcp --dport "$PORT_FORWARD_PORT" -j DNAT --to-destination "${CLIENT_PRIVATE_IP}:${PORT_FORWARD_PORT}"
        iptables -t nat -I PREROUTING -i "$PUB_IFACE" -p udp --dport "$PORT_FORWARD_PORT" -j DNAT --to-destination "${CLIENT_PRIVATE_IP}:${PORT_FORWARD_PORT}"
        # Allow forwarded packets through regardless of conntrack state.
        iptables -I FORWARD -i "$PUB_IFACE" -o "$PRIV_IFACE" -p tcp -d "$CLIENT_PRIVATE_IP" --dport "$PORT_FORWARD_PORT" -j ACCEPT
        iptables -I FORWARD -i "$PUB_IFACE" -o "$PRIV_IFACE" -p udp -d "$CLIENT_PRIVATE_IP" --dport "$PORT_FORWARD_PORT" -j ACCEPT
    fi
fi

# UPnP/NAT-PMP via miniupnpd: lets the client dynamically request port mappings.
# miniupnpd manages its own iptables chain (MINIUPNPD) and handles IGD discovery
# via SSDP on the private-net interface. libp2p's NATPortMap() will find and use it.
if [[ -n "$UPNP" && "$UPNP" != "false" ]]; then
    echo "Starting miniupnpd (UPnP IGD + NAT-PMP)"
    mkdir -p /etc/miniupnpd
    cat > /etc/miniupnpd/miniupnpd.conf <<EOF
ext_ifname=$PUB_IFACE
listening_ip=$PRIV_IFACE
port=2189
system_uptime=yes
secure_mode=no
enable_natpmp=yes
clean_ruleset_interval=600
EOF
    miniupnpd -f /etc/miniupnpd/miniupnpd.conf
    echo "miniupnpd started on $PRIV_IFACE (ext: $PUB_IFACE)"
fi

# Apply network degradation if configured.
# tc netem is egress-only, so we apply on BOTH interfaces for symmetric delay:
#   PUB_IFACE egress  = client→server direction (forwarded packets leaving toward servers)
#   PRIV_IFACE egress = server→client direction (forwarded packets leaving toward client)
# LATENCY_MS is one-way delay, so RTT = 2 * LATENCY_MS.
if [[ "$PACKET_LOSS" -gt 0 || "$LATENCY_MS" -gt 0 ]]; then
    NETEM_ARGS=""
    if [[ "$LATENCY_MS" -gt 0 ]]; then
        NETEM_ARGS="delay ${LATENCY_MS}ms"
    fi
    if [[ "$PACKET_LOSS" -gt 0 ]]; then
        NETEM_ARGS="$NETEM_ARGS loss ${PACKET_LOSS}%"
    fi
    echo "Applying tc netem on $PUB_IFACE (client→server): $NETEM_ARGS"
    tc qdisc add dev "$PUB_IFACE" root netem $NETEM_ARGS
    echo "Applying tc netem on $PRIV_IFACE (server→client): $NETEM_ARGS"
    tc qdisc add dev "$PRIV_IFACE" root netem $NETEM_ARGS
    echo "Effective RTT: $((LATENCY_MS * 2))ms"
fi

echo "=== Router ready ==="
echo "iptables rules:"
iptables -L -v -n
echo ""
echo "NAT rules:"
iptables -t nat -L -v -n

# Keep the container running
exec tail -f /dev/null
