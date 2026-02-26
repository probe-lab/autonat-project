#!/bin/bash
set -euo pipefail

# YAML-driven experiment runner for AutoNAT v2 testbed.
#
# Usage:
#   ./testbed/run.sh <scenario.yaml> [options]
#
# Options:
#   --timeout=N       Override timeout per scenario (seconds)
#   --runs=N          Override number of runs per scenario
#   --filter=K=V,...  Filter scenarios (AND logic): nat_type=symmetric,transport=quic
#   --dry-run         Print expanded scenarios without executing
#
# Examples:
#   ./testbed/run.sh testbed/scenarios/matrix.yaml
#   ./testbed/run.sh testbed/scenarios/matrix.yaml --dry-run
#   ./testbed/run.sh testbed/scenarios/matrix.yaml --filter=nat_type=none,transport=quic,server_count=5
#   ./testbed/run.sh testbed/scenarios/flight-wifi.yaml --runs=5
#
# Dependencies: yq, jq, python3

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

SCENARIO_FILE="${1:?Usage: $0 <scenario.yaml> [--timeout=N] [--runs=N] [--filter=K=V,...] [--dry-run]}"
shift

# Parse options
OPT_TIMEOUT=""
OPT_RUNS=""
OPT_FILTER=""
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --timeout=*)  OPT_TIMEOUT="${arg#*=}" ;;
        --runs=*)     OPT_RUNS="${arg#*=}" ;;
        --filter=*)   OPT_FILTER="${arg#*=}" ;;
        --dry-run)    DRY_RUN=true ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# Check dependencies
for cmd in yq jq python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "Error: $cmd is required but not found."
        [[ "$cmd" == "yq" ]] && echo "  Install: brew install yq  (or)  snap install yq"
        exit 1
    fi
done

# --- Parse YAML into expanded scenario list (JSON array) ---

YAML_JSON=$(yq -o=json "$SCENARIO_FILE")
SCENARIO_NAME=$(echo "$YAML_JSON" | jq -r '.name // "unnamed"')

# Read defaults
DEFAULT_TIMEOUT=$(echo "$YAML_JSON" | jq -r '.defaults.timeout_s // 120')
DEFAULT_RUNS=$(echo "$YAML_JSON" | jq -r '.defaults.runs // 1')

