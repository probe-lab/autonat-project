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
#   --output=PATH     Output directory (default: results/testbed/<name>-<timestamp>)
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
OPT_OUTPUT=""
DRY_RUN=false

for arg in "$@"; do
    case "$arg" in
        --timeout=*)  OPT_TIMEOUT="${arg#*=}" ;;
        --runs=*)     OPT_RUNS="${arg#*=}" ;;
        --filter=*)   OPT_FILTER="${arg#*=}" ;;
        --output=*)   OPT_OUTPUT="${arg#*=}" ;;
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
        mock_behaviors: (.mock_behaviors // null),
        mock_delays: (.mock_delays // null),
        mock_jitters: (.mock_jitters // null),
        mock_probabilities: (.mock_probabilities // null),
        mock_tcp_behaviors: (.mock_tcp_behaviors // null),
        mock_quic_behaviors: (.mock_quic_behaviors // null),
        assertions: (.assertions // null)
    }]
')

# --- Validate scenario structure and field values ---

# Check that exactly one of matrix/scenarios is present
HAS_MATRIX=$(echo "$YAML_JSON" | jq 'has("matrix")')
HAS_SCENARIOS=$(echo "$YAML_JSON" | jq 'has("scenarios")')
if [[ "$HAS_MATRIX" == "true" && "$HAS_SCENARIOS" == "true" ]]; then
    echo "Validation error: YAML file has both 'matrix' and 'scenarios' — use one or the other."
    exit 1
fi
if [[ "$HAS_MATRIX" == "false" && "$HAS_SCENARIOS" == "false" ]]; then
    echo "Validation error: YAML file has neither 'matrix' nor 'scenarios' — one is required."
    exit 1
fi

# If matrix mode, verify all values are arrays
if [[ "$HAS_MATRIX" == "true" ]]; then
    NON_ARRAYS=$(echo "$YAML_JSON" | jq -r '.matrix | to_entries[] | select(.value | type != "array") | .key')
    if [[ -n "$NON_ARRAYS" ]]; then
        echo "Validation error: matrix fields must be arrays. Non-array fields: $NON_ARRAYS"
        exit 1
    fi
fi

validate_scenario() {
    local idx=$1
    local s=$2
    local prefix="Validation error (scenario #$((idx+1)))"

    # nat_type
    local nat_type
    nat_type=$(echo "$s" | jq -r '.nat_type // empty')
    if [[ -n "$nat_type" ]]; then
        case "$nat_type" in
            none|full-cone|address-restricted|port-restricted|symmetric) ;;
            *) echo "$prefix: invalid nat_type '$nat_type' (expected: none, full-cone, address-restricted, port-restricted, symmetric)"; exit 1 ;;
        esac
    fi

    # transport
    local transport
    transport=$(echo "$s" | jq -r '.transport // empty')
    if [[ -n "$transport" ]]; then
        case "$transport" in
            tcp|quic|both) ;;
            *) echo "$prefix: invalid transport '$transport' (expected: tcp, quic, both)"; exit 1 ;;
        esac
    fi

    # server_count (only validate if mock_behaviors is not set)
    local has_mock
    has_mock=$(echo "$s" | jq '.mock_behaviors != null')
    if [[ "$has_mock" == "false" ]]; then
        local server_count
        server_count=$(echo "$s" | jq -r '.server_count // empty')
        if [[ -n "$server_count" ]]; then
            case "$server_count" in
                3|4|5|6|7|ipfs-network) ;;
                *) echo "$prefix: invalid server_count '$server_count' (expected: integer 3-7 or 'ipfs-network')"; exit 1 ;;
            esac
        fi
    fi

    # mock_behaviors: must be array of exactly 3 valid behavior strings
    if [[ "$has_mock" == "true" ]]; then
        local mock_len
        mock_len=$(echo "$s" | jq '.mock_behaviors | length')
        if [[ "$mock_len" -ne 3 ]]; then
            echo "$prefix: mock_behaviors must have exactly 3 elements (got $mock_len)"
            exit 1
        fi
        local invalid_behaviors
        invalid_behaviors=$(echo "$s" | jq -r '.mock_behaviors[] | select(. as $b | ["reject","refuse","force-unreachable","internal-error","timeout","force-reachable","wrong-nonce","no-dialback-msg"] | index($b) | not)')
        if [[ -n "$invalid_behaviors" ]]; then
            echo "$prefix: invalid mock_behaviors value(s): $invalid_behaviors"
            echo "  Valid: reject, refuse, force-unreachable, internal-error, timeout, force-reachable, wrong-nonce, no-dialback-msg"
            exit 1
        fi
    fi

    # mock_delays: must be array of exactly 3 non-negative integers
    local has_delays
    has_delays=$(echo "$s" | jq '.mock_delays != null')
    if [[ "$has_delays" == "true" ]]; then
        local delays_len
        delays_len=$(echo "$s" | jq '.mock_delays | length')
        if [[ "$delays_len" -ne 3 ]]; then
            echo "$prefix: mock_delays must have exactly 3 elements (got $delays_len)"
            exit 1
        fi
        local invalid_delays
        invalid_delays=$(echo "$s" | jq -r '.mock_delays[] | select(type != "number" or . < 0 or . != (. | floor))')
        if [[ -n "$invalid_delays" ]]; then
            echo "$prefix: mock_delays values must be non-negative integers (got invalid: $invalid_delays)"
            exit 1
        fi
    fi

    # mock_jitters: must be array of exactly 3 non-negative integers
    local has_jitters
    has_jitters=$(echo "$s" | jq '.mock_jitters != null')
    if [[ "$has_jitters" == "true" ]]; then
        local jitters_len
        jitters_len=$(echo "$s" | jq '.mock_jitters | length')
        if [[ "$jitters_len" -ne 3 ]]; then
            echo "$prefix: mock_jitters must have exactly 3 elements (got $jitters_len)"
            exit 1
        fi
        local invalid_jitters
        invalid_jitters=$(echo "$s" | jq -r '.mock_jitters[] | select(type != "number" or . < 0 or . != (. | floor))')
        if [[ -n "$invalid_jitters" ]]; then
            echo "$prefix: mock_jitters values must be non-negative integers (got invalid: $invalid_jitters)"
            exit 1
        fi
    fi

    # mock_probabilities: must be array of exactly 3 numbers in [0.0, 1.0]
    local has_probs
    has_probs=$(echo "$s" | jq '.mock_probabilities != null')
    if [[ "$has_probs" == "true" ]]; then
        local probs_len
        probs_len=$(echo "$s" | jq '.mock_probabilities | length')
        if [[ "$probs_len" -ne 3 ]]; then
            echo "$prefix: mock_probabilities must have exactly 3 elements (got $probs_len)"
            exit 1
        fi
        local invalid_probs
        invalid_probs=$(echo "$s" | jq -r '.mock_probabilities[] | select(type != "number" or . < 0 or . > 1)')
        if [[ -n "$invalid_probs" ]]; then
            echo "$prefix: mock_probabilities values must be numbers in [0.0, 1.0] (got invalid: $invalid_probs)"
            exit 1
        fi
    fi

    # mock_tcp_behaviors / mock_quic_behaviors: optional array of 3 valid behavior strings or nulls
    for field in mock_tcp_behaviors mock_quic_behaviors; do
        local has_field
        has_field=$(echo "$s" | jq ".$field != null")
        if [[ "$has_field" == "true" ]]; then
            local field_len
            field_len=$(echo "$s" | jq ".$field | length")
            if [[ "$field_len" -ne 3 ]]; then
                echo "$prefix: $field must have exactly 3 elements (got $field_len)"
                exit 1
            fi
            local invalid_field
            invalid_field=$(echo "$s" | jq -r ".$field[] | select(. != null) | select(. as \$b | [\"reject\",\"refuse\",\"force-unreachable\",\"internal-error\",\"timeout\",\"force-reachable\",\"wrong-nonce\",\"no-dialback-msg\",\"probabilistic\",\"actual\"] | index(\$b) | not)")
            if [[ -n "$invalid_field" ]]; then
                echo "$prefix: invalid $field value(s): $invalid_field"
                exit 1
            fi
        fi
    done

    # port_remap: must match INT:INT format
    local port_remap
    port_remap=$(echo "$s" | jq -r '.port_remap // empty')
    if [[ -n "$port_remap" ]]; then
        if ! [[ "$port_remap" =~ ^[0-9]+:[0-9]+$ ]]; then
            echo "$prefix: invalid port_remap '$port_remap' (expected format: 'INT:INT', e.g. '4001:29538')"
            exit 1
        fi
    fi

    # tcp_block_port: must be valid port number 1-65535
    local tcp_block_port
    tcp_block_port=$(echo "$s" | jq -r '.tcp_block_port // empty')
    if [[ -n "$tcp_block_port" ]]; then
        if ! [[ "$tcp_block_port" =~ ^[0-9]+$ ]] || [[ "$tcp_block_port" -lt 1 || "$tcp_block_port" -gt 65535 ]]; then
            echo "$prefix: invalid tcp_block_port '$tcp_block_port' (expected: integer 1-65535)"
            exit 1
        fi
    fi

    # packet_loss: must be 0-100
    local packet_loss
    packet_loss=$(echo "$s" | jq -r '.packet_loss // 0')
    if ! [[ "$packet_loss" =~ ^[0-9]+$ ]] || [[ "$packet_loss" -lt 0 || "$packet_loss" -gt 100 ]]; then
        echo "$prefix: invalid packet_loss '$packet_loss' (expected: integer 0-100)"
        exit 1
    fi

    # timeout_s: must be positive integer
    local timeout_s
    timeout_s=$(echo "$s" | jq -r '.timeout_s')
    if ! [[ "$timeout_s" =~ ^[0-9]+$ ]] || [[ "$timeout_s" -lt 1 ]]; then
        echo "$prefix: invalid timeout_s '$timeout_s' (expected: positive integer)"
        exit 1
    fi

    # runs: must be positive integer
    local runs
    runs=$(echo "$s" | jq -r '.runs')
    if ! [[ "$runs" =~ ^[0-9]+$ ]] || [[ "$runs" -lt 1 ]]; then
        echo "$prefix: invalid runs '$runs' (expected: positive integer)"
        exit 1
    fi

    # assertions: validate types if present
    local has_assertions
    has_assertions=$(echo "$s" | jq '.assertions != null')
    if [[ "$has_assertions" == "true" ]]; then
        local invalid_types
        invalid_types=$(echo "$s" | jq -r '.assertions[].type | select(. as $t | ["no_event","has_event","info"] | index($t) | not)')
        if [[ -n "$invalid_types" ]]; then
            echo "$prefix: invalid assertion type(s): $invalid_types (expected: no_event, has_event, info)"
            exit 1
        fi
    fi
}

