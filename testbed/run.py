#!/usr/bin/env python3
"""YAML-driven experiment runner for AutoNAT v2 testbed.

Replaces run.sh with a more maintainable Python implementation.
Supports all existing scenario features plus dynamic mid-test toggles
for time-to-update measurements.

Usage:
    ./testbed/run.py <scenario.yaml> [options]

Options:
    --timeout=N       Override timeout per scenario (seconds)
    --runs=N          Override number of runs per scenario
    --filter=K=V,...  Filter scenarios (AND logic)
    --output=PATH     Output directory
    --dry-run         Print expanded scenarios without executing

Dependencies: docker compose, python3, pyyaml (or yq)
"""

import argparse
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMPOSE_FILE = "testbed/docker/compose.yml"
JAEGER_URL = "http://localhost:16686"
JAEGER_SERVICE = "autonat-testbed"
CLIENT_PRIVATE_IP = "10.0.1.10"
DEFAULT_PORT = 4001

VALID_NAT_TYPES = {"none", "full-cone", "address-restricted", "port-restricted", "symmetric"}
VALID_TRANSPORTS = {"tcp", "quic", "both"}
VALID_SERVER_COUNTS = {"3", "4", "5", "6", "7", "ipfs-network"}
VALID_MOCK_BEHAVIORS = {
    "reject", "refuse", "force-unreachable", "internal-error", "timeout",
    "force-reachable", "wrong-nonce", "no-dialback-msg", "probabilistic", "actual",
}
VALID_ASSERTION_TYPES = {"no_event", "has_event", "info"}
VALID_TOGGLE_ACTIONS = {"add_port_forward", "remove_port_forward"}
VALID_TOGGLE_WAITS = {"converged", "sleep"}

# Span names emitted by the Go testbed node (via emitSpan)
SPAN_REACHABLE_ADDRS = "reachable_addrs_changed"
SPAN_REACHABILITY = "reachability_changed"

# Log pattern for human-readable output (still shown from docker logs)
LOG_KEY_EVENTS = re.compile(
    r"REACHABLE|UNREACHABLE|REACHABILITY|Connected|connect_failed|peer_discovery"
)


def _parse_list_attr(val):
    """Parse a value that may be a list or a stringified JSON array."""
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val.startswith("["):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return []


# ---------------------------------------------------------------------------
# YAML loading (use pyyaml if available, else shell out to yq)
# ---------------------------------------------------------------------------

