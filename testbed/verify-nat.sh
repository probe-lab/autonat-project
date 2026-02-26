#!/bin/bash
set -euo pipefail

# Verify NAT types work correctly using plain TCP/UDP (no libp2p).
#
# For each NAT type, starts router + client + 2 servers and runs
# connectivity tests to confirm the NAT behaves as expected.
#
# Usage:
#   ./testbed/verify-nat.sh              # run all NAT types
#   ./testbed/verify-nat.sh symmetric    # run one NAT type

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

PASS=0
FAIL=0
SKIP=0

pass() { echo "  ✓ PASS: $1"; ((PASS++)) || true; }
fail() { echo "  ✗ FAIL: $1"; ((FAIL++)) || true; }
skip() { echo "  - SKIP: $1"; ((SKIP++)) || true; }

compose() {
    docker compose -f testbed/docker/compose.yml --profile test "$@"
}

# ── Environment setup ────────────────────────────────────────────────────

start_env() {
    local nat_type="$1"
    echo ""
    echo "============================================"
    echo "  NAT Type: $nat_type"
    echo "============================================"

    compose down --volumes --remove-orphans 2>/dev/null || true
    NAT_TYPE="$nat_type" compose up -d --build 2>&1 | grep -E "^(  |#[0-9]+ \[)" | tail -5

    # Wait for router's iptables rules to be ready (entrypoint may need time
    # to discover the client IP for full-cone and address-restricted NATs)
    local ready=false
    for i in $(seq 1 30); do
        if compose exec -T router sh -c "iptables -L FORWARD -n 2>/dev/null" | grep -qE 'ACCEPT|DROP'; then
            ready=true
            break
        fi
        sleep 1
    done
    if [[ "$ready" != "true" ]]; then
        echo "  WARNING: Router iptables not ready after 30s"
    fi

    # Get router's private-net IP and set client gateway
    ROUTER_PRIV_IP=$(compose exec -T router sh -c \
        "ip -4 addr show | grep -oE '10\.0\.1\.[0-9]+' | head -1" | tr -d '\r\n[:space:]')
    ROUTER_PUB_IP=$(compose exec -T router sh -c \
        "ip -4 addr show | grep -oE '73\.0\.0\.[0-9]+' | head -1" | tr -d '\r\n[:space:]')

    compose exec -T test-client sh -c "
        ip route del default 2>/dev/null || true
        ip route add default via $ROUTER_PRIV_IP
    " 2>/dev/null

    CLIENT_IP=$(compose exec -T test-client sh -c "hostname -i" | tr -d '\r\n[:space:]')
    SERVER1_IP=$(compose exec -T test-server1 sh -c "hostname -i" | tr -d '\r\n[:space:]')
    SERVER2_IP=$(compose exec -T test-server2 sh -c "hostname -i" | tr -d '\r\n[:space:]')

    # Add routes from servers to private-net via router
    compose exec -T test-server1 sh -c "ip route add 10.0.1.0/24 via $ROUTER_PUB_IP 2>/dev/null || true" 2>/dev/null
    compose exec -T test-server2 sh -c "ip route add 10.0.1.0/24 via $ROUTER_PUB_IP 2>/dev/null || true" 2>/dev/null

    echo "  Client: $CLIENT_IP | Server1: $SERVER1_IP | Server2: $SERVER2_IP"
    echo "  Router: pub=$ROUTER_PUB_IP priv=$ROUTER_PRIV_IP"
}

teardown() {
    compose down --volumes --remove-orphans 2>/dev/null || true
}

# ── Test primitives ──────────────────────────────────────────────────────
# Uses socat for UDP (more reliable than BusyBox nc in Docker exec).

# Test TCP reachability: can <from> connect to <target_ip>:<port>?
# Returns 0=reachable, 1=unreachable
tcp_reach() {
    local from="$1" target_ip="$2" port="$3"

    # Listen on test-client (socat TCP listener, writes received data to file)
    compose exec -d -T test-client sh -c \
        "rm -f /tmp/tcp_out; timeout 6 socat -u TCP-LISTEN:$port,reuseaddr OPEN:/tmp/tcp_out,creat,trunc" 2>/dev/null || true
    sleep 1

    # Connect from source
    compose exec -T "$from" sh -c \
        "echo PROBE | timeout 3 socat -u - TCP:$target_ip:$port" 2>/dev/null || true
    sleep 2

    local got
    got=$(compose exec -T test-client sh -c "cat /tmp/tcp_out 2>/dev/null" | tr -d '\r\n[:space:]')
    [[ "$got" == *"PROBE"* ]]
}

