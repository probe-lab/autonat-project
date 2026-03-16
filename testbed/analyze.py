#!/usr/bin/env python3
"""Analyze AutoNAT v2 trace files and compute experiment metrics.

Usage:
  python3 testbed/analyze.py [--metric METRIC[,METRIC...]] [--output FORMAT] <trace.json> [...]

Arguments:
  trace.json ...   One or more OTEL trace JSONL files (one span per line).

Options:
  --metric METRIC  Comma-separated list of metrics to compute. Default: all.
                   Valid: fnr, fpr, time_to_confidence, time_to_update,
                         protocol_overhead, all
  --output FORMAT  Output format: text (default) or json.

Examples:
  python3 testbed/analyze.py results/testbed/fnr-run/*.json
  python3 testbed/analyze.py --metric fnr,time_to_confidence results/*.json
  python3 testbed/analyze.py --output json results/run.json | jq .
"""

import json
import sys
import statistics
from pathlib import Path


# ---------------------------------------------------------------------------
# OTel trace parsing (shared with eval-assertions.py)
# ---------------------------------------------------------------------------

def _extract_value(v):
    if isinstance(v, dict):
        return v.get("Value", v)
    return v


def _flatten_attrs(attrs):
    out = {}
    if not attrs:
        return out
    for attr in attrs:
        key = attr.get("Key", "")
        val = _extract_value(attr.get("Value", {}))
        out[key] = val
    return out


def _coerce_int(v):
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_trace(path):
    """Parse a JSONL trace file.

    Returns a dict:
      {
        "session": {attrs, events: [{name, time, attrs}]},
        "refresh_cycles": [{attrs, events}],
        "probes": [{attrs, events}],
        "server_selections": [{attrs}],
      }

    Supports two formats:
    - Old format: testbed events are Events[] on the autonat.session span
    - New format (span-per-event): testbed events are individual top-level spans
      (e.g. "reachable_addrs_changed", "reachability_changed", "connected", etc.)
      These are converted to session events for backward compatibility.
    """
    # Known span-per-event names from main.go emitSpan() calls
    TESTBED_SPAN_NAMES = {
        "started", "shutdown", "reachability_changed", "reachable_addrs_changed",
        "nat_device_type_changed", "addresses_updated", "local_protocols_updated",
        "auto_relay_addrs_updated", "connected", "connect_failed",
        "bootstrap_start", "bootstrap_done", "bootstrap_error",
        "bootstrap_connected", "peer_discovery_start", "peer_discovery_done",
        "peer_discovery_timeout",
    }

    result = {
        "session": None,
        "refresh_cycles": [],
        "probes": [],
        "server_selections": [],
    }
    # Collect span-per-event spans to merge into session events
    extra_events = []

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                continue

            name = span.get("Name", "")
            attrs = _flatten_attrs(span.get("Attributes", []))
            raw_events = span.get("Events") or []
            events = [
                {
                    "name": e.get("Name", ""),
                    "time": e.get("Time", ""),
                    "attrs": _flatten_attrs(e.get("Attributes", [])),
                }
                for e in raw_events
            ]

            if name == "autonat.session":
                result["session"] = {"attrs": attrs, "events": events}
            elif name == "autonatv2.refresh_cycle":
                result["refresh_cycles"].append({"attrs": attrs, "events": events})
            elif name == "autonatv2.probe":
                result["probes"].append({"attrs": attrs, "events": events})
            elif name == "autonatv2.server_selection":
                result["server_selections"].append({"attrs": attrs})
            elif name in TESTBED_SPAN_NAMES:
                # New span-per-event format: convert to session event
                extra_events.append({
                    "name": name,
                    "time": span.get("StartTime", ""),
                    "attrs": attrs,
                })

    # If we found span-per-event spans, merge them into the session
    if extra_events:
        if result["session"] is None:
            result["session"] = {"attrs": {}, "events": []}
        result["session"]["events"].extend(extra_events)

    return result


# ---------------------------------------------------------------------------
# Metric computation helpers
# ---------------------------------------------------------------------------

def _session_events(trace, event_name):
    if trace["session"] is None:
        return []
    return [e for e in trace["session"]["events"] if e["name"] == event_name]


def _elapsed_ms(event):
    return _coerce_int(event["attrs"].get("elapsed_ms"))