# Validate each expanded scenario
for ((vi=0; vi<$(echo "$SCENARIOS" | jq 'length'); vi++)); do
    validate_scenario "$vi" "$(echo "$SCENARIOS" | jq ".[$vi]")"
done

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
    printf "  %-4s %-20s %-10s %-14s %-30s %-6s %-8s %-6s\n" "#" "NAT Type" "Transport" "Servers" "Mock Behaviors" "Loss" "Latency" "Runs"
    for ((i=0; i<TOTAL; i++)); do
        S=$(echo "$SCENARIOS" | jq ".[$i]")
        nat=$(echo "$S" | jq -r '.nat_type')
        tr=$(echo "$S" | jq -r '.transport')
        sc=$(echo "$S" | jq -r '.server_count')
        mock=$(echo "$S" | jq -r 'if .mock_behaviors then (.mock_behaviors | join(",")) else "-" end')
        loss=$(echo "$S" | jq -r '.packet_loss')
        lat=$(echo "$S" | jq -r '.latency_ms')
        runs=$(echo "$S" | jq -r '.runs')
        # Format server_count: show "ipfs-network" as-is, "mock" for mock scenarios, numbers as-is
        if [[ "$mock" != "-" ]]; then
            sc="mock(3)"
        elif [[ "$sc" == "null" ]]; then
            sc="?"
        fi
        printf "  %-4d %-20s %-10s %-14s %-30s %-6s %-8s %-6s\n" \
            "$((i+1))" "$nat" "$tr" "$sc" "$mock" "${loss}%" "${lat}ms" "$runs"
    done
    echo ""
    exit 0
