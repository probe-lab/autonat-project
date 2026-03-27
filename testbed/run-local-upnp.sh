#!/bin/bash
set -euo pipefail

# Run AutoNAT v2 + UPnP local tests across go, rust, and js implementations.
# Must be run on a machine behind a real NAT router with UPnP enabled.
#
# Usage: ./testbed/run-local-upnp.sh [options]
#
# Options:
#   --impl=<go|rust|js|all>   Implementation to test (default: all)
#   --transport=<tcp|quic|both>  Transport (default: both)
#   --timeout=<seconds>       Max time per run (default: 120)
#   --runs=<count>            Number of runs per impl (default: 1)
#   --port=<number>           Listen port (default: 4001)
#
# Examples:
#   ./testbed/run-local-upnp.sh
#   ./testbed/run-local-upnp.sh --impl=rust --timeout=300
#   ./testbed/run-local-upnp.sh --impl=all --runs=3

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Defaults
IMPL="all"
TRANSPORT="both"
TIMEOUT=120
RUNS=1
PORT=4001
STABLE_WAIT=15

# Parse arguments
for arg in "$@"; do
    case "$arg" in
        --impl=*)      IMPL="${arg#*=}" ;;
        --transport=*) TRANSPORT="${arg#*=}" ;;
        --timeout=*)   TIMEOUT="${arg#*=}" ;;
        --runs=*)      RUNS="${arg#*=}" ;;
        --port=*)      PORT="${arg#*=}" ;;
        -h|--help)
            head -17 "$0" | tail -15
            exit 0
            ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="results/local/upnp-${TIMESTAMP}"
mkdir -p "$RESULT_DIR"

echo "=== AutoNAT v2 + UPnP Local Test ==="
echo "Implementations: $IMPL"
echo "Transport:       $TRANSPORT"
echo "Timeout:         ${TIMEOUT}s per run"
echo "Runs:            $RUNS per impl"
echo "Output:          $RESULT_DIR/"
echo ""

# ---------------------------------------------------------------------------
# Run a single implementation
# ---------------------------------------------------------------------------
run_impl() {
    local impl_name="$1"
    local binary_cmd="$2"
    shift 2
    local extra_args=("$@")

    echo ""
    echo "========================================"
    echo "  $impl_name"
    echo "========================================"

    for ((run=1; run<=RUNS; run++)); do
        echo "--- Run $run/$RUNS ---"

        TRACE_LOG="${RESULT_DIR}/${impl_name}-run${run}.trace.json"

        # Start the node
        $binary_cmd \
            --transport="$TRANSPORT" \
            --port="$PORT" \
            --trace-file="$TRACE_LOG" \
            "${extra_args[@]}" &
        local NODE_PID=$!

        echo "Started $impl_name node (PID $NODE_PID), tracing to $TRACE_LOG"
        echo "Waiting for reachability events (timeout: ${TIMEOUT}s, stable: ${STABLE_WAIT}s)..."

        local START_EPOCH
        START_EPOCH=$(date +%s)

        while true; do
            local NOW
            NOW=$(date +%s)
            local ELAPSED=$((NOW - START_EPOCH))

            if [[ $ELAPSED -ge $TIMEOUT ]]; then
                echo ""
                echo "Timeout reached (${TIMEOUT}s)."
                break
            fi

            if ! kill -0 "$NODE_PID" 2>/dev/null; then
                echo ""
                echo "Node exited unexpectedly."
                break
            fi

            # Check for reachable addresses in trace file
            if [[ -f "$TRACE_LOG" ]]; then
                local HAS_REACHABLE
                HAS_REACHABLE=$(jq -r '
                    select(.Name == "reachable_addrs_changed") |
                    .Attributes[] |
                    select(.Key == "reachable") |
                    .Value.Value |
                    if type == "array" then length else 0 end
                ' "$TRACE_LOG" 2>/dev/null | grep -v '^0$' | tail -1 || true)

                if [[ -n "$HAS_REACHABLE" ]]; then
                    local LAST_MS
                    LAST_MS=$(jq -r '
                        select(.Name == "reachable_addrs_changed") |
                        .Attributes[] |
                        select(.Key == "elapsed_ms") |
                        .Value.Value
                    ' "$TRACE_LOG" 2>/dev/null | tail -1 || true)

                    local LAST_EPOCH=$((START_EPOCH + ${LAST_MS:-0} / 1000))
                    local SINCE_LAST=$((NOW - LAST_EPOCH))
                    if [[ $SINCE_LAST -ge $STABLE_WAIT ]]; then
                        echo ""
                        echo "Stable for ${STABLE_WAIT}s — reachable addresses found"
                        break
                    fi
                fi
            fi

            sleep 2
            printf "."
        done

        # Stop the node
        if kill -0 "$NODE_PID" 2>/dev/null; then
            kill -TERM "$NODE_PID" 2>/dev/null || true
            wait "$NODE_PID" 2>/dev/null || true
        fi

        echo ""

        # Quick summary
        if [[ -f "$TRACE_LOG" ]] && [[ -s "$TRACE_LOG" ]]; then
            local REACHABLE_COUNT
            REACHABLE_COUNT=$(jq -r '
                select(.Name == "reachable_addrs_changed") |
                .Attributes[] |
                select(.Key == "reachable") |
                .Value.Value |
                if type == "array" then .[] else empty end
            ' "$TRACE_LOG" 2>/dev/null | sort -u | wc -l | tr -d ' ')

            local FIRST_REACHABLE_MS
            FIRST_REACHABLE_MS=$(jq -r '
                select(.Name == "reachable_addrs_changed") |
                select(.Attributes[] | select(.Key == "reachable") | .Value.Value | if type == "array" then length > 0 else false end) |
                .Attributes[] | select(.Key == "elapsed_ms") | .Value.Value
            ' "$TRACE_LOG" 2>/dev/null | head -1 || echo "none")

            echo "  Reachable addresses: $REACHABLE_COUNT"
            echo "  First reachable at: ${FIRST_REACHABLE_MS}ms"
        else
            echo "  No trace output."
        fi

        # Gap between runs
        if [[ $run -lt $RUNS ]]; then
            sleep 2
        fi
    done
}

# ---------------------------------------------------------------------------
# Build and run each implementation
# ---------------------------------------------------------------------------

if [[ "$IMPL" == "all" || "$IMPL" == "go" ]]; then
    echo "Building Go node..."
    go build -o ./autonat-node-go ./testbed
    run_impl "go" "./autonat-node-go" --role=client --bootstrap
    rm -f ./autonat-node-go
fi

if [[ "$IMPL" == "all" || "$IMPL" == "rust" ]]; then
    echo "Building Rust node..."
    (cd testbed/docker/node-rust && cargo build --release)
    run_impl "rust" "./testbed/docker/node-rust/target/release/autonat-node-rust" \
        --role=client --bootstrap --upnp
fi

if [[ "$IMPL" == "all" || "$IMPL" == "js" ]]; then
    echo "Building JS node..."
    (cd testbed/docker/node-js && npm install && npm run build)
    run_impl "js" "node testbed/docker/node-js/dist/index.js" \
        --role=client --bootstrap --upnp
fi

echo ""
echo "=== Done ==="
echo "Results in: $RESULT_DIR/"
echo "Analyze with: python3 testbed/analyze.py $RESULT_DIR/*.json"