def _list_attr(event, key):
    val = event["attrs"].get(key, [])
    if isinstance(val, list):
        return val
    # Jaeger v3 API serializes StringSlice as a string like '["a","b"]'
    if isinstance(val, str) and val.startswith("["):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return []


# ---------------------------------------------------------------------------
# FNR — False Negative Rate
#
# A "false negative" in a trace: the node *could* be reachable (at least one
# probe returned "public") but no reachable_addrs_changed event with a
# non-empty "reachable" list was ever emitted.
# ---------------------------------------------------------------------------

def compute_fnr(traces):
    total = len(traces)
    fn_count = 0
    details = []

    for path, trace in traces:
        # Check if any probe succeeded (server said "public")
        any_public = any(
            p["attrs"].get("autonat.reachability") == "public"
            for p in trace["probes"]
        )
        # Check if reachable_addrs_changed with non-empty reachable was emitted
        converged_reachable = any(
            len(_list_attr(e, "reachable")) > 0
            for e in _session_events(trace, "reachable_addrs_changed")
        )

        is_fn = any_public and not converged_reachable
        if is_fn:
            fn_count += 1
        details.append({
            "file": str(path),
            "any_probe_public": any_public,
            "converged_reachable": converged_reachable,
            "false_negative": is_fn,
        })

    rate = fn_count / total if total > 0 else 0.0
    return {
        "metric": "false_negative_rate",
        "total": total,
        "false_negatives": fn_count,
        "rate": rate,
        "details": details,
    }


# ---------------------------------------------------------------------------
# FPR — False Positive Rate
#
# A "false positive": no probe returned "public" (node is unreachable) but
# reachable_addrs_changed with non-empty "reachable" was emitted anyway.
# ---------------------------------------------------------------------------