# Send UDP from client to a server (to create NAT mapping).
# Usage: client_udp_send <server_ip> <server_port> <client_src_port>
client_udp_send() {
    local server_ip="$1" server_port="$2" client_src_port="$3"

    # Listen on both servers (absorb the packet)
    compose exec -T test-server1 sh -c "killall socat 2>/dev/null || true" 2>/dev/null || true
    compose exec -T test-server2 sh -c "killall socat 2>/dev/null || true" 2>/dev/null || true
    compose exec -d -T test-server1 sh -c \
        "timeout 5 socat -u UDP-RECV:$server_port /dev/null" 2>/dev/null || true
    compose exec -d -T test-server2 sh -c \
        "timeout 5 socat -u UDP-RECV:$server_port /dev/null" 2>/dev/null || true
    sleep 1

    # Send from client using a specific source port
    compose exec -T test-client sh -c \
        "echo INIT | timeout 2 socat -u - UDP-SENDTO:$server_ip:$server_port,sourceport=$client_src_port" 2>/dev/null || true
    sleep 1
}

# Test UDP reachability: can <from> send UDP to <target_ip>:<target_port>?
# Optionally from a specific source port.
# Returns 0=reached, 1=not reached
udp_reach() {
    local from="$1" target_ip="$2" target_port="$3" src_port="${4:-}"

    # Clean up previous listeners
    compose exec -T test-client sh -c "killall socat 2>/dev/null; rm -f /tmp/udp_out" 2>/dev/null || true

    # Start UDP listener detached on test-client
    compose exec -d -T test-client sh -c \
        "timeout 8 socat -u UDP-RECV:$target_port OPEN:/tmp/udp_out,creat,trunc" 2>/dev/null || true
    sleep 2

    # Send from source (kill any lingering socat on the sender first to free ports)
    compose exec -T "$from" sh -c "killall socat 2>/dev/null" 2>/dev/null || true
    local src_opt=""
    if [[ -n "$src_port" ]]; then
        src_opt=",sourceport=$src_port"
    fi
    compose exec -T "$from" sh -c \
        "echo PROBE | timeout 3 socat -u - UDP-SENDTO:$target_ip:$target_port${src_opt}" 2>/dev/null || true
    sleep 2

    local got
    got=$(compose exec -T test-client sh -c "cat /tmp/udp_out 2>/dev/null" | tr -d '\r\n[:space:]')
    compose exec -T test-client sh -c "killall socat 2>/dev/null" 2>/dev/null || true
    [[ "$got" == *"PROBE"* ]]
}

