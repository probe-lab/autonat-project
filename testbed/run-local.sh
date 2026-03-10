#!/bin/bash
set -euo pipefail

# Run AutoNAT v2 client locally (no Docker) against the real IPFS/libp2p network.
#
# Usage: ./testbed/run-local.sh [options]
#
# Options:
#   --transport=<tcp|quic|both>  Transport to use (default: both)
#   --timeout=<seconds>          Max time per run (default: 120)
#   --runs=<count>               Number of runs (default: 1)
#   --label=<string>             Label for result file (default: local)
#   --port=<number>              Listen port (default: 4001)
#   --peers=<multiaddr>          Connect to a specific server instead of bootstrapping IPFS
#
# Examples:
#   ./testbed/run-local.sh
#   ./testbed/run-local.sh --transport=quic --timeout=60
#   ./testbed/run-local.sh --runs=3 --label=home-wifi
#   ./testbed/run-local.sh --peers=/ip4/1.2.3.4/tcp/4001/p2p/12D3Koo...

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Defaults
TRANSPORT="both"
TIMEOUT=120
RUNS=1
LABEL="local"
PORT=4001
PEERS=""

# Parse arguments
for arg in "$@"; do
    case "$arg" in
        --transport=*) TRANSPORT="${arg#*=}" ;;
        --timeout=*)   TIMEOUT="${arg#*=}" ;;
        --runs=*)      RUNS="${arg#*=}" ;;
        --label=*)     LABEL="${arg#*=}" ;;
        --port=*)      PORT="${arg#*=}" ;;
        --peers=*)     PEERS="${arg#*=}" ;;
        -h|--help)
            head -16 "$0" | tail -14
            exit 0
            ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# Check dependencies
if ! command -v jq &>/dev/null; then
    echo "Error: jq is required but not installed."
    echo "  macOS: brew install jq"
    echo "  Linux: apt-get install jq"
    exit 1
fi

if ! command -v go &>/dev/null; then
    echo "Error: go toolchain is required but not found."
    exit 1
fi

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_BASE="results/local/${TRANSPORT}-${LABEL}-${TIMESTAMP}"
SUMMARY_FILE="${RESULT_BASE}.json"
BINARY="./autonat-node"
STABLE_WAIT=15  # seconds to wait after last event before declaring stable

mkdir -p results/local

echo "=== AutoNAT v2 Local Experiment ==="
echo "Transport:  $TRANSPORT"
echo "Timeout:    ${TIMEOUT}s per run"
echo "Runs:       $RUNS"
echo "Label:      $LABEL"
echo "Output:     $SUMMARY_FILE"
echo ""

# Build the binary
echo "Building autonat-node..."
go build -o "$BINARY" ./testbed
echo "Build complete."
echo ""

# Collect results from all runs
ALL_RUNS_JSON="[]"