def compute_fpr(traces):
    total = len(traces)
    fp_count = 0
    details = []

    for path, trace in traces:
        any_public = any(
            p["attrs"].get("autonat.reachability") == "public"
            for p in trace["probes"]
        )
        reported_reachable = any(
            len(_list_attr(e, "reachable")) > 0
            for e in _session_events(trace, "reachable_addrs_changed")
        )

        is_fp = not any_public and reported_reachable
        if is_fp:
            fp_count += 1
        details.append({
            "file": str(path),
            "any_probe_public": any_public,
            "reported_reachable": reported_reachable,
            "false_positive": is_fp,
        })

    rate = fp_count / total if total > 0 else 0.0
    return {
        "metric": "false_positive_rate",
        "total": total,
        "false_positives": fp_count,
        "rate": rate,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Time-to-Confidence
#
# For each trace: find the first reachable_addrs_changed event with a non-empty
# "reachable" list. The elapsed_ms of that event is the time-to-confidence for
# reachable nodes.
#
# For unreachable nodes: find the first reachable_addrs_changed event with a
# non-empty "unreachable" list. Report both separately.
# ---------------------------------------------------------------------------

def compute_time_to_confidence(traces):
    reachable_times = []
    unreachable_times = []
    no_convergence = 0

    for path, trace in traces:
        events = _session_events(trace, "reachable_addrs_changed")
        ttc_reachable = None
        ttc_unreachable = None
        for e in events:
            if ttc_reachable is None and len(_list_attr(e, "reachable")) > 0:
                ttc_reachable = _elapsed_ms(e)
            if ttc_unreachable is None and len(_list_attr(e, "unreachable")) > 0:
                ttc_unreachable = _elapsed_ms(e)

        if ttc_reachable is not None:
            reachable_times.append(ttc_reachable)
        elif ttc_unreachable is not None:
            unreachable_times.append(ttc_unreachable)
        else:
            no_convergence += 1

    def stats(values):
        if not values:
            return None
        values_sorted = sorted(values)
        n = len(values_sorted)
        p95_idx = max(0, int(n * 0.95) - 1)
        return {
            "n": n,
            "mean_ms": round(statistics.mean(values_sorted)),
            "median_ms": round(statistics.median(values_sorted)),
            "p95_ms": values_sorted[p95_idx],
            "min_ms": values_sorted[0],
            "max_ms": values_sorted[-1],
        }

    return {
        "metric": "time_to_confidence",
        "total": len(traces),
        "no_convergence": no_convergence,
        "reachable": stats(reachable_times),
        "unreachable": stats(unreachable_times),
    }


# ---------------------------------------------------------------------------
# Time-to-Update
#
# Measures how long it takes AutoNAT to detect a mid-session reachability
# change. Requires a trace with at least two reachable_addrs_changed events
# that flip status (reachable → unreachable or vice versa).
#
# Note: the full dynamic test (P3.6) injects a network change at a known time
# via iptables; without that timestamp the best we can do is measure the time
# between status flips as a lower bound.
# ---------------------------------------------------------------------------

def compute_time_to_update(traces):
    flip_times = []
    no_flip = 0

    for path, trace in traces:
        events = _session_events(trace, "reachable_addrs_changed")
        prev_reachable = None
        flip_elapsed = None
        flip_from = None

        for e in events:
            is_reachable = len(_list_attr(e, "reachable")) > 0
            if prev_reachable is not None and is_reachable != prev_reachable:
                elapsed = _elapsed_ms(e)
                if elapsed is not None:
                    flip_elapsed = elapsed
                    flip_from = "reachable→unreachable" if prev_reachable else "unreachable→reachable"
                    break
            prev_reachable = is_reachable

        if flip_elapsed is not None:
            flip_times.append({"elapsed_ms": flip_elapsed, "direction": flip_from})
        else:
            no_flip += 1

    return {
        "metric": "time_to_update",
        "total": len(traces),
        "no_flip_detected": no_flip,
        "note": "elapsed_ms is time of status change since session start, not since network change event. Full P3.6 dynamic test provides the delta.",
        "flips": flip_times,
    }


# ---------------------------------------------------------------------------
# Protocol Overhead
#
# Counts probes and refresh cycles per session, and estimates byte-level
# protocol overhead from trace data (P3.7).
#
# Message size model (from autonatv2.proto):
#   DialRequest:      12 + sum(len(addr)) bytes  (nonce + framing + multiaddrs)
#   DialDataRequest:  16 bytes (fixed: addrIdx + numBytes + framing)
#   DialDataResponse: num_bytes (30K-100K amplification, 0 if not triggered)
#   DialResponse:     12 bytes (fixed: status + addrIdx + dialStatus + framing)
#   DialBack:         12 bytes (fixed: nonce + framing, separate stream)
# ---------------------------------------------------------------------------

def _probe_byte_estimate(probe):
    """Estimate byte costs for a single probe span.

    Uses span events (dial_data_requested, response_received, dial_back_received)
    when available, falls back to attribute-based estimation.

    Returns a dict with per-message and aggregate byte estimates.
    """
    attrs = probe["attrs"]
    events = probe.get("events", []) or []

    # Parse multiaddr list to compute DialRequest size
    addrs_raw = attrs.get("autonat.addrs", "[]")
    if isinstance(addrs_raw, str):
        try:
            addrs = json.loads(addrs_raw)
        except (json.JSONDecodeError, ValueError):
            addrs = []
    elif isinstance(addrs_raw, list):
        addrs = addrs_raw
    else:
        addrs = []

    # DialRequest: 12 bytes framing/nonce + serialized multiaddrs
    addr_bytes = sum(len(a.encode("utf-8")) if isinstance(a, str) else len(str(a)) for a in addrs)
    dial_request = 12 + addr_bytes

    # Check events for dial_data and dial_back
    event_names = {e["name"] for e in events}

    # DialDataRequest + DialDataResponse (amplification)
    dial_data_request = 0
    dial_data_response = 0
    if "dial_data_requested" in event_names:
        dial_data_request = 16
        for e in events:
            if e["name"] == "dial_data_requested":
                nb = _coerce_int(e["attrs"].get("num_bytes"))
                if nb is not None:
                    dial_data_response = nb
                break

    # DialResponse: always present if probe completed
    dial_response = 12

    # DialBack: present if server successfully dialed back
    dial_back = 0
    if "dial_back_received" in event_names:
        dial_back = 12
    elif not events:
        # No events available — infer from reachability attribute
        if attrs.get("autonat.reachability") == "public":
            dial_back = 12

    total = dial_request + dial_data_request + dial_data_response + dial_response + dial_back
    amplification = dial_data_response
    protocol_only = total - amplification

    return {
        "dial_request": dial_request,
        "dial_data_request": dial_data_request,
        "dial_data_response": dial_data_response,
        "dial_response": dial_response,
        "dial_back": dial_back,
        "total": total,
        "amplification": amplification,
        "protocol_only": protocol_only,
    }


def compute_protocol_overhead(traces):
    probe_counts = []
    cycle_counts = []
    session_durations_ms = []

    # Byte-level stats per probe and per session
    protocol_bytes_per_probe = []
    amplification_bytes_per_probe = []
    total_bytes_per_probe = []
    total_bytes_per_session = []
    amplification_triggered = 0
    total_probes = 0

    for path, trace in traces:
        probe_counts.append(len(trace["probes"]))
        cycle_counts.append(len(trace["refresh_cycles"]))

        # Estimate session duration from shutdown event elapsed_ms
        shutdown_events = _session_events(trace, "shutdown")
        if shutdown_events:
            dur = _elapsed_ms(shutdown_events[-1])
            if dur is not None:
                session_durations_ms.append(dur)

        # Compute byte estimates for each probe
        session_total = 0
        for probe in trace["probes"]:
            est = _probe_byte_estimate(probe)
            protocol_bytes_per_probe.append(est["protocol_only"])
            amplification_bytes_per_probe.append(est["amplification"])
            total_bytes_per_probe.append(est["total"])
            session_total += est["total"]
            total_probes += 1
            if est["amplification"] > 0:
                amplification_triggered += 1

        if trace["probes"]:
            total_bytes_per_session.append(session_total)

    def stats(values):
        if not values:
            return None
        values_sorted = sorted(values)
        n = len(values_sorted)
        p95_idx = max(0, int(n * 0.95) - 1)
        return {
            "n": n,
            "mean": round(statistics.mean(values_sorted), 1),
            "median": statistics.median(values_sorted),
            "p95": values_sorted[p95_idx],
        }

    amp_pct = (amplification_triggered / total_probes * 100) if total_probes > 0 else 0.0

    return {
        "metric": "protocol_overhead",
        "total": len(traces),
        "probes_per_run": stats(probe_counts),
        "refresh_cycles_per_run": stats(cycle_counts),
        "session_duration_ms": stats(session_durations_ms),
        "protocol_bytes_per_probe": stats(protocol_bytes_per_probe),
        "amplification_bytes_per_probe": stats(amplification_bytes_per_probe),
        "total_bytes_per_probe": stats(total_bytes_per_probe),
        "total_bytes_per_session": stats(total_bytes_per_session),
        "amplification_triggered_pct": round(amp_pct, 1),
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _pct(rate):
    return f"{rate * 100:.2f}%"


def _ms(v):
    if v is None:
        return "n/a"
    return f"{v}ms"


def _bytes(v):
    if v is None:
        return "n/a"
    v = float(v)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}MB"
    if v >= 1_000:
        return f"{v / 1_000:.1f}KB"
    return f"{int(v)}B"


def format_text(results):
    lines = [f"=== Trace Analysis: {results['total_files']} file(s) ===", ""]

    for r in results["metrics"]:
        m = r["metric"]

        if m == "false_negative_rate":
            lines.append(f"False Negative Rate (FNR):")
            lines.append(f"  {r['false_negatives']}/{r['total']} = {_pct(r['rate'])}")
            lines.append("")

        elif m == "false_positive_rate":
            lines.append(f"False Positive Rate (FPR):")
            lines.append(f"  {r['false_positives']}/{r['total']} = {_pct(r['rate'])}")
            lines.append("")

        elif m == "time_to_confidence":
            lines.append(f"Time-to-Confidence:")
            if r["no_convergence"] > 0:
                lines.append(f"  {r['no_convergence']}/{r['total']} runs did not converge")
            if r["reachable"]:
                s = r["reachable"]
                lines.append(f"  Reachable convergence (n={s['n']}):")
                lines.append(f"    mean:   {_ms(s['mean_ms'])}")
                lines.append(f"    median: {_ms(s['median_ms'])}")
                lines.append(f"    p95:    {_ms(s['p95_ms'])}")
            if r["unreachable"]:
                s = r["unreachable"]
                lines.append(f"  Unreachable convergence (n={s['n']}):")
                lines.append(f"    mean:   {_ms(s['mean_ms'])}")
                lines.append(f"    median: {_ms(s['median_ms'])}")
                lines.append(f"    p95:    {_ms(s['p95_ms'])}")
            lines.append("")

        elif m == "time_to_update":
            lines.append(f"Time-to-Update:")
            lines.append(f"  {r['no_flip_detected']}/{r['total']} runs had no status flip")
            if r["flips"]:
                for fl in r["flips"]:
                    lines.append(f"  flip at {_ms(fl['elapsed_ms'])} ({fl['direction']})")
            lines.append(f"  Note: {r['note']}")
            lines.append("")

        elif m == "protocol_overhead":
            lines.append(f"Protocol Overhead (estimated):")
            if r["probes_per_run"]:
                s = r["probes_per_run"]
                lines.append(f"  probes per run:         mean={s['mean']}, median={s['median']}, p95={s['p95']}")
            if r["refresh_cycles_per_run"]:
                s = r["refresh_cycles_per_run"]
                lines.append(f"  refresh cycles per run: mean={s['mean']}, median={s['median']}, p95={s['p95']}")
            if r["session_duration_ms"]:
                s = r["session_duration_ms"]
                lines.append(f"  session duration:       mean={_ms(s['mean'])}, median={_ms(s['median'])}")
            if r.get("protocol_bytes_per_probe"):
                s = r["protocol_bytes_per_probe"]
                lines.append(f"  protocol bytes/probe:   mean={_bytes(s['mean'])}, median={_bytes(s['median'])}, p95={_bytes(s['p95'])}  (excl. amplification)")
            if r.get("amplification_bytes_per_probe"):
                s = r["amplification_bytes_per_probe"]
                lines.append(f"  amplification bytes:    mean={_bytes(s['mean'])}, median={_bytes(s['median'])}, p95={_bytes(s['p95'])}")
            if r.get("total_bytes_per_session"):
                s = r["total_bytes_per_session"]
                lines.append(f"  total bytes/session:    mean={_bytes(s['mean'])}, median={_bytes(s['median'])}, p95={_bytes(s['p95'])}")
            if r.get("amplification_triggered_pct") is not None:
                lines.append(f"  amplification triggered: {r['amplification_triggered_pct']}% of probes")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

METRIC_FUNCS = {
    "fnr": compute_fnr,
    "false_negative_rate": compute_fnr,
    "fpr": compute_fpr,
    "false_positive_rate": compute_fpr,
    "time_to_confidence": compute_time_to_confidence,
    "time_to_update": compute_time_to_update,
    "protocol_overhead": compute_protocol_overhead,
}

ALL_METRICS = ["fnr", "fpr", "time_to_confidence", "time_to_update", "protocol_overhead"]


def main():
    args = sys.argv[1:]

    # Parse options
    requested_metrics = ALL_METRICS
    output_format = "text"
    files = []

    i = 0
    while i < len(args):
        if args[i] == "--metric" and i + 1 < len(args):
            i += 1
            names = [m.strip() for m in args[i].split(",")]
            requested_metrics = []
            for n in names:
                if n == "all":
                    requested_metrics = ALL_METRICS
                    break
                elif n in METRIC_FUNCS:
                    canonical = n if n in METRIC_FUNCS and not n.startswith("false_") else n
                    requested_metrics.append(n)
                else:
                    print(f"Unknown metric: {n}", file=sys.stderr)
                    sys.exit(1)
        elif args[i] == "--output" and i + 1 < len(args):
            i += 1
            output_format = args[i]
            if output_format not in ("text", "json"):
                print(f"Unknown output format: {output_format}", file=sys.stderr)
                sys.exit(1)
        elif args[i].startswith("--"):
            print(f"Unknown option: {args[i]}", file=sys.stderr)
            sys.exit(1)
        else:
            files.append(Path(args[i]))
        i += 1

    if not files:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    # Load traces
    traces = []
    for path in files:
        try:
            trace = parse_trace(path)
            traces.append((path, trace))
        except Exception as e:
            print(f"Warning: failed to parse {path}: {e}", file=sys.stderr)

    if not traces:
        print("Error: no valid trace files loaded.", file=sys.stderr)
        sys.exit(1)

    # Deduplicate requested metrics (preserve order)
    seen = set()
    unique_metrics = []
    for m in requested_metrics:
        if m not in seen:
            seen.add(m)
            unique_metrics.append(m)

    # Compute metrics
    metric_results = []
    for m in unique_metrics:
        fn = METRIC_FUNCS.get(m)
        if fn:
            metric_results.append(fn(traces))

    output = {
        "total_files": len(traces),
        "metrics": metric_results,
    }

    if output_format == "json":
        json.dump(output, sys.stdout, indent=2, default=str)
        print()
    else:
        print(format_text(output))


if __name__ == "__main__":
    main()