# Observe what source port a server sees when the client sends UDP through NAT.
# Uses conntrack on the router.
# Usage: get_nat_mapped_port <dest_server_ip> <dest_port> <client_src_port>
get_nat_mapped_port() {
    local dest_ip="$1" dest_port="$2" client_src_port="$3"

    client_udp_send "$dest_ip" "$dest_port" "$client_src_port"
    sleep 1

    # Read conntrack on router
    local mapping
    mapping=$(compose exec -T router sh -c "
        cat /proc/net/nf_conntrack 2>/dev/null | grep 'sport=$client_src_port' | grep 'dport=$dest_port' | head -1
    " 2>/dev/null || true)

    if [[ -z "$mapping" ]]; then
        echo ""
        return
    fi

    # In conntrack, the reply tuple shows the NATted source port
    # Format: ... src=<orig_src> dst=<orig_dst> sport=<orig_sport> dport=<orig_dport> src=<reply_src> dst=<reply_dst> sport=<reply_sport> dport=<nat_sport>
    # The last sport= is the NATted source port
    local nat_port
    nat_port=$(echo "$mapping" | grep -oE 'sport=[0-9]+' | tail -1 | cut -d= -f2)
    echo "$nat_port"
}

# ── NAT tests ────────────────────────────────────────────────────────────

test_no_nat() {
    start_env "none"
    echo "  --- Tests ---"

    # Server can reach client directly via TCP
    if tcp_reach test-server1 "$CLIENT_IP" 8000; then
        pass "TCP: Server1 reaches client directly"
    else
        fail "TCP: Server1 cannot reach client"
    fi

    # Server can reach client directly via UDP
    if udp_reach test-server1 "$CLIENT_IP" 8001; then
        pass "UDP: Server1 reaches client directly"
    else
        fail "UDP: Server1 cannot reach client"
    fi

    # Uncontacted server can also reach client (no filtering)
    if udp_reach test-server2 "$CLIENT_IP" 8002; then
        pass "UDP: Server2 (uncontacted) reaches client"
    else
        fail "UDP: Server2 (uncontacted) cannot reach client"
    fi
}

test_full_cone() {
    start_env "full-cone"
    echo "  --- Tests ---"

    # Create mapping: client → server1
    client_udp_send "$SERVER1_IP" 7000 9000

    # Server1 (contacted) can reach client through NAT
    if udp_reach test-server1 "$ROUTER_PUB_IP" 9000; then
        pass "UDP: Server1 (contacted) reaches client"
    else
        fail "UDP: Server1 (contacted) cannot reach client"
    fi

    # Server2 (NEVER contacted) can ALSO reach client — full-cone property
    if udp_reach test-server2 "$ROUTER_PUB_IP" 9000; then
        pass "UDP: Server2 (uncontacted) reaches client — FULL CONE confirmed"
    else
        fail "UDP: Server2 (uncontacted) blocked — NOT full cone"
    fi
}

test_address_restricted() {
    start_env "address-restricted"
    echo "  --- Tests ---"

    # Create mapping: client → server1
    client_udp_send "$SERVER1_IP" 7000 9000

    # Server1 from DIFFERENT port can reach client (same IP allowed)
    if udp_reach test-server1 "$ROUTER_PUB_IP" 9000 7777; then
        pass "UDP: Server1 different port reaches client — address filtering only"
    else
        fail "UDP: Server1 different port blocked — too restrictive"
    fi

    # Server2 (never contacted) should be BLOCKED
    if udp_reach test-server2 "$ROUTER_PUB_IP" 9000; then
        fail "UDP: Server2 (uncontacted) reaches client — should be blocked!"
    else
        pass "UDP: Server2 (uncontacted) blocked — ADDRESS RESTRICTED confirmed"
    fi
}

test_port_restricted() {
    start_env "port-restricted"
    echo "  --- Tests ---"

    # Create mapping: client → server1:7000
    client_udp_send "$SERVER1_IP" 7000 9000

    # Server1 from SAME port — should work
    if udp_reach test-server1 "$ROUTER_PUB_IP" 9000 7000; then
        pass "UDP: Server1 same port (7000) reaches client"
    else
        fail "UDP: Server1 same port (7000) blocked"
    fi

    # Server1 from DIFFERENT port — should be BLOCKED
    if udp_reach test-server1 "$ROUTER_PUB_IP" 9000 7777; then
        fail "UDP: Server1 different port (7777) reaches client — should be blocked!"
    else
        pass "UDP: Server1 different port blocked — PORT RESTRICTED confirmed"
    fi

    # Server2 (uncontacted) — should be BLOCKED
    if udp_reach test-server2 "$ROUTER_PUB_IP" 9000; then
        fail "UDP: Server2 (uncontacted) reaches client — should be blocked!"
    else
        pass "UDP: Server2 (uncontacted) blocked"
    fi
}

test_symmetric() {
    start_env "symmetric"
    echo "  --- Tests ---"

    # Send from client to BOTH servers from the same source port
    # and check if the NAT assigns different external ports
    compose exec -d -T test-server1 sh -c "timeout 5 socat -u UDP-RECV:7001 /dev/null" 2>/dev/null || true
    compose exec -d -T test-server2 sh -c "timeout 5 socat -u UDP-RECV:7002 /dev/null" 2>/dev/null || true
    sleep 1

    compose exec -T test-client sh -c \
        "echo TEST1 | timeout 2 socat -u - UDP-SENDTO:$SERVER1_IP:7001,sourceport=9000" 2>/dev/null || true
    sleep 1

    compose exec -T test-client sh -c \
        "echo TEST2 | timeout 2 socat -u - UDP-SENDTO:$SERVER2_IP:7002,sourceport=9000" 2>/dev/null || true
    sleep 1

    # Check conntrack for the two mappings
    local ct_output
    ct_output=$(compose exec -T router sh -c "cat /proc/net/nf_conntrack 2>/dev/null" 2>/dev/null || true)

    # Conntrack format: ... src=X dst=Y sport=9000 dport=7001 src=Y dst=ROUTER sport=7001 dport=<NAT_PORT>
    # The NATted source port is the LAST dport= in the reply tuple
    local port_to_s1 port_to_s2
    port_to_s1=$(echo "$ct_output" | grep "dport=7001" | grep -oE 'dport=[0-9]+' | tail -1 | cut -d= -f2)
    port_to_s2=$(echo "$ct_output" | grep "dport=7002" | grep -oE 'dport=[0-9]+' | tail -1 | cut -d= -f2)

    echo "  NAT mapped port → server1: ${port_to_s1:-(not found)}"
    echo "  NAT mapped port → server2: ${port_to_s2:-(not found)}"

    if [[ -n "$port_to_s1" && -n "$port_to_s2" ]]; then
        if [[ "$port_to_s1" != "$port_to_s2" ]]; then
            pass "Different external ports per destination ($port_to_s1 vs $port_to_s2) — SYMMETRIC confirmed"
        else
            fail "Same external port ($port_to_s1) for both — not symmetric"
        fi
    else
        skip "Could not read conntrack entries"
    fi

    # Also verify filtering: server2 cannot reach client via server1's mapping
    if [[ -n "$port_to_s1" ]]; then
        if udp_reach test-server2 "$ROUTER_PUB_IP" "$port_to_s1"; then
            fail "UDP: Server2 reached client via server1's mapping — should be blocked"
        else
            pass "UDP: Server2 blocked from server1's mapping — symmetric filtering confirmed"
        fi
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────

trap teardown EXIT

SELECTED="${1:-all}"

case "$SELECTED" in
    all)
        test_no_nat
        test_full_cone
        test_address_restricted
        test_port_restricted
        test_symmetric
        ;;
    none)               test_no_nat ;;
    full-cone)          test_full_cone ;;
    address-restricted) test_address_restricted ;;
    port-restricted)    test_port_restricted ;;
    symmetric)          test_symmetric ;;
    *)
        echo "Usage: $0 [none|full-cone|address-restricted|port-restricted|symmetric|all]"
        exit 1
        ;;
esac

echo ""
echo "============================================"
echo "  Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "============================================"

[[ $FAIL -eq 0 ]]