fi

# --- Execute scenarios ---

DC="docker compose -f testbed/docker/compose.yml"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -n "$OPT_OUTPUT" ]]; then
    RESULT_DIR="$OPT_OUTPUT"
else
    RESULT_DIR="results/testbed/${SCENARIO_NAME}-${TIMESTAMP}"
fi
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
    HAS_MOCK=$(echo "$S" | jq '.mock_behaviors != null')

    # Extract mock behaviors and per-server options (arrays → per-server env vars)
    MOCK_BEHAVIOR_1="" MOCK_BEHAVIOR_2="" MOCK_BEHAVIOR_3=""
    MOCK_DELAY_1="" MOCK_DELAY_2="" MOCK_DELAY_3=""
    MOCK_JITTER_1="" MOCK_JITTER_2="" MOCK_JITTER_3=""
    MOCK_PROBABILITY_1="" MOCK_PROBABILITY_2="" MOCK_PROBABILITY_3=""
    MOCK_TCP_BEHAVIOR_1="" MOCK_TCP_BEHAVIOR_2="" MOCK_TCP_BEHAVIOR_3=""
    MOCK_QUIC_BEHAVIOR_1="" MOCK_QUIC_BEHAVIOR_2="" MOCK_QUIC_BEHAVIOR_3=""
    if [[ "$HAS_MOCK" == "true" ]]; then
        MOCK_BEHAVIOR_1=$(echo "$S" | jq -r '.mock_behaviors[0] // "force-unreachable"')
        MOCK_BEHAVIOR_2=$(echo "$S" | jq -r '.mock_behaviors[1] // "force-unreachable"')
        MOCK_BEHAVIOR_3=$(echo "$S" | jq -r '.mock_behaviors[2] // "force-unreachable"')
        MOCK_DELAY_1=$(echo "$S" | jq -r '.mock_delays[0] // 0')
        MOCK_DELAY_2=$(echo "$S" | jq -r '.mock_delays[1] // 0')
        MOCK_DELAY_3=$(echo "$S" | jq -r '.mock_delays[2] // 0')
        MOCK_JITTER_1=$(echo "$S" | jq -r '.mock_jitters[0] // 0')
        MOCK_JITTER_2=$(echo "$S" | jq -r '.mock_jitters[1] // 0')
        MOCK_JITTER_3=$(echo "$S" | jq -r '.mock_jitters[2] // 0')
        MOCK_PROBABILITY_1=$(echo "$S" | jq -r '.mock_probabilities[0] // 0.5')
        MOCK_PROBABILITY_2=$(echo "$S" | jq -r '.mock_probabilities[1] // 0.5')
        MOCK_PROBABILITY_3=$(echo "$S" | jq -r '.mock_probabilities[2] // 0.5')
        MOCK_TCP_BEHAVIOR_1=$(echo "$S" | jq -r '.mock_tcp_behaviors[0] // ""')
        MOCK_TCP_BEHAVIOR_2=$(echo "$S" | jq -r '.mock_tcp_behaviors[1] // ""')
        MOCK_TCP_BEHAVIOR_3=$(echo "$S" | jq -r '.mock_tcp_behaviors[2] // ""')
        MOCK_QUIC_BEHAVIOR_1=$(echo "$S" | jq -r '.mock_quic_behaviors[0] // ""')
        MOCK_QUIC_BEHAVIOR_2=$(echo "$S" | jq -r '.mock_quic_behaviors[1] // ""')
        MOCK_QUIC_BEHAVIOR_3=$(echo "$S" | jq -r '.mock_quic_behaviors[2] // ""')
    fi

    # Compute obs_addr_thresh if not overridden
    if [[ -z "$OBS_THRESH" ]]; then
        if [[ "$HAS_MOCK" == "true" ]]; then
            OBS_THRESH=2
        elif [[ "$SERVER_COUNT" != "ipfs-network" && "$SERVER_COUNT" -lt 4 ]]; then
            OBS_THRESH=2
        else
            OBS_THRESH=4
        fi
    fi

    # Map server_count to Docker compose profiles and numeric SERVERS value
    PROFILES=""
    SERVERS="$SERVER_COUNT"
    if [[ "$HAS_MOCK" == "true" ]]; then
        PROFILES="--profile mock"
        SERVERS="mock"
    elif [[ "$SERVER_COUNT" == "ipfs-network" ]]; then
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
    if [[ "$HAS_MOCK" == "true" ]]; then
        CLIENT_CONTAINER="client-mock"
    elif [[ "$SERVERS" == "public" ]]; then
        CLIENT_CONTAINER="client-public"
    elif [[ "$NAT_TYPE" == "none" ]]; then
        CLIENT_CONTAINER="client-nonat"
    fi

    for ((run=1; run<=RUNS; run++)); do
        SCENARIO_NUM=$((SCENARIO_NUM + 1))

        # Build run label
        SCENARIO_NAME_FIELD=$(echo "$S" | jq -r '.name // empty')
        if [[ -n "$SCENARIO_NAME_FIELD" ]]; then
            RUN_LABEL="$SCENARIO_NAME_FIELD"
        else
            RUN_LABEL="${NAT_TYPE}-${TRANSPORT}-${SERVERS}"
        fi
        [[ "$PACKET_LOSS" != "0" ]] && RUN_LABEL="${RUN_LABEL}-loss${PACKET_LOSS}"
        [[ "$LATENCY_MS" != "0" ]] && RUN_LABEL="${RUN_LABEL}-lat${LATENCY_MS}"
        [[ "$RUNS" -gt 1 ]] && RUN_LABEL="${RUN_LABEL}-run${run}"

        RESULT_FILE="$RESULT_DIR/${RUN_LABEL}.json"

        echo "--- [$SCENARIO_NUM] $RUN_LABEL ---"
        if [[ "$HAS_MOCK" == "true" ]]; then
            echo "  mock_behaviors=[$MOCK_BEHAVIOR_1,$MOCK_BEHAVIOR_2,$MOCK_BEHAVIOR_3] transport=$TRANSPORT timeout=${TIMEOUT}s"
        else
            echo "  NAT=$NAT_TYPE transport=$TRANSPORT servers=$SERVER_COUNT loss=${PACKET_LOSS}% latency=${LATENCY_MS}ms timeout=${TIMEOUT}s"
        fi

        # Export environment for docker compose
        export NAT_TYPE TRANSPORT PACKET_LOSS LATENCY_MS TCP_BLOCK_PORT PORT_REMAP
        export OBS_ADDR_THRESH="$OBS_THRESH"
        export MOCK_BEHAVIOR_1 MOCK_BEHAVIOR_2 MOCK_BEHAVIOR_3
        export MOCK_DELAY_1 MOCK_DELAY_2 MOCK_DELAY_3
        export MOCK_JITTER_1 MOCK_JITTER_2 MOCK_JITTER_3
        export MOCK_PROBABILITY_1 MOCK_PROBABILITY_2 MOCK_PROBABILITY_3
        export MOCK_TCP_BEHAVIOR_1 MOCK_TCP_BEHAVIOR_2 MOCK_TCP_BEHAVIOR_3
        export MOCK_QUIC_BEHAVIOR_1 MOCK_QUIC_BEHAVIOR_2 MOCK_QUIC_BEHAVIOR_3

        # Clean up from previous runs
        # shellcheck disable=SC2086
        $DC $PROFILES down --volumes --remove-orphans 2>/dev/null || true

        # Start containers
        # shellcheck disable=SC2086
        $DC $PROFILES up --build -d

        # Wait for server containers to become healthy (addr file written = node ready)
        printf "  Waiting for servers"
        HEALTH_DEADLINE=$(( $(date +%s) + 60 ))
        while true; do
            if [[ $(date +%s) -ge $HEALTH_DEADLINE ]]; then
                echo " (timeout, proceeding anyway)"
                break
            fi
            # Count containers with a healthcheck that haven't reached healthy yet
            NOT_HEALTHY=$(
                # shellcheck disable=SC2086
                $DC $PROFILES ps --format json 2>/dev/null \
                | jq -r 'select(.Health != "" and .Health != "healthy") | .Service' \
                2>/dev/null | wc -l | tr -d ' '
            )
            if [[ "$NOT_HEALTHY" == "0" ]]; then
                echo " ready"
                break
            fi
            sleep 2
            printf "."
        done

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

        # Copy trace results
        if [[ -f "results/testbed/trace.json" ]]; then
            cp "results/testbed/trace.json" "$RESULT_FILE"
            rm -f "results/testbed/trace.json"
        else
            echo "  Warning: no trace.json found"
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
if [[ $SCENARIO_NUM -gt 0 ]]; then
    FNR=$(awk "BEGIN { printf \"%.4f\", $TOTAL_FAIL / $SCENARIO_NUM }")
    echo "FNR:    $FNR  ($TOTAL_FAIL false negatives out of $SCENARIO_NUM runs)"
fi
echo "Output: $RESULT_DIR/"

if [[ $TOTAL_FAIL -gt 0 ]]; then
    exit 1
fi
