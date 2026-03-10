#!/bin/bash
set -euo pipefail

# Run the autonat-node as a local AutoNAT v2 server with full tracing.
#
# The server listens for incoming AutoNAT v2 dial requests from clients,
# performs dial-backs to test their reachability, and records everything
# in an OTEL trace file.
#
# Usage:
#   ./testbed/run-server.sh [options]
#
# Options:
#   --transport=<tcp|quic|both>   Transport to listen on (default: both)
#   --port=<number>               Listen port (default: 4001)
#   --addr-file=<path>            Write multiaddr to file for client discovery
#   --otlp-endpoint=<url>         Also push traces to OTLP collector (e.g. Jaeger)
#   --label=<string>              Label for output files (default: server)
#
# Examples:
#   ./testbed/run-server.sh
#   ./testbed/run-server.sh --transport=quic --port=5001
#   ./testbed/run-server.sh --otlp-endpoint=http://localhost:4318
#
# Output:
#   results/server/<label>-<timestamp>.trace.json  — OTEL trace
#   results/server/<label>-<timestamp>.addr        — multiaddr for sharing with clients
#
# After the server stops, run the validation script on the trace:
#   ./testbed/validate-server-nat.sh results/server/<label>-<timestamp>.trace.json
#
# Dependencies: go, jq

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Defaults
TRANSPORT="both"
PORT=4001
ADDR_FILE=""
OTLP_ENDPOINT=""
LABEL="server"

for arg in "$@"; do
    case "$arg" in
        --transport=*)   TRANSPORT="${arg#*=}" ;;
        --port=*)        PORT="${arg#*=}" ;;
        --addr-file=*)   ADDR_FILE="${arg#*=}" ;;
        --otlp-endpoint=*) OTLP_ENDPOINT="${arg#*=}" ;;
        --label=*)       LABEL="${arg#*=}" ;;
        -h|--help)
            head -30 "$0" | tail -28
            exit 0
            ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# Check dependencies
for cmd in go jq; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "Error: $cmd is required but not installed."
        exit 1
    fi
done

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="results/server"
TRACE_FILE="${RESULT_DIR}/${LABEL}-${TIMESTAMP}.trace.json"
ADDR_OUT="${RESULT_DIR}/${LABEL}-${TIMESTAMP}.addr"
BINARY="./autonat-node-server"

mkdir -p "$RESULT_DIR"

echo "=== AutoNAT v2 Server ==="
echo "Transport: $TRANSPORT"
echo "Port:      $PORT"
echo "Trace:     $TRACE_FILE"
echo ""

# Build
echo "Building autonat-node..."
go build -C testbed -o "../$BINARY" .
echo "Build complete."
echo ""

# Detect local IPs for display
LOCAL_IPS=$(ip -4 addr show scope global 2>/dev/null \
    | grep -oE 'inet [0-9.]+' | awk '{print $2}' \
    || ifconfig 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' \
    || echo "unknown")

echo "Local IPs detected:"
for ip in $LOCAL_IPS; do
    echo "  $ip"
done
echo ""
echo "The server's full multiaddr will be printed below once it starts."
echo "Share it with clients using --peers=<multiaddr>"
echo ""

# Build node flags
NODE_FLAGS=(
    "--role=server"
    "--transport=$TRANSPORT"
    "--port=$PORT"
    "--trace-file=$TRACE_FILE"
    "--addr-file=$ADDR_OUT"
)
[[ -n "$OTLP_ENDPOINT" ]] && NODE_FLAGS+=("--otlp-endpoint=$OTLP_ENDPOINT")

# Start the server, tee logs to file and stdout
LOG_FILE="${RESULT_DIR}/${LABEL}-${TIMESTAMP}.log"

echo "Starting server... (Ctrl-C to stop)"
echo ""

"$BINARY" "${NODE_FLAGS[@]}" 2>&1 | tee "$LOG_FILE" &
SERVER_PID=$!

# Wait for the addr file to be written (node is ready)
DEADLINE=$(($(date +%s) + 15))
while [[ ! -s "$ADDR_OUT" ]]; do
    if [[ $(date +%s) -ge $DEADLINE ]]; then
        echo "ERROR: Server did not write addr file within 15s. Check log: $LOG_FILE"
        kill "$SERVER_PID" 2>/dev/null || true
        rm -f "$BINARY"
        exit 1
    fi
    sleep 0.5
done

MULTIADDR=$(cat "$ADDR_OUT")
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║ SERVER READY                                                      ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║ Multiaddr:                                                        ║"
echo "  $MULTIADDR"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║ Run a client against this server:                                 ║"
echo "  ./testbed/run-local.sh --peers=$MULTIADDR"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "Trace file: $TRACE_FILE"
echo "Log file:   $LOG_FILE"
echo ""
echo "--- Live server activity (Ctrl-C to stop) ---"

# Monitor log for key events and print highlights
tail -f "$LOG_FILE" 2>/dev/null | grep --line-buffered -E \
    "Connected|connect_failed|REACHAB|PROTOCOLS|peer_id|Listening|Starting|Shutdown|autonat" \
    || true &
TAIL_PID=$!

# Wait for server to exit (SIGINT/SIGTERM)
wait "$SERVER_PID" 2>/dev/null || true
kill "$TAIL_PID" 2>/dev/null || true

echo ""
echo "--- Server stopped ---"
echo ""
echo "Trace written to: $TRACE_FILE"
echo ""
echo "Validate NAT status from server perspective:"
echo "  ./testbed/validate-server-nat.sh $TRACE_FILE"

rm -f "$BINARY"