# Expand scenarios: either from explicit list or matrix Cartesian product
if echo "$YAML_JSON" | jq -e '.matrix' &>/dev/null; then
    # Matrix mode: Cartesian product of all array fields
    SCENARIOS=$(echo "$YAML_JSON" | jq '
        .matrix | to_entries
        | reduce .[] as $dim (
            [{}];
            . as $acc | $dim.value | map(. as $val |
                $acc[] | . + {($dim.key): $val}
            )
        )
    ')
else
    # Explicit scenarios list
    SCENARIOS=$(echo "$YAML_JSON" | jq '.scenarios // []')
fi

# Merge defaults into each scenario
SCENARIOS=$(echo "$SCENARIOS" | jq --argjson dt "$DEFAULT_TIMEOUT" --argjson dr "$DEFAULT_RUNS" '
    [.[] | . + {
        timeout_s: (.timeout_s // $dt),
        runs: (.runs // $dr),
        packet_loss: (.packet_loss // 0),
        latency_ms: (.latency_ms // 0),
        tcp_block_port: (.tcp_block_port // null),
        port_remap: (.port_remap // null),
        obs_addr_thresh: (.obs_addr_thresh // null),
        assertions: (.assertions // null)
    }]
')

# Apply CLI overrides
if [[ -n "$OPT_TIMEOUT" ]]; then
    SCENARIOS=$(echo "$SCENARIOS" | jq --argjson t "$OPT_TIMEOUT" '[.[] | .timeout_s = $t]')
fi
if [[ -n "$OPT_RUNS" ]]; then
    SCENARIOS=$(echo "$SCENARIOS" | jq --argjson r "$OPT_RUNS" '[.[] | .runs = $r]')
fi

# Apply --filter (AND logic, comma-separated key=value pairs)
if [[ -n "$OPT_FILTER" ]]; then
    IFS=',' read -ra FILTERS <<< "$OPT_FILTER"
    for f in "${FILTERS[@]}"; do
        KEY="${f%%=*}"
        VAL="${f#*=}"
        # Match as string or number
        SCENARIOS=$(echo "$SCENARIOS" | jq --arg k "$KEY" --arg v "$VAL" '
            [.[] | select((.[$k] | tostring) == $v)]
        ')
    done
fi

TOTAL=$(echo "$SCENARIOS" | jq 'length')

# --- Dry run: print table and exit ---

if $DRY_RUN; then
    echo ""
    echo "Scenario file: $(basename "$SCENARIO_FILE") ($TOTAL scenarios)"
    echo ""
    printf "  %-4s %-20s %-10s %-14s %-6s %-8s %-6s\n" "#" "NAT Type" "Transport" "Servers" "Loss" "Latency" "Runs"
    for ((i=0; i<TOTAL; i++)); do
        S=$(echo "$SCENARIOS" | jq ".[$i]")
        nat=$(echo "$S" | jq -r '.nat_type')
        tr=$(echo "$S" | jq -r '.transport')
        sc=$(echo "$S" | jq -r '.server_count')
        loss=$(echo "$S" | jq -r '.packet_loss')
        lat=$(echo "$S" | jq -r '.latency_ms')
        runs=$(echo "$S" | jq -r '.runs')
        # Format server_count: show "ipfs-network" as-is, numbers as-is
        [[ "$sc" == "null" ]] && sc="?"
        printf "  %-4d %-20s %-10s %-14s %-6s %-8s %-6s\n" \
            "$((i+1))" "$nat" "$tr" "$sc" "${loss}%" "${lat}ms" "$runs"
    done
    echo ""
    exit 0
fi

# --- Execute scenarios ---

DC="docker compose -f testbed/docker/compose.yml"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="results/testbed/${SCENARIO_NAME}-${TIMESTAMP}"
mkdir -p "$RESULT_DIR"

TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_SKIP=0
SCENARIO_NUM=0

echo "=== AutoNAT v2 Experiment Runner ==="
echo "Scenario:  $SCENARIO_NAME ($(basename "$SCENARIO_FILE"))"
echo "Scenarios: $TOTAL"
echo "Output:    $RESULT_DIR/"
echo ""

for ((i=0; i<TOTAL; i++)); do
    S=$(echo "$SCENARIOS" | jq ".[$i]")
    NAT_TYPE=$(echo "$S" | jq -r '.nat_type')
    TRANSPORT=$(echo "$S" | jq -r '.transport')
    SERVER_COUNT=$(echo "$S" | jq -r '.server_count')
    PACKET_LOSS=$(echo "$S" | jq -r '.packet_loss')
    LATENCY_MS=$(echo "$S" | jq -r '.latency_ms')
    TIMEOUT=$(echo "$S" | jq -r '.timeout_s')
    RUNS=$(echo "$S" | jq -r '.runs')
    TCP_BLOCK_PORT=$(echo "$S" | jq -r '.tcp_block_port // empty')
    PORT_REMAP=$(echo "$S" | jq -r '.port_remap // empty')
    OBS_THRESH=$(echo "$S" | jq -r '.obs_addr_thresh // empty')
    HAS_ASSERTIONS=$(echo "$S" | jq '.assertions != null')

    # Compute obs_addr_thresh if not overridden
    if [[ -z "$OBS_THRESH" ]]; then
        if [[ "$SERVER_COUNT" != "ipfs-network" && "$SERVER_COUNT" -lt 4 ]]; then
            OBS_THRESH=2
        else
            OBS_THRESH=4
        fi
    fi

    # Map server_count to Docker compose profiles and numeric SERVERS value
    PROFILES=""
    SERVERS="$SERVER_COUNT"
    if [[ "$SERVER_COUNT" == "ipfs-network" ]]; then
        PROFILES="--profile public"
        SERVERS="public"
    elif [[ "$NAT_TYPE" == "none" ]]; then
        PROFILES="--profile nonat"
        if [[ "$SERVER_COUNT" -ge 5 ]]; then
            PROFILES="$PROFILES --profile 5servers"
        fi
        if [[ "$SERVER_COUNT" -ge 7 ]]; then
            PROFILES="$PROFILES --profile 7servers"
        fi
    else
        PROFILES="--profile local"
        if [[ "$SERVER_COUNT" -ge 5 ]]; then
            PROFILES="$PROFILES --profile 5servers"
        fi
        if [[ "$SERVER_COUNT" -ge 7 ]]; then
            PROFILES="$PROFILES --profile 7servers"
        fi
    fi

    # Determine client container name
    CLIENT_CONTAINER="client"
    if [[ "$SERVERS" == "public" ]]; then
        CLIENT_CONTAINER="client-public"
    elif [[ "$NAT_TYPE" == "none" ]]; then
        CLIENT_CONTAINER="client-nonat"
    fi

    for ((run=1; run<=RUNS; run++)); do
        SCENARIO_NUM=$((SCENARIO_NUM + 1))
        RUN_LABEL="${NAT_TYPE}-${TRANSPORT}-${SERVERS}"
        [[ "$PACKET_LOSS" != "0" ]] && RUN_LABEL="${RUN_LABEL}-loss${PACKET_LOSS}"
        [[ "$LATENCY_MS" != "0" ]] && RUN_LABEL="${RUN_LABEL}-lat${LATENCY_MS}"
        [[ "$RUNS" -gt 1 ]] && RUN_LABEL="${RUN_LABEL}-run${run}"

        RESULT_FILE="$RESULT_DIR/${RUN_LABEL}.json"

        echo "--- [$SCENARIO_NUM] $RUN_LABEL ---"
        echo "  NAT=$NAT_TYPE transport=$TRANSPORT servers=$SERVER_COUNT loss=${PACKET_LOSS}% latency=${LATENCY_MS}ms timeout=${TIMEOUT}s"

        # Export environment for docker compose
        export NAT_TYPE TRANSPORT PACKET_LOSS LATENCY_MS TCP_BLOCK_PORT PORT_REMAP
        export OBS_ADDR_THRESH="$OBS_THRESH"

        # Clean up from previous runs
        # shellcheck disable=SC2086
        $DC $PROFILES down --volumes --remove-orphans 2>/dev/null || true

        # Start containers
        # shellcheck disable=SC2086
        $DC $PROFILES up --build -d

        # Wait for containers to start
        sleep 5

        # Monitor logs for reachability events
        START_EPOCH=$(date +%s)
        CONVERGED=false

        while true; do
            ELAPSED=$(( $(date +%s) - START_EPOCH ))
            if [[ $ELAPSED -ge $TIMEOUT ]]; then
                echo "  Timeout (${TIMEOUT}s)"
                break
            fi

            if $DC logs "$CLIENT_CONTAINER" 2>/dev/null | grep -qE "REACHABILITY CHANGED|REACHABLE ADDRS CHANGED: reachable=[1-9]|REACHABLE ADDRS CHANGED:.*unreachable=[1-9]"; then
                CONVERGED=true
                sleep 30  # wait for v2 confidence to stabilize
                break
            fi

            sleep 2
            printf "."
        done
        echo ""

        # Show relevant logs
        $DC logs "$CLIENT_CONTAINER" 2>/dev/null | grep -E "REACHABLE|UNREACHABLE|REACHABILITY|Connected|connect_failed|peer_discovery" || true

        # Copy results
        if [[ -f "results/testbed/experiment.json" ]]; then
            cp "results/testbed/experiment.json" "$RESULT_FILE"
            rm -f "results/testbed/experiment.json"
        else
            echo "  Warning: no experiment.json found"
        fi

        # Run assertions if present
        RUN_PASS=true
        if [[ "$HAS_ASSERTIONS" == "true" && -f "$RESULT_FILE" ]]; then
            ASSERTIONS_JSON=$(echo "$S" | jq '.assertions')
            ASSERT_RESULTS=$(echo "$ASSERTIONS_JSON" | python3 "$SCRIPT_DIR/eval-assertions.py" "$RESULT_FILE")

            # Print assertion results
            echo "$ASSERT_RESULTS" | jq -r '.[] |
                if .status == "INFO" then
                    "  \(.status): \(.label): \(.value // "n/a")"
                else
                    "  \(.status): \(.message)"
                end
            '

            # Check for failures
            FAIL_COUNT=$(echo "$ASSERT_RESULTS" | jq '[.[] | select(.pass == false)] | length')
            if [[ "$FAIL_COUNT" -gt 0 ]]; then
                RUN_PASS=false
            fi

            # Save assertion results alongside experiment data
            echo "$ASSERT_RESULTS" > "${RESULT_FILE%.json}.assertions.json"
        fi

        if $RUN_PASS; then
            TOTAL_PASS=$((TOTAL_PASS + 1))
        else
            TOTAL_FAIL=$((TOTAL_FAIL + 1))
        fi

        # Tear down
        # shellcheck disable=SC2086
        $DC $PROFILES down --volumes --remove-orphans 2>/dev/null || true
        echo ""
    done
done

# --- Summary ---

echo "=== Summary: $SCENARIO_NAME ==="
echo "Total:  $SCENARIO_NUM"
echo "Passed: $TOTAL_PASS"
echo "Failed: $TOTAL_FAIL"
echo "Output: $RESULT_DIR/"

if [[ $TOTAL_FAIL -gt 0 ]]; then
    exit 1
fi