def load_yaml(path: str) -> dict:
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f)
    except ImportError:
        pass
    # Fallback to yq
    if not shutil.which("yq"):
        print("Error: pyyaml or yq is required. Install: pip install pyyaml  (or)  brew install yq")
        sys.exit(1)
    result = subprocess.run(["yq", "-o=json", path], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Docker Compose wrapper
# ---------------------------------------------------------------------------

class Compose:
    def __init__(self, compose_file: str):
        self.base = ["docker", "compose", "-f", compose_file]

    def run(self, *args, profiles: list[str] | None = None,
            capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        cmd = list(self.base)
        for p in (profiles or []):
            cmd += ["--profile", p]
        cmd += list(args)
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=check,
        )

    def up(self, profiles: list[str]):
        self.run("up", "--build", "-d", profiles=profiles)

    def down(self, profiles: list[str]):
        self.run("down", "--volumes", "--remove-orphans",
                 profiles=profiles, check=False)

    def logs(self, container: str) -> str:
        r = self.run("logs", container, capture=True, check=False)
        return r.stdout or ""

    def ps_json(self, profiles: list[str]) -> list[dict]:
        r = self.run("ps", "--format", "json", profiles=profiles,
                      capture=True, check=False)
        if not r.stdout:
            return []
        items = []
        for line in r.stdout.strip().splitlines():
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return items

    def exec_router(self, script: str):
        """Execute a bash script inside the router container."""
        self.run("exec", "-T", "router", "bash", "-c", script, check=True)


# ---------------------------------------------------------------------------
# Jaeger API client
# ---------------------------------------------------------------------------

class Jaeger:
    """Query Jaeger HTTP API for spans."""

    def __init__(self, base_url: str = JAEGER_URL, service: str = JAEGER_SERVICE):
        self.base_url = base_url.rstrip("/")
        self.service = service

    def _get(self, path: str) -> dict | None:
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            return None

    def find_spans(self, start_time: datetime | None = None) -> list[dict]:
        """Find all spans for our service since start_time.

        Returns a flat list of span dicts with keys:
            name, start_us, duration_us, attrs (dict of key->value)
        """
        params = f"query.service_name={self.service}&query.search_depth=1000"
        if start_time:
            # Jaeger v3 requires both start_time_min and start_time_max
            ts = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            params += f"&query.start_time_min={ts}"
            end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            params += f"&query.start_time_max={end}"
        else:
            # Default: last hour
            now = datetime.now(timezone.utc)
            start = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            params += f"&query.start_time_min={start}&query.start_time_max={end}"

        data = self._get(f"/api/v3/traces?{params}")
        if not data:
            return []

        spans = []
        # v3 returns {"result": {"resourceSpans": [...]}}
        resource_spans = (data.get("result", {}).get("resourceSpans", [])
                          or data.get("resourceSpans", []))
        for rs in resource_spans:
            for scope_span in rs.get("scopeSpans", []):
                for s in scope_span.get("spans", []):
                    attrs = {}
                    for a in s.get("attributes", []):
                        key = a.get("key", "")
                        val = a.get("value", {})
                        # OTel proto uses typed values
                        for vtype in ("stringValue", "intValue", "boolValue"):
                            if vtype in val:
                                attrs[key] = val[vtype]
                                break
                        if "arrayValue" in val:
                            attrs[key] = [
                                v.get("stringValue", str(v))
                                for v in val["arrayValue"].get("values", [])
                            ]
                    spans.append({
                        "name": s.get("name", ""),
                        "trace_id": s.get("traceId", ""),
                        "start_ns": s.get("startTimeUnixNano", "0"),
                        "attrs": attrs,
                    })
        return spans

    def count_spans(self, span_name: str, start_time: datetime | None = None,
                    filter_fn=None) -> int:
        """Count spans matching name and optional filter."""
        spans = self.find_spans(start_time)
        count = 0
        for s in spans:
            if s["name"] == span_name:
                if filter_fn is None or filter_fn(s):
                    count += 1
        return count

    def has_convergence_span(self, start_time: datetime | None = None) -> bool:
        """Check if any reachability convergence span exists."""
        spans = self.find_spans(start_time)
        for s in spans:
            if s["name"] in (SPAN_REACHABLE_ADDRS, SPAN_REACHABILITY):
                return True
        return False

    def export_trace_jsonl(self, start_time: datetime | None = None) -> list[dict]:
        """Export all spans as a list of dicts compatible with analyze.py's parse_trace().

        Converts Jaeger v3 OTLP format to the JSONL format that stdouttrace produced,
        so existing analysis tools work unchanged.
        """
        spans = self.find_spans(start_time)
        jsonl_spans = []
        for s in spans:
            # Convert attrs dict back to OTEL SDK format for analyze.py compatibility
            otel_attrs = []
            for k, v in s["attrs"].items():
                if isinstance(v, list):
                    otel_attrs.append({"Key": k, "Value": {"Type": "STRINGSLICE", "Value": v}})
                elif isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
                    otel_attrs.append({"Key": k, "Value": {"Type": "INT64", "Value": int(v)}})
                else:
                    otel_attrs.append({"Key": k, "Value": {"Type": "STRING", "Value": str(v)}})

            jsonl_spans.append({
                "Name": s["name"],
                "SpanContext": {"TraceID": s.get("trace_id", "")},
                "Attributes": otel_attrs,
                # New span-per-event model: no Events array, data is in Attributes
                "Events": None,
            })
        return jsonl_spans

    def wait_ready(self, timeout: int = 30):
        """Wait for Jaeger to be responsive."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._get("/api/v3/services") is not None:
                return
            time.sleep(1)
        print("  Warning: Jaeger not responding, traces may be incomplete")


# ---------------------------------------------------------------------------
# Scenario schema
# ---------------------------------------------------------------------------

@dataclass
class Toggle:
    """A mid-test action to execute during a run."""
    action: str           # add_port_forward, remove_port_forward
    wait_for: str         # converged, sleep
    sleep_s: int = 30     # seconds to sleep (when wait_for=sleep)
    timeout_s: int = 180  # max wait for convergence


@dataclass
class Assertion:
    type: str
    event: str = ""
    filter: dict = field(default_factory=dict)
    message: str = ""
    extract: str = ""
    select: str = "first"
    label: str = ""


@dataclass
class Scenario:
    nat_type: str = "none"
    transport: str = "both"
    server_count: str = "7"
    packet_loss: int = 0
    latency_ms: int = 0
    timeout_s: int = 120
    runs: int = 1
    name: str = ""
    tcp_block_port: Optional[int] = None
    port_remap: Optional[str] = None
    port_forward: Optional[bool] = None
    upnp: Optional[bool] = None
    obs_addr_thresh: Optional[int] = None
    unreliable_servers: Optional[int] = None
    observe_after_convergence_s: Optional[int] = None
    autonat_refresh: Optional[int] = None
    mock_behaviors: Optional[list[str]] = None
    mock_delays: Optional[list[int]] = None
    mock_jitters: Optional[list[int]] = None
    mock_probabilities: Optional[list[float]] = None
    mock_tcp_behaviors: Optional[list[str]] = None
    mock_quic_behaviors: Optional[list[str]] = None
    dynamic_toggles: Optional[list[Toggle]] = None
    assertions: Optional[list[Assertion]] = None


def parse_scenario(raw: dict, defaults: dict) -> Scenario:
    """Parse a raw dict into a Scenario, applying defaults."""
    d = {**defaults, **{k: v for k, v in raw.items() if v is not None}}

    toggles = None
    if "dynamic_toggles" in d and d["dynamic_toggles"] is not None:
        toggles = [
            Toggle(
                action=t["action"],
                wait_for=t.get("wait_for", "converged"),
                sleep_s=t.get("sleep_s", 30),
                timeout_s=t.get("timeout_s", 180),
            )
            for t in d["dynamic_toggles"]
        ]

    assertions = None
    if "assertions" in d and d["assertions"] is not None:
        assertions = [Assertion(**a) for a in d["assertions"]]

    return Scenario(
        nat_type=d.get("nat_type", "none"),
        transport=d.get("transport", "both"),
        server_count=str(d.get("server_count", "7")),
        packet_loss=int(d.get("packet_loss", 0)),
        latency_ms=int(d.get("latency_ms", 0)),
        timeout_s=int(d.get("timeout_s", 120)),
        runs=int(d.get("runs", 1)),
        name=d.get("name", ""),
        tcp_block_port=d.get("tcp_block_port"),
        port_remap=d.get("port_remap"),
        port_forward=d.get("port_forward"),
        upnp=d.get("upnp"),
        obs_addr_thresh=d.get("obs_addr_thresh"),
        unreliable_servers=d.get("unreliable_servers"),
        observe_after_convergence_s=d.get("observe_after_convergence_s"),
        autonat_refresh=d.get("autonat_refresh"),
        mock_behaviors=d.get("mock_behaviors"),
        mock_delays=d.get("mock_delays"),
        mock_jitters=d.get("mock_jitters"),
        mock_probabilities=d.get("mock_probabilities"),
        mock_tcp_behaviors=d.get("mock_tcp_behaviors"),
        mock_quic_behaviors=d.get("mock_quic_behaviors"),
        dynamic_toggles=toggles,
        assertions=assertions,
    )


# ---------------------------------------------------------------------------
# Scenario expansion
# ---------------------------------------------------------------------------

def expand_matrix(matrix: dict) -> list[dict]:
    """Cartesian product of matrix fields."""
    keys = list(matrix.keys())
    values = [matrix[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def expand_scenarios(yaml_data: dict) -> tuple[str, list[Scenario]]:
    """Parse YAML and return (name, list of Scenario)."""
    name = yaml_data.get("name", "unnamed")
    defaults = yaml_data.get("defaults", {})

    if "matrix" in yaml_data and "scenarios" in yaml_data:
        print("Error: YAML has both 'matrix' and 'scenarios' — use one.")
        sys.exit(1)
    if "matrix" not in yaml_data and "scenarios" not in yaml_data:
        print("Error: YAML has neither 'matrix' nor 'scenarios'.")
        sys.exit(1)

    if "matrix" in yaml_data:
        raw_list = expand_matrix(yaml_data["matrix"])
    else:
        raw_list = yaml_data["scenarios"]

    return name, [parse_scenario(r, defaults) for r in raw_list]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(scenarios: list[Scenario]):
    for i, s in enumerate(scenarios):
        pfx = f"Validation error (scenario #{i+1})"

        if s.nat_type not in VALID_NAT_TYPES:
            _fail(f"{pfx}: invalid nat_type '{s.nat_type}'")
        if s.transport not in VALID_TRANSPORTS:
            _fail(f"{pfx}: invalid transport '{s.transport}'")
        if s.mock_behaviors is None and s.server_count not in VALID_SERVER_COUNTS:
            _fail(f"{pfx}: invalid server_count '{s.server_count}'")
        if s.packet_loss < 0 or s.packet_loss > 100:
            _fail(f"{pfx}: packet_loss must be 0-100")
        if s.timeout_s < 1:
            _fail(f"{pfx}: timeout_s must be positive")
        if s.runs < 1:
            _fail(f"{pfx}: runs must be positive")
        if s.port_remap and not re.match(r"^\d+:\d+$", s.port_remap):
            _fail(f"{pfx}: port_remap must be INT:INT")
        if s.tcp_block_port is not None and not (1 <= s.tcp_block_port <= 65535):
            _fail(f"{pfx}: tcp_block_port must be 1-65535")

        if s.mock_behaviors is not None:
            if len(s.mock_behaviors) != 3:
                _fail(f"{pfx}: mock_behaviors must have 3 elements")
            for b in s.mock_behaviors:
                if b not in VALID_MOCK_BEHAVIORS:
                    _fail(f"{pfx}: invalid mock behavior '{b}'")

        if s.mock_delays is not None:
            if len(s.mock_delays) != 3:
                _fail(f"{pfx}: mock_delays must have 3 elements")
        if s.mock_jitters is not None:
            if len(s.mock_jitters) != 3:
                _fail(f"{pfx}: mock_jitters must have 3 elements")
        if s.mock_probabilities is not None:
            if len(s.mock_probabilities) != 3:
                _fail(f"{pfx}: mock_probabilities must have 3 elements")
            for p in s.mock_probabilities:
                if not (0.0 <= p <= 1.0):
                    _fail(f"{pfx}: mock_probabilities must be in [0, 1]")

        for field_name in ("mock_tcp_behaviors", "mock_quic_behaviors"):
            val = getattr(s, field_name)
            if val is not None:
                if len(val) != 3:
                    _fail(f"{pfx}: {field_name} must have 3 elements")
                for b in val:
                    if b and b not in VALID_MOCK_BEHAVIORS:
                        _fail(f"{pfx}: invalid {field_name} value '{b}'")

        if s.dynamic_toggles:
            for t in s.dynamic_toggles:
                if t.action not in VALID_TOGGLE_ACTIONS:
                    _fail(f"{pfx}: invalid toggle action '{t.action}'")
                if t.wait_for not in VALID_TOGGLE_WAITS:
                    _fail(f"{pfx}: invalid toggle wait_for '{t.wait_for}'")

        if s.assertions:
            for a in s.assertions:
                if a.type not in VALID_ASSERTION_TYPES:
                    _fail(f"{pfx}: invalid assertion type '{a.type}'")


def _fail(msg: str):
    print(msg)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def apply_filter(scenarios: list[Scenario], filter_str: str) -> list[Scenario]:
    """Filter scenarios by comma-separated key=value pairs (AND logic)."""
    if not filter_str:
        return scenarios
    filters = {}
    for pair in filter_str.split(","):
        k, v = pair.split("=", 1)
        filters[k] = v
    result = []
    for s in scenarios:
        match = True
        for k, v in filters.items():
            actual = str(getattr(s, k, ""))
            if actual != v:
                match = False
                break
        if match:
            result.append(s)
    return result


# ---------------------------------------------------------------------------
# Profile / container mapping
# ---------------------------------------------------------------------------

def get_profiles(s: Scenario) -> list[str]:
    """Map scenario to Docker Compose profiles."""
    profiles = []
    has_mock = s.mock_behaviors is not None

    if has_mock:
        profiles.append("mock")
    elif s.server_count == "ipfs-network":
        profiles.append("public")
    elif s.nat_type == "none":
        profiles.append("nonat")
    else:
        profiles.append("local")

    if not has_mock and s.server_count not in ("ipfs-network",):
        sc = int(s.server_count) if s.server_count.isdigit() else 0
        if sc >= 5:
            profiles.append("5servers")
        if sc >= 7:
            profiles.append("7servers")

    if s.unreliable_servers and s.unreliable_servers > 0:
        profiles.append("unreliable")

    return profiles


def get_client_container(s: Scenario) -> str:
    if s.mock_behaviors is not None:
        return "client-mock"
    if s.server_count == "ipfs-network":
        return "client-public"
    if s.nat_type == "none":
        return "client-nonat"
    return "client"


def get_obs_thresh(s: Scenario) -> int:
    if s.obs_addr_thresh is not None:
        return s.obs_addr_thresh
    if s.mock_behaviors is not None:
        return 2
    sc = int(s.server_count) if s.server_count.isdigit() else 99
    if sc < 4:
        return 2
    return 4


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def build_env(s: Scenario) -> dict[str, str]:
    """Build environment variables for docker compose."""
    env = {
        "NAT_TYPE": s.nat_type,
        "TRANSPORT": s.transport,
        "PACKET_LOSS": str(s.packet_loss),
        "LATENCY_MS": str(s.latency_ms),
        "TCP_BLOCK_PORT": str(s.tcp_block_port) if s.tcp_block_port else "",
        "PORT_REMAP": s.port_remap or "",
        "PORT_FORWARD": "true" if s.port_forward else "",
        "UPNP": "true" if s.upnp else "",
        "OBS_ADDR_THRESH": str(get_obs_thresh(s)),
        "AUTONAT_REFRESH": str(s.autonat_refresh or 0),
    }

    # Mock server env vars
    for i in range(3):
        idx = str(i + 1)
        env[f"MOCK_BEHAVIOR_{idx}"] = (s.mock_behaviors[i] if s.mock_behaviors else "")
        env[f"MOCK_DELAY_{idx}"] = str(s.mock_delays[i]) if s.mock_delays else "0"
        env[f"MOCK_JITTER_{idx}"] = str(s.mock_jitters[i]) if s.mock_jitters else "0"
        env[f"MOCK_PROBABILITY_{idx}"] = str(s.mock_probabilities[i]) if s.mock_probabilities else "0.5"
        env[f"MOCK_TCP_BEHAVIOR_{idx}"] = (s.mock_tcp_behaviors[i] if s.mock_tcp_behaviors else "") or ""
        env[f"MOCK_QUIC_BEHAVIOR_{idx}"] = (s.mock_quic_behaviors[i] if s.mock_quic_behaviors else "") or ""

    return env


# ---------------------------------------------------------------------------
# Convergence detection (via Jaeger spans)
# ---------------------------------------------------------------------------

def wait_for_convergence(jaeger: Jaeger, start_time: datetime,
                         timeout: int) -> tuple[bool, int]:
    """Wait for any reachability convergence span in Jaeger. Returns (converged, elapsed_s)."""
    start = time.time()
    while True:
        elapsed = int(time.time() - start)
        if elapsed >= timeout:
            print(f"  Timeout ({timeout}s)")
            return False, elapsed

        if jaeger.has_convergence_span(start_time):
            print(f"  Converged ({elapsed}s), stabilizing...")
            time.sleep(30)
            return True, elapsed

        time.sleep(3)
        print(".", end="", flush=True)


def count_jaeger_spans(jaeger: Jaeger, span_name: str,
                       start_time: datetime, filter_fn=None) -> int:
    """Count spans matching name in Jaeger since start_time."""
    return jaeger.count_spans(span_name, start_time, filter_fn)


def wait_for_new_span(jaeger: Jaeger, span_name: str, start_time: datetime,
                      prev_count: int, timeout: int,
                      description: str, filter_fn=None) -> tuple[bool, int]:
    """Wait for new spans matching name (by count increase). Returns (detected, elapsed_s)."""
    start = time.time()
    print(f"  Waiting for {description}", end="", flush=True)
    while True:
        elapsed = int(time.time() - start)
        if elapsed >= timeout:
            print(f" timeout ({timeout}s)")
            return False, elapsed

        current = count_jaeger_spans(jaeger, span_name, start_time, filter_fn)
        if current > prev_count:
            elapsed = int(time.time() - start)
            print(f" detected ({elapsed}s)")
            return True, elapsed

        time.sleep(3)
        print(".", end="", flush=True)


# ---------------------------------------------------------------------------
# Dynamic toggles (iptables changes on the router)
# ---------------------------------------------------------------------------

def exec_toggle(dc: Compose, action: str, port: int = DEFAULT_PORT):
    """Add or remove port forwarding rules on the router."""
    flag = "-I" if action == "add_port_forward" else "-D"
    # For remove, ignore errors (rules may not exist if add failed)
    suffix = " 2>/dev/null || true" if action == "remove_port_forward" else ""
    script = f"""
        PUB_IFACE=$(ip -4 addr show | grep '73\\.0\\.0\\.' | awk '{{print $NF}}')
        PRIV_IFACE=$(ip -4 addr show | grep '10\\.0\\.1\\.' | awk '{{print $NF}}')
        iptables -t nat {flag} PREROUTING -i $PUB_IFACE -p tcp --dport {port} -j DNAT --to-destination {CLIENT_PRIVATE_IP}:{port}{suffix}
        iptables -t nat {flag} PREROUTING -i $PUB_IFACE -p udp --dport {port} -j DNAT --to-destination {CLIENT_PRIVATE_IP}:{port}{suffix}
        iptables {flag} FORWARD -i $PUB_IFACE -o $PRIV_IFACE -p tcp -d {CLIENT_PRIVATE_IP} --dport {port} -j ACCEPT{suffix}
        iptables {flag} FORWARD -i $PUB_IFACE -o $PRIV_IFACE -p udp -d {CLIENT_PRIVATE_IP} --dport {port} -j ACCEPT{suffix}
    """
    dc.exec_router(script)
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"  [{ts}] toggle: {action} (port {port})")


def run_toggles(dc: Compose, jaeger: Jaeger, start_time: datetime,
                toggles: list[Toggle]) -> list[dict]:
    """Execute dynamic toggles and return timing results."""
    results = []
    for i, t in enumerate(toggles):
        phase = i + 1
        print(f"\n  --- Toggle phase {phase}: {t.action} (wait_for={t.wait_for}) ---")

        # Determine what span to watch for after this toggle.
        # Watch for either v2 per-address (reachable_addrs_changed) or v1
        # whole-node (reachability_changed), whichever fires first.
        if t.action == "add_port_forward":
            watch_span = None  # watch both span types
            filter_fn = None   # any new convergence span counts
            desc = "reachability change (reachable)"
        else:
            watch_span = None
            filter_fn = None
            desc = "reachability change (unreachable)"

        # Wait before toggling
        if t.wait_for == "converged":
            prev_count = count_jaeger_spans(jaeger, SPAN_REACHABLE_ADDRS, start_time)
            ok, wait_elapsed = wait_for_new_span(
                jaeger, SPAN_REACHABLE_ADDRS, start_time,
                prev_count=0,  # any existing span counts as converged
                timeout=t.timeout_s,
                description="initial convergence",
            )
            if ok:
                print(f"  Stabilizing {t.sleep_s}s before toggle...")
                time.sleep(t.sleep_s)
        elif t.wait_for == "sleep":
            print(f"  Sleeping {t.sleep_s}s before toggle...")
            time.sleep(t.sleep_s)

        # Record state before toggle — count both v1 and v2 convergence spans
        prev_v1 = count_jaeger_spans(jaeger, SPAN_REACHABILITY, start_time)
        prev_v2 = count_jaeger_spans(jaeger, SPAN_REACHABLE_ADDRS, start_time)
        toggle_epoch = time.time()

        # Execute the toggle
        exec_toggle(dc, t.action)

        # Wait for either v1 or v2 to detect the change
        start_wait = time.time()
        detected = False
        elapsed = 0
        while True:
            elapsed = int(time.time() - start_wait)
            if elapsed >= t.timeout_s:
                print(f" timeout ({t.timeout_s}s)")
                break
            cur_v1 = count_jaeger_spans(jaeger, SPAN_REACHABILITY, start_time)
            cur_v2 = count_jaeger_spans(jaeger, SPAN_REACHABLE_ADDRS, start_time)
            if cur_v1 > prev_v1 or cur_v2 > prev_v2:
                which = "v1" if cur_v1 > prev_v1 else "v2"
                print(f" detected via {which} ({elapsed}s)")
                detected = True
                break
            time.sleep(3)
            print(".", end="", flush=True)

        results.append({
            "phase": phase,
            "action": t.action,
            "detected": detected,
            "time_to_detect_s": elapsed,
            "toggle_utc": datetime.fromtimestamp(toggle_epoch, tz=timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    return results


# ---------------------------------------------------------------------------
# Assertion evaluation (reuse eval-assertions.py)
# ---------------------------------------------------------------------------

def run_assertions(assertions: list[Assertion], trace_path: str,
                   script_dir: str) -> tuple[list[dict], int]:
    """Run assertions against trace file. Returns (results, fail_count)."""
    eval_script = os.path.join(script_dir, "eval-assertions.py")
    if not os.path.exists(eval_script):
        print(f"  Warning: {eval_script} not found, skipping assertions")
        return [], 0

    assertions_json = json.dumps([
        {k: v for k, v in a.__dict__.items() if v} for a in assertions
    ])

    result = subprocess.run(
        ["python3", eval_script, trace_path],
        input=assertions_json,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Warning: assertion evaluation failed: {result.stderr}")
        return [], 0

    try:
        results = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  Warning: could not parse assertion results")
        return [], 0

    fail_count = 0
    for r in results:
        if r.get("status") == "INFO":
            print(f"  {r['status']}: {r.get('label', '')}: {r.get('value', 'n/a')}")
        else:
            print(f"  {r['status']}: {r.get('message', '')}")
        if not r.get("pass", True):
            fail_count += 1

    return results, fail_count


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def dry_run(scenario_name: str, scenarios: list[Scenario]):
    print(f"\nScenario file: {scenario_name} ({len(scenarios)} scenarios)\n")
    fmt = "  {:<4} {:<20} {:<10} {:<10} {:<30} {:<6} {:<8} {:<6} {:<8}"
    print(fmt.format("#", "NAT Type", "Transport", "Servers", "Mock Behaviors",
                      "Loss", "Latency", "Runs", "Toggles"))
    for i, s in enumerate(scenarios):
        mock = ",".join(s.mock_behaviors) if s.mock_behaviors else "-"
        sc = "mock(3)" if s.mock_behaviors else s.server_count
        toggles = str(len(s.dynamic_toggles)) if s.dynamic_toggles else "-"
        print(fmt.format(
            i + 1, s.nat_type, s.transport, sc, mock,
            f"{s.packet_loss}%", f"{s.latency_ms}ms", s.runs, toggles,
        ))
    print()


# ---------------------------------------------------------------------------
# Wait for healthy containers
# ---------------------------------------------------------------------------

def wait_for_healthy(dc: Compose, profiles: list[str], timeout: int = 60):
    print("  Waiting for servers", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = dc.ps_json(profiles)
        not_healthy = sum(
            1 for item in items
            if item.get("Health") and item.get("Health") != "healthy"
        )
        if not_healthy == 0:
            print(" ready")
            return
        time.sleep(2)
        print(".", end="", flush=True)
    print(" (timeout, proceeding)")


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------

def run_scenario(dc: Compose, jaeger: Jaeger, s: Scenario, run_num: int,
                 result_file: str, script_dir: str) -> bool:
    """Execute a single scenario run. Returns True if passed."""
    profiles = get_profiles(s)
    container = get_client_container(s)
    env = build_env(s)

    # Export env vars for docker compose
    os.environ.update(env)

    # Clean up previous run
    dc.down(profiles)

    # Record start time for Jaeger queries
    run_start = datetime.now(timezone.utc)

    # Start containers
    dc.up(profiles)

    # Wait for Jaeger and servers to be ready
    jaeger.wait_ready()
    wait_for_healthy(dc, profiles)

    # Monitor for convergence via Jaeger spans
    converged, elapsed = wait_for_convergence(jaeger, run_start, s.timeout_s)
    print()

    # Optional: continue observing after convergence to capture oscillation
    if converged and s.observe_after_convergence_s:
        obs = s.observe_after_convergence_s
        print(f"  Observing for {obs}s after convergence...")
        time.sleep(obs)
        print(f"  Observation complete (total {elapsed + obs + 30}s)")

    # Execute dynamic toggles if present
    toggle_results = []
    if s.dynamic_toggles:
        toggle_results = run_toggles(dc, jaeger, run_start, s.dynamic_toggles)
        print()

    # Show relevant log lines from docker (human-readable output)
    logs = dc.logs(container)
    for line in logs.splitlines():
        if LOG_KEY_EVENTS.search(line):
            print(f"  {line.rstrip()}")

    # Export traces from Jaeger and save as JSONL (compatible with analyze.py)
    trace_spans = jaeger.export_trace_jsonl(run_start)
    if trace_spans:
        with open(result_file, "w") as f:
            for span in trace_spans:
                f.write(json.dumps(span) + "\n")
        print(f"  Exported {len(trace_spans)} spans from Jaeger to {result_file}")
    else:
        print("  Warning: no spans found in Jaeger")

    # Save toggle results if present
    if toggle_results:
        toggle_file = result_file.replace(".json", ".toggles.json")
        with open(toggle_file, "w") as f:
            json.dump(toggle_results, f, indent=2)
        print(f"  Toggle results: {toggle_file}")
        for tr in toggle_results:
            status = "detected" if tr["detected"] else "NOT detected"
            print(f"    Phase {tr['phase']} ({tr['action']}): "
                  f"{status} in {tr['time_to_detect_s']}s")

    # Run assertions against exported trace file
    run_pass = True
    if s.assertions and os.path.exists(result_file):
        results, fail_count = run_assertions(s.assertions, result_file, script_dir)
        if fail_count > 0:
            run_pass = False
        # Save assertion results
        assert_file = result_file.replace(".json", ".assertions.json")
        with open(assert_file, "w") as f:
            json.dump(results, f, indent=2)

    # Tear down
    dc.down(profiles)

    return run_pass


def build_run_label(s: Scenario, run_num: int) -> str:
    if s.name:
        label = s.name
    else:
        sc = "mock" if s.mock_behaviors else s.server_count
        label = f"{s.nat_type}-{s.transport}-{sc}"
    if s.packet_loss:
        label += f"-loss{s.packet_loss}"
    if s.latency_ms:
        label += f"-lat{s.latency_ms}"
    if s.runs > 1:
        label += f"-run{run_num}"
    return label


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AutoNAT v2 experiment runner")
    parser.add_argument("scenario_file", help="YAML scenario file")
    parser.add_argument("--timeout", type=int, default=None, help="Override timeout")
    parser.add_argument("--runs", type=int, default=None, help="Override runs")
    parser.add_argument("--filter", default="", help="Filter scenarios (K=V,...)")
    parser.add_argument("--output", default="", help="Output directory")
    parser.add_argument("--dry-run", action="store_true", help="Print scenarios only")
    args = parser.parse_args()

    # Ensure we're in the project root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    os.chdir(project_dir)

    # Load and expand scenarios
    yaml_data = load_yaml(args.scenario_file)
    scenario_name, scenarios = expand_scenarios(yaml_data)

    # Validate
    validate(scenarios)

    # Apply overrides
    if args.timeout:
        for s in scenarios:
            s.timeout_s = args.timeout
    if args.runs:
        for s in scenarios:
            s.runs = args.runs

    # Filter
    scenarios = apply_filter(scenarios, args.filter)

    if args.dry_run:
        dry_run(scenario_name, scenarios)
        return

    if not scenarios:
        print("No scenarios match the filter.")
        sys.exit(0)

    # Check docker compose
    if not shutil.which("docker"):
        print("Error: docker is required")
        sys.exit(1)

    # Setup output
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_dir = args.output or f"results/testbed/{scenario_name}-{timestamp}"
    os.makedirs(result_dir, exist_ok=True)

    dc = Compose(COMPOSE_FILE)
    jaeger = Jaeger()
    total_pass = 0
    total_fail = 0
    scenario_num = 0

    print(f"=== AutoNAT v2 Experiment Runner ===")
    print(f"Scenario:  {scenario_name} ({os.path.basename(args.scenario_file)})")
    print(f"Scenarios: {len(scenarios)}")
    print(f"Output:    {result_dir}/")
    print()

    for s in scenarios:
        for run_num in range(1, s.runs + 1):
            scenario_num += 1
            label = build_run_label(s, run_num)
            result_file = os.path.join(result_dir, f"{label}.json")

            print(f"--- [{scenario_num}] {label} ---")
            if s.mock_behaviors:
                print(f"  mock_behaviors=[{','.join(s.mock_behaviors)}] "
                      f"transport={s.transport} timeout={s.timeout_s}s")
            else:
                toggles_info = ""
                if s.dynamic_toggles:
                    toggles_info = f" toggles={len(s.dynamic_toggles)}"
                print(f"  NAT={s.nat_type} transport={s.transport} "
                      f"servers={s.server_count} loss={s.packet_loss}% "
                      f"latency={s.latency_ms}ms timeout={s.timeout_s}s{toggles_info}")

            passed = run_scenario(dc, jaeger, s, run_num, result_file, script_dir)

            if passed:
                total_pass += 1
            else:
                total_fail += 1
            print()

    # Summary
    print(f"=== Summary: {scenario_name} ===")
    print(f"Total:  {scenario_num}")
    print(f"Passed: {total_pass}")
    print(f"Failed: {total_fail}")
    if scenario_num > 0:
        fnr = total_fail / scenario_num
        print(f"FNR:    {fnr:.4f}  ({total_fail} false negatives out of {scenario_num} runs)")
    print(f"Output: {result_dir}/")

    if total_fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