for ((run=1; run<=RUNS; run++)); do
    echo "--- Run $run/$RUNS ---"

    TRACE_LOG="${RESULT_BASE}-run${run}.trace.json"
    NODE_PID=""

    # Start the node
    if [[ -n "$PEERS" ]]; then
        "$BINARY" --role=client --peers="$PEERS" --transport="$TRANSPORT" --port="$PORT" --trace-file="$TRACE_LOG" &
    else
        "$BINARY" --role=client --bootstrap --transport="$TRANSPORT" --port="$PORT" --trace-file="$TRACE_LOG" &
    fi
    NODE_PID=$!

    echo "Started node (PID $NODE_PID), tracing to $TRACE_LOG"
    echo "Waiting for reachability events (timeout: ${TIMEOUT}s, stable after: ${STABLE_WAIT}s)..."

    # Monitor for convergence using stderr log output (grep docker logs style)
    START_EPOCH=$(date +%s)
    LAST_EVENT_EPOCH=0
    CONVERGED=false

    while true; do
        NOW=$(date +%s)
        ELAPSED=$((NOW - START_EPOCH))

        # Check timeout
        if [[ $ELAPSED -ge $TIMEOUT ]]; then
            echo ""
            echo "Timeout reached (${TIMEOUT}s)."
            break
        fi

        # Check if process is still running
        if ! kill -0 "$NODE_PID" 2>/dev/null; then
            echo ""
            echo "Node exited unexpectedly."
            break
        fi

        # Check for reachability events in trace file
        if [[ -f "$TRACE_LOG" ]]; then
            # Look for session span events with reachability_changed
            LAST_REACH=$(jq -r 'select(.Name == "autonat.session") | .Events[]? | select(.Name == "reachability_changed") | .Attributes[] | select(.Key == "elapsed_ms") | .Value.Value' "$TRACE_LOG" 2>/dev/null | tail -1 || true)
            LAST_REACH_STATUS=$(jq -r 'select(.Name == "autonat.session") | .Events[]? | select(.Name == "reachability_changed") | .Attributes[] | select(.Key == "reachability") | .Value.Value' "$TRACE_LOG" 2>/dev/null | tail -1 || true)

            if [[ -n "$LAST_REACH" && "$LAST_REACH" != "null" ]]; then
                LAST_EVENT_EPOCH=$((START_EPOCH + LAST_REACH / 1000))
                SINCE_LAST=$((NOW - LAST_EVENT_EPOCH))
                if [[ $SINCE_LAST -ge $STABLE_WAIT ]]; then
                    echo ""
                    echo "Stable for ${STABLE_WAIT}s (reachability: $LAST_REACH_STATUS)"
                    CONVERGED=true
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

    # Parse results from trace file
    if [[ ! -f "$TRACE_LOG" ]] || [[ ! -s "$TRACE_LOG" ]]; then
        echo "Warning: no trace output for run $run"
        RUN_JSON=$(jq -n \
            --argjson run "$run" \
            --arg trace_log "$TRACE_LOG" \
            '{
                run: $run,
                final_reachability: "none",
                time_to_first_event_ms: null,
                time_to_stable_ms: null,
                events: [],
                bootstrap_peers_connected: 0,
                trace_log: $trace_log
            }')
    else
        # Extract reachability events from OTEL trace
        REACH_EVENTS=$(jq -r '
            select(.Name == "autonat.session") | .Events[]? |
            select(.Name == "reachability_changed") |
            { elapsed_ms: (.Attributes[] | select(.Key == "elapsed_ms") | .Value.Value),
              reachability: (.Attributes[] | select(.Key == "reachability") | .Value.Value) }
        ' "$TRACE_LOG" 2>/dev/null || true)

        FIRST_MS="null"
        LAST_MS="null"
        FINAL_STATUS="none"

        if [[ -n "$REACH_EVENTS" ]]; then
            FIRST_MS=$(echo "$REACH_EVENTS" | jq -s '.[0].elapsed_ms' 2>/dev/null || echo "null")
            LAST_MS=$(echo "$REACH_EVENTS" | jq -s '.[-1].elapsed_ms' 2>/dev/null || echo "null")
            FINAL_STATUS=$(echo "$REACH_EVENTS" | jq -rs '.[-1].reachability' 2>/dev/null || echo "none")
        fi

        BOOTSTRAP_COUNT=$(jq -r '
            select(.Name == "autonat.session") | .Events[]? |
            select(.Name == "bootstrap_connected") | .Name
        ' "$TRACE_LOG" 2>/dev/null | wc -l | tr -d ' ' || echo "0")

        # Extract reachability-related events
        EVENTS=$(jq -s '[
            .[] | select(.Name == "autonat.session") | .Events[]? |
            select(.Name == "reachability_changed" or .Name == "reachable_addrs_changed") |
            { type: .Name,
              elapsed_ms: (.Attributes[] | select(.Key == "elapsed_ms") | .Value.Value),
              reachability: ((.Attributes[] | select(.Key == "reachability") | .Value.Value) // null),
              addresses: ((.Attributes[] | select(.Key == "addresses") | .Value.Value) // null) }
        ]' "$TRACE_LOG" 2>/dev/null || echo "[]")

        RUN_JSON=$(jq -n \
            --argjson run "$run" \
            --arg final "$FINAL_STATUS" \
            --argjson first_ms "$FIRST_MS" \
            --argjson last_ms "$LAST_MS" \
            --argjson events "$EVENTS" \
            --argjson bootstrap "$BOOTSTRAP_COUNT" \
            --arg trace_log "$TRACE_LOG" \
            '{
                run: $run,
                final_reachability: $final,
                time_to_first_event_ms: $first_ms,
                time_to_stable_ms: $last_ms,
                events: $events,
                bootstrap_peers_connected: $bootstrap,
                trace_log: $trace_log
            }')

        echo "  Reachability: $FINAL_STATUS"
        echo "  Time to first event: ${FIRST_MS}ms"
        echo "  Time to stable: ${LAST_MS}ms"
        echo "  Bootstrap peers: $BOOTSTRAP_COUNT"
    fi

    ALL_RUNS_JSON=$(echo "$ALL_RUNS_JSON" | jq --argjson r "$RUN_JSON" '. + [$r]')

    # Small gap between runs
    if [[ $run -lt $RUNS ]]; then
        echo ""
        sleep 2
    fi
done

# Write summary
jq -n \
    --arg mode "local" \
    --arg transport "$TRANSPORT" \
    --arg label "$LABEL" \
    --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --argjson runs "$ALL_RUNS_JSON" \
    '{
        mode: $mode,
        transport: $transport,
        label: $label,
        timestamp: $timestamp,
        runs: $runs
    }' > "$SUMMARY_FILE"

echo ""
echo "=== Summary ==="
echo "Results saved to: $SUMMARY_FILE"
jq '.' "$SUMMARY_FILE"

# Clean up binary
rm -f "$BINARY"
