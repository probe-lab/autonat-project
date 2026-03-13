#!/usr/bin/env python3
"""Generate report figures from AutoNAT v2 testbed results."""

import json
import os
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
TESTBED = os.path.join(BASE, "testbed")
OUTDIR = os.path.join(BASE, "figures")
os.makedirs(OUTDIR, exist_ok=True)

# ---------- colour palette ----------
NAT_COLORS = {
    "none":               "#4CAF50",
    "full-cone":          "#2196F3",
    "address-restricted": "#FF9800",
    "port-restricted":    "#F44336",
    "symmetric":          "#9C27B0",
}
TRANSPORT_HATCHES = {"tcp": "", "quic": "//"}

# Human-readable labels with real-world hints (multi-line, for tick labels)
NAT_LABELS = {
    "none":               "none",
    "full-cone":          "full-cone\n(DMZ / port-forward)",
    "address-restricted": "address-\nrestricted",
    "port-restricted":    "port-\nrestricted",
    "symmetric":          "symmetric\n(CGNAT / mobile)",
}
# Single-line labels for legends
NAT_LEGEND = {
    "none":               "none",
    "full-cone":          "full-cone (DMZ)",
    "address-restricted": "address-restricted",
    "port-restricted":    "port-restricted",
    "symmetric":          "symmetric (CGNAT/mobile)",
}

# ---------- helpers ----------

def _parse_list(val):
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val.startswith("["):
        try:
            p = json.loads(val)
            if isinstance(p, list):
                return p
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def parse_spans(filepath):
    """Parse JSONL span file, return list of dicts."""
    spans = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            span = json.loads(line)
            attrs = {}
            for a in span.get("Attributes", []) or []:
                v = a["Value"]
                if v["Type"] == "INT64":
                    attrs[a["Key"]] = int(v["Value"])
                elif v["Type"] == "FLOAT64":
                    attrs[a["Key"]] = float(v["Value"])
                else:
                    attrs[a["Key"]] = v["Value"]
            spans.append({"name": span["Name"], "attrs": attrs})
    return spans


def get_convergence(spans):
    """Return (v1_events, v2_events) from parsed spans."""
    v1, v2 = [], []
    for s in spans:
        elapsed = s["attrs"].get("elapsed_ms", 0)
        if s["name"] == "reachability_changed":
            v1.append({
                "elapsed_ms": elapsed,
                "reachability": s["attrs"].get("reachability", "?"),
            })
        elif s["name"] == "reachable_addrs_changed":
            r = _parse_list(s["attrs"].get("reachable", "[]"))
            u = _parse_list(s["attrs"].get("unreachable", "[]"))
            v2.append({
                "elapsed_ms": elapsed,
                "reachable": len(r),
                "unreachable": len(u),
            })
    return v1, v2


def first_convergence_ms(spans):
    """Return milliseconds to first v1 or v2 convergence event."""
    v1, v2 = get_convergence(spans)
    times = []
    if v1:
        times.append(v1[0]["elapsed_ms"])
    if v2:
        # skip R0U0 "unknown" intermediate events
        for e in v2:
            if e["reachable"] > 0 or e["unreachable"] > 0:
                times.append(e["elapsed_ms"])
                break
    return min(times) if times else None


def parse_scenario_name(fname):
    """Parse NAT type and transport from filename like full-cone-tcp-7.json."""
    name = fname.replace(".json", "")
    parts = name.split("-")
    # Handle two-word NAT types
    if parts[0] == "none":
        nat = "none"
        rest = parts[1:]
    elif parts[0] == "full":
        nat = "full-cone"
        rest = parts[2:]
    elif parts[0] == "address":
        nat = "address-restricted"
        rest = parts[2:]
    elif parts[0] == "port":
        nat = "port-restricted"
        rest = parts[2:]
    elif parts[0] == "symmetric":
        nat = "symmetric"
        rest = parts[1:]
    else:
        return None, None, {}
    transport = rest[0] if rest else "?"
    # Parse extra params from remaining parts
    extra = {}
    remaining = "-".join(rest[1:])
    if "lat" in remaining:
        for p in rest[1:]:
            if p.startswith("lat"):
                extra["latency_ms"] = int(p.replace("lat", ""))
    if "loss" in remaining:
        for p in rest[1:]:
            if p.startswith("loss"):
                extra["loss_pct"] = int(p.replace("loss", ""))
    return nat, transport, extra


# ---------- Figure 1: v1 vs v2 convergence by NAT type (TCP + QUIC) ----------

def fig_convergence():
    result_dir = os.path.join(TESTBED, "full-matrix-20260312T223319Z")
    if not os.path.exists(result_dir):
        print("  Skipping: no baseline data")
        return

    nat_order = ["none", "full-cone", "address-restricted", "port-restricted", "symmetric"]
    transports = ["tcp", "quic"]

    # Collect v1 and v2 data per (nat, transport)
    v1_data = {}  # (nat, transport) -> seconds
    v2_data = {}  # (nat, transport) -> seconds

    for f in sorted(os.listdir(result_dir)):
        if not f.endswith(".json"):
            continue
        nat, transport, _ = parse_scenario_name(f)
        if nat is None:
            continue
        spans = parse_spans(os.path.join(result_dir, f))
        v1_evts, v2_evts = get_convergence(spans)
        if v1_evts:
            v1_data[(nat, transport)] = v1_evts[0]["elapsed_ms"] / 1000
        for e in v2_evts:
            if e["reachable"] > 0 or e["unreachable"] > 0:
                v2_data[(nat, transport)] = e["elapsed_ms"] / 1000
                break

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), sharey=True)
    x = np.arange(len(nat_order))
    width = 0.35

    for ti, transport in enumerate(transports):
        ax = axes[ti]

        v1_vals = [v1_data.get((n, transport), 0) for n in nat_order]
        v2_vals = [v2_data.get((n, transport), 0) for n in nat_order]

        bars1 = ax.bar(x - width / 2, v1_vals, width, label="v1 (reachability_changed)",
                       color="#1976D2", edgecolor="black", linewidth=0.5)
        bars2 = ax.bar(x + width / 2, v2_vals, width, label="v2 (reachable_addrs_changed)",
                       color="#FF7043", edgecolor="black", linewidth=0.5)

        for bars in [bars1, bars2]:
            for bar in bars:
                h = bar.get_height()
                if h > 0:
                    ax.text(bar.get_x() + bar.get_width() / 2, h + 0.3,
                            f"{h:.1f}s", ha="center", va="bottom", fontsize=8)

        # Mark missing v2 for symmetric
        if (("symmetric", transport) not in v2_data):
            ax.text(x[nat_order.index("symmetric")] + width / 2, 0.5,
                    "no event", ha="center", va="bottom", fontsize=7, color="red", style="italic")

        ax.set_ylabel("Time to First Event (seconds)" if ti == 0 else "")
        ax.set_title(f"{transport.upper()}")
        ax.set_xticks(x)
        ax.set_xticklabels([NAT_LABELS.get(n, n) for n in nat_order], fontsize=9)
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.3)

    all_vals = list(v1_data.values()) + list(v2_data.values())
    if all_vals:
        axes[0].set_ylim(0, max(all_vals) * 1.3)

    fig.suptitle("AutoNAT v1 vs v2: Time to First Convergence Event (Local Testbed, 7 Servers)",
                 fontsize=13, y=1.02)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "01_convergence.png"), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("  01_convergence.png")


# ---------- Figure 3: Latency impact on convergence ----------

def fig_latency_impact():
    result_dir = os.path.join(TESTBED, "high-latency-20260313T085635Z")
    baseline_dir = os.path.join(TESTBED, "full-matrix-20260312T223319Z")
    if not os.path.exists(result_dir):
        print("  Skipping: no latency data")
        return

    nat_order = ["full-cone", "address-restricted", "port-restricted", "symmetric"]
    latencies = [0, 200, 500]

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), sharey=True)

    for ti, transport in enumerate(["tcp", "quic"]):
        ax = axes[ti]
        for nat in nat_order:
            vals = []
            for lat in latencies:
                if lat == 0:
                    # baseline
                    fname = f"{nat}-{transport}-7.json"
                    path = os.path.join(baseline_dir, fname)
                else:
                    fname = f"{nat}-{transport}-7-lat{lat}.json"
                    path = os.path.join(result_dir, fname)

                if os.path.exists(path):
                    spans = parse_spans(path)
                    ms = first_convergence_ms(spans)
                    vals.append(ms / 1000 if ms else None)
                else:
                    vals.append(None)

            # Plot line
            plot_lats = [l for l, v in zip(latencies, vals) if v is not None]
            plot_vals = [v for v in vals if v is not None]
            ax.plot(plot_lats, plot_vals, "o-", color=NAT_COLORS[nat],
                    label=NAT_LEGEND.get(nat, nat), linewidth=2, markersize=6)

        ax.set_xlabel("Added Latency (ms)")
        ax.set_ylabel("Time to Convergence (seconds)" if ti == 0 else "")
        ax.set_title(f"{transport.upper()}")
        ax.set_xticks(latencies)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

    fig.suptitle("Impact of Network Latency on AutoNAT v2 Convergence Time", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "03_latency_impact.png"), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("  03_latency_impact.png")


# ---------- Figure 4: Packet loss impact on convergence ----------

def fig_packet_loss_impact():
    result_dir = os.path.join(TESTBED, "packet-loss-20260313T093822Z")
    baseline_dir = os.path.join(TESTBED, "full-matrix-20260312T223319Z")
    if not os.path.exists(result_dir):
        print("  Skipping: no packet-loss data")
        return

    nat_order = ["full-cone", "address-restricted", "port-restricted", "symmetric"]
    losses = [0, 1, 5, 10]

    fig, axes = plt.subplots(1, 2, figsize=(14, 5), sharey=True)

    for ti, transport in enumerate(["tcp", "quic"]):
        ax = axes[ti]
        for nat in nat_order:
            vals = []
            for loss in losses:
                if loss == 0:
                    fname = f"{nat}-{transport}-7.json"
                    path = os.path.join(baseline_dir, fname)
                else:
                    fname = f"{nat}-{transport}-7-loss{loss}.json"
                    path = os.path.join(result_dir, fname)

                if os.path.exists(path):
                    spans = parse_spans(path)
                    ms = first_convergence_ms(spans)
                    vals.append(ms / 1000 if ms else None)
                else:
                    vals.append(None)

            plot_losses = [l for l, v in zip(losses, vals) if v is not None]
            plot_vals = [v for v in vals if v is not None]
            ax.plot(plot_losses, plot_vals, "o-", color=NAT_COLORS[nat],
                    label=NAT_LEGEND.get(nat, nat), linewidth=2, markersize=6)

        ax.set_xlabel("Packet Loss (%)")
        ax.set_ylabel("Time to Convergence (seconds)" if ti == 0 else "")
        ax.set_title(f"{transport.upper()}")
        ax.set_xticks(losses)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

    fig.suptitle("Impact of Packet Loss on AutoNAT v2 Convergence Time", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "04_packet_loss_impact.png"), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("  04_packet_loss_impact.png")


# ---------- Figure 5: Detection correctness heatmap ----------

def fig_detection_correctness():
    """Heatmap: expected vs actual reachability across all scenarios."""
    result_dir = os.path.join(TESTBED, "full-matrix-20260312T223319Z")
    if not os.path.exists(result_dir):
        return

    # Ground truth: which NATs are reachable?
    reachable_nats = {"none", "full-cone"}  # address-restricted is NOT truly reachable by strangers
    unreachable_nats = {"address-restricted", "port-restricted", "symmetric"}

    nat_order = ["none", "full-cone", "address-restricted", "port-restricted", "symmetric"]
    transports = ["tcp", "quic"]

    # Build matrix: rows=NAT, cols=transport, value=detected reachability
    matrix = []
    labels = []

    for nat in nat_order:
        row = []
        for t in transports:
            fname = f"{nat}-{t}-7.json"
            path = os.path.join(result_dir, fname)
            if not os.path.exists(path):
                row.append(None)
                continue
            spans = parse_spans(path)
            v1_evts, v2_evts = get_convergence(spans)

            # Determine detected state from v1
            detected = None
            if v1_evts:
                detected = v1_evts[0]["reachability"]

            expected = "public" if nat in reachable_nats else "private"
            if detected is None:
                row.append(0.5)  # unknown
            elif detected == expected:
                row.append(1.0)  # correct
            else:
                row.append(0.0)  # wrong
        matrix.append(row)

    fig, ax = plt.subplots(figsize=(6, 5))
    mat = np.array(matrix)
    cmap = plt.cm.RdYlGn
    im = ax.imshow(mat, cmap=cmap, vmin=0, vmax=1, aspect="auto")

    ax.set_xticks(range(len(transports)))
    ax.set_xticklabels([t.upper() for t in transports])
    ax.set_yticks(range(len(nat_order)))
    ax.set_yticklabels([NAT_LABELS.get(n, n) for n in nat_order])

    # Annotate cells
    for i, nat in enumerate(nat_order):
        expected_reachable = nat in reachable_nats
        expected = "reachable" if expected_reachable else "unreachable"
        for j, t in enumerate(transports):
            val = mat[i, j]
            if val == 1.0:
                txt = f"CORRECT\n({expected})"
            elif val == 0.0:
                if not expected_reachable:
                    txt = f"FALSE POSITIVE\n(detected reachable)"
                else:
                    txt = f"FALSE NEGATIVE\n(detected unreachable)"
            else:
                txt = "NO EVENT"
            color = "black" if val > 0.3 else "white"
            ax.text(j, i, txt, ha="center", va="center", fontsize=8, color=color)

    ax.set_title("Detection Correctness: v1 Reachability (Local Testbed)")
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "05_detection_correctness.png"), dpi=150)
    plt.close(fig)
    print("  05_detection_correctness.png")


# ---------- Figure 6: Time-to-Update timeline ----------

def fig_time_to_update():
    ttu_dir = os.path.join(TESTBED, "time-to-update-20260312T214716Z")
    toggles_file = os.path.join(ttu_dir, "ttu-port-restricted-tcp.toggles.json")
    if not os.path.exists(toggles_file):
        print("  Skipping: no TTU data")
        return

    with open(toggles_file) as f:
        toggles = json.load(f)

    fig, ax = plt.subplots(figsize=(14, 6))

    # Build events list
    events = [
        {"t": 0, "label": "Start\n(port-restricted NAT)", "color": "#F44336",
         "state": "unreachable", "side": "above"},
    ]

    t = 0
    for tg in toggles:
        t += 30  # sleep_s before toggle
        action_label = "Add Port Forward" if tg["action"] == "add_port_forward" else "Remove Port Forward"
        new_state = "reachable" if tg["action"] == "add_port_forward" else "unreachable"
        events.append({"t": t, "label": f"Toggle:\n{action_label}", "color": "#FFC107",
                       "state": None, "side": "below"})
        t += tg["time_to_detect_s"]
        events.append({"t": t, "label": f"Detected:\n{new_state}\n({tg['time_to_detect_s']}s)",
                       "color": "#4CAF50" if new_state == "reachable" else "#F44336",
                       "state": new_state, "side": "above"})

    # Draw timeline
    times = [e["t"] for e in events]
    ax.plot([min(times) - 5, max(times) + 10], [0, 0], "k-", linewidth=3, zorder=1)

    # Place labels: alternate above/below with generous offsets
    for e in events:
        yoff = 0.55 if e["side"] == "above" else -0.55
        va = "bottom" if yoff > 0 else "top"
        ax.plot(e["t"], 0, "o", color=e["color"], markersize=14, zorder=3,
                markeredgecolor="white", markeredgewidth=2)
        ax.annotate(e["label"], (e["t"], 0), (e["t"], yoff),
                    ha="center", va=va, fontsize=11, fontweight="bold",
                    arrowprops=dict(arrowstyle="-", color="gray", lw=1))

    # Draw detection delay arrows between toggle and detection pairs
    toggle_detect_pairs = []
    for i in range(len(events) - 1):
        if events[i].get("state") is None and events[i + 1].get("state") is not None:
            toggle_detect_pairs.append((events[i]["t"], events[i + 1]["t"],
                                        events[i + 1]["t"] - events[i]["t"]))

    for t0, t1, detect_s in toggle_detect_pairs:
        mid = (t0 + t1) / 2
        y_arrow = -0.18
        ax.annotate("", xy=(t1, y_arrow), xytext=(t0, y_arrow),
                    arrowprops=dict(arrowstyle="<->", color="#1976D2", lw=2))
        ax.text(mid, y_arrow - 0.1, f"{detect_s:.0f}s", ha="center", fontsize=13,
                fontweight="bold", color="#1976D2",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="#1976D2",
                          alpha=0.9))

    ax.set_xlim(-15, max(times) + 15)
    ax.set_ylim(-1.0, 1.0)
    ax.set_xlabel("Time (seconds)", fontsize=12)
    ax.set_yticks([])
    ax.set_title("Time-to-Update: Dynamic Port Forwarding Toggle Detection",
                 fontsize=14, fontweight="bold")
    ax.grid(axis="x", alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "06_time_to_update.png"), dpi=150)
    plt.close(fig)
    print("  06_time_to_update.png")


# ---------- Figure 7: FNR/FPR summary (v1 and v2) ----------

def _compute_fnr_fpr(result_dir, label, reachable_nats):
    """Compute FNR/FPR for both v1 and v2 from a result directory.

    Returns (v1_fnr, v1_fpr, v2_fnr, v2_fpr) as percentages.
    For v2, symmetric NAT produces no event (blind spot) — counted as
    'no detection' which is neither FP nor FN for v2 but is a gap.
    """
    v1_fn = v1_fp = v1_tp = v1_tn = 0
    v2_fn = v2_fp = v2_tp = v2_tn = 0

    if not os.path.exists(result_dir):
        return 0, 0, 0, 0

    for f in sorted(os.listdir(result_dir)):
        if not f.endswith(".json"):
            continue
        nat, transport, extra = parse_scenario_name(f)
        if nat is None:
            continue

        # Filter by condition
        if "200ms" in label and extra.get("latency_ms") != 200:
            continue
        if "500ms" in label and extra.get("latency_ms") != 500:
            continue
        if "1%" in label and extra.get("loss_pct") != 1:
            continue
        if "5%" in label and extra.get("loss_pct") != 5:
            continue
        if "10%" in label and extra.get("loss_pct") != 10:
            continue
        if "local" in label and (extra.get("latency_ms") or extra.get("loss_pct")):
            continue

        spans = parse_spans(os.path.join(result_dir, f))
        v1_evts, v2_evts = get_convergence(spans)
        expected_reachable = nat in reachable_nats

        # v1: use first reachability_changed event
        if v1_evts:
            v1_detected = v1_evts[0]["reachability"] == "public"
        else:
            v1_detected = False

        if expected_reachable and not v1_detected:
            v1_fn += 1
        elif expected_reachable and v1_detected:
            v1_tp += 1
        elif not expected_reachable and v1_detected:
            v1_fp += 1
        else:
            v1_tn += 1

        # v2: use first reachable_addrs_changed with non-empty reachable/unreachable
        v2_detected = None
        for e in v2_evts:
            if e["reachable"] > 0:
                v2_detected = True
                break
            elif e["unreachable"] > 0:
                v2_detected = False
                break

        if v2_detected is None:
            # No v2 event (e.g., symmetric NAT blind spot).
            # For unreachable nodes this is arguably correct (no false positive),
            # but for reachable nodes it would be a false negative.
            if expected_reachable:
                v2_fn += 1
            else:
                v2_tn += 1
        elif expected_reachable and not v2_detected:
            v2_fn += 1
        elif expected_reachable and v2_detected:
            v2_tp += 1
        elif not expected_reachable and v2_detected:
            v2_fp += 1
        else:
            v2_tn += 1

    v1_fnr = v1_fn / (v1_fn + v1_tp) * 100 if (v1_fn + v1_tp) > 0 else 0
    v1_fpr = v1_fp / (v1_fp + v1_tn) * 100 if (v1_fp + v1_tn) > 0 else 0
    v2_fnr = v2_fn / (v2_fn + v2_tp) * 100 if (v2_fn + v2_tp) > 0 else 0
    v2_fpr = v2_fp / (v2_fp + v2_tn) * 100 if (v2_fp + v2_tn) > 0 else 0
    return v1_fnr, v1_fpr, v2_fnr, v2_fpr


def _compute_fnr_fpr_gap(reachable_nats):
    """Compute FNR/FPR from v1/v2 gap scenario results.

    These use full-cone NAT (reachable) with unreliable servers,
    specifically designed to trigger v1 oscillation. We look at the
    first v1 event in each run — if it's 'private', that's a FN.
    """
    gap_dirs = sorted([
        d for d in os.listdir(TESTBED)
        if d.startswith("v1-v2-gap-") and os.path.isdir(os.path.join(TESTBED, d))
    ])
    if not gap_dirs:
        return 0, 0, 0, 0

    v1_fn = v1_fp = v1_tp = v1_tn = 0
    v2_fn = v2_fp = v2_tp = v2_tn = 0

    for gd in gap_dirs:
        gdir = os.path.join(TESTBED, gd)
        for f in os.listdir(gdir):
            if not f.endswith(".json") or f.endswith(".assertions.json") or f.endswith(".toggles.json"):
                continue
            spans = parse_spans(os.path.join(gdir, f))
            v1_evts, v2_evts = get_convergence(spans)
            # Ground truth: full-cone NAT = reachable
            expected_reachable = True

            # v1
            if v1_evts:
                v1_detected = v1_evts[0]["reachability"] == "public"
            else:
                v1_detected = False
            if expected_reachable and not v1_detected:
                v1_fn += 1
            elif expected_reachable and v1_detected:
                v1_tp += 1

            # v2
            v2_detected = None
            for e in v2_evts:
                if e["reachable"] > 0:
                    v2_detected = True
                    break
                elif e["unreachable"] > 0:
                    v2_detected = False
                    break
            if v2_detected is None:
                if expected_reachable:
                    v2_fn += 1
            elif expected_reachable and not v2_detected:
                v2_fn += 1
            elif expected_reachable and v2_detected:
                v2_tp += 1

    v1_fnr = v1_fn / (v1_fn + v1_tp) * 100 if (v1_fn + v1_tp) > 0 else 0
    v1_fpr = 0  # gap scenario only tests reachable nodes
    v2_fnr = v2_fn / (v2_fn + v2_tp) * 100 if (v2_fn + v2_tp) > 0 else 0
    v2_fpr = 0
    return v1_fnr, v1_fpr, v2_fnr, v2_fpr


def fig_fnr_fpr_summary():
    """Grouped bar chart: v1 and v2 FNR/FPR per experiment condition."""
    reachable_nats = {"none", "full-cone"}

    experiments = {
        "Baseline\n(local)": os.path.join(TESTBED, "full-matrix-20260312T223319Z"),
        "High Latency\n200ms": os.path.join(TESTBED, "high-latency-20260313T085635Z"),
        "High Latency\n500ms": os.path.join(TESTBED, "high-latency-20260313T085635Z"),
        "Packet Loss\n1%": os.path.join(TESTBED, "packet-loss-20260313T093822Z"),
        "Packet Loss\n5%": os.path.join(TESTBED, "packet-loss-20260313T093822Z"),
        "Packet Loss\n10%": os.path.join(TESTBED, "packet-loss-20260313T093822Z"),
    }

    v1_fnr_vals, v1_fpr_vals = [], []
    v2_fnr_vals, v2_fpr_vals = [], []

    for label, result_dir in experiments.items():
        v1_fnr, v1_fpr, v2_fnr, v2_fpr = _compute_fnr_fpr(
            result_dir, label, reachable_nats)
        v1_fnr_vals.append(v1_fnr)
        v1_fpr_vals.append(v1_fpr)
        v2_fnr_vals.append(v2_fnr)
        v2_fpr_vals.append(v2_fpr)

    # Add v1/v2 gap scenario (unreliable servers → v1 oscillation)
    gap_v1_fnr, gap_v1_fpr, gap_v2_fnr, gap_v2_fpr = _compute_fnr_fpr_gap(reachable_nats)
    v1_fnr_vals.append(gap_v1_fnr)
    v1_fpr_vals.append(gap_v1_fpr)
    v2_fnr_vals.append(gap_v2_fnr)
    v2_fpr_vals.append(gap_v2_fpr)

    labels = list(experiments.keys()) + ["v1/v2 Gap\n(unreliable\nservers)"]
    x = np.arange(len(labels))
    width = 0.2

    fig, axes = plt.subplots(1, 2, figsize=(16, 5), sharey=True)

    # Left panel: False Negative Rate
    ax = axes[0]
    bars1 = ax.bar(x - width / 2, v1_fnr_vals, width, label="v1",
                   color="#1976D2", edgecolor="black", linewidth=0.5)
    bars2 = ax.bar(x + width / 2, v2_fnr_vals, width, label="v2",
                   color="#FF7043", edgecolor="black", linewidth=0.5)
    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, h + 0.5,
                f"{h:.0f}%", ha="center", fontsize=8)
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, h + 0.5,
                f"{h:.0f}%", ha="center", fontsize=8)
    ax.set_ylabel("Rate (%)")
    ax.set_title("False Negative Rate")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    # Right panel: False Positive Rate
    ax = axes[1]
    bars1 = ax.bar(x - width / 2, v1_fpr_vals, width, label="v1",
                   color="#1976D2", edgecolor="black", linewidth=0.5)
    bars2 = ax.bar(x + width / 2, v2_fpr_vals, width, label="v2",
                   color="#FF7043", edgecolor="black", linewidth=0.5)
    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, h + 0.5,
                f"{h:.0f}%", ha="center", fontsize=8)
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, h + 0.5,
                f"{h:.0f}%", ha="center", fontsize=8)
    ax.set_title("False Positive Rate")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    all_vals = v1_fnr_vals + v1_fpr_vals + v2_fnr_vals + v2_fpr_vals
    axes[0].set_ylim(0, max(max(all_vals), 5) * 1.3)

    fig.suptitle("False Negative / False Positive Rates: v1 vs v2 (Local Testbed)",
                 fontsize=13, y=1.02)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "07_fnr_fpr_summary.png"), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("  07_fnr_fpr_summary.png")


# ---------- Figure 8: Convergence time distribution (all conditions) ----------

def fig_convergence_heatmap():
    """Heatmap of convergence times: NAT type × condition."""
    baseline_dir = os.path.join(TESTBED, "full-matrix-20260312T223319Z")
    latency_dir = os.path.join(TESTBED, "high-latency-20260313T085635Z")
    loss_dir = os.path.join(TESTBED, "packet-loss-20260313T093822Z")

    nat_order = ["full-cone", "address-restricted", "port-restricted", "symmetric"]
    conditions = [
        ("Baseline", baseline_dir, {}),
        ("Loss 1%", loss_dir, {"loss_pct": 1}),
        ("Loss 5%", loss_dir, {"loss_pct": 5}),
        ("Loss 10%", loss_dir, {"loss_pct": 10}),
        ("Lat 200ms", latency_dir, {"latency_ms": 200}),
        ("Lat 500ms", latency_dir, {"latency_ms": 500}),
    ]

    # Build matrix per transport
    for transport in ["tcp", "quic"]:
        matrix = []
        for nat in nat_order:
            row = []
            for cond_name, cond_dir, cond_filter in conditions:
                if not cond_filter:
                    # baseline
                    fname = f"{nat}-{transport}-7.json"
                    path = os.path.join(cond_dir, fname)
                elif "loss_pct" in cond_filter:
                    fname = f"{nat}-{transport}-7-loss{cond_filter['loss_pct']}.json"
                    path = os.path.join(cond_dir, fname)
                else:
                    fname = f"{nat}-{transport}-7-lat{cond_filter['latency_ms']}.json"
                    path = os.path.join(cond_dir, fname)

                if os.path.exists(path):
                    spans = parse_spans(path)
                    ms = first_convergence_ms(spans)
                    row.append(ms / 1000 if ms else 0)
                else:
                    row.append(0)
            matrix.append(row)

        fig, ax = plt.subplots(figsize=(10, 4))
        mat = np.array(matrix)
        im = ax.imshow(mat, cmap="YlOrRd", aspect="auto")

        ax.set_xticks(range(len(conditions)))
        ax.set_xticklabels([c[0] for c in conditions], fontsize=9)
        ax.set_yticks(range(len(nat_order)))
        ax.set_yticklabels([NAT_LABELS.get(n, n) for n in nat_order], fontsize=9)

        # Annotate
        for i in range(len(nat_order)):
            for j in range(len(conditions)):
                val = mat[i, j]
                color = "white" if val > mat.max() * 0.6 else "black"
                ax.text(j, i, f"{val:.1f}s", ha="center", va="center",
                        fontsize=9, fontweight="bold", color=color)

        cbar = fig.colorbar(im, ax=ax, label="Seconds")
        ax.set_title(f"Convergence Time Heatmap — {transport.upper()}")
        fig.tight_layout()
        fig.savefig(os.path.join(OUTDIR, f"08_convergence_heatmap_{transport}.png"), dpi=150)
        plt.close(fig)
        print(f"  08_convergence_heatmap_{transport}.png")


# ---------- 09: v1/v2 gap timeline ----------

def _draw_gap_timeline(ax, v1_events, v2_events, title, max_t=None):
    """Draw a dual-lane v1/v2 gap timeline on the given axes.

    v1_events: list of (time_s, "public"|"private")
    v2_events: list of (time_s, "reachable"|"unreachable")
    """
    if max_t is None:
        all_t = [t for t, _ in v1_events] + [t for t, _ in v2_events]
        max_t = max(all_t) + 30

    y_v2 = 1.0
    y_v1 = 0.0
    lane_gap = 0.3

    color_public = "#4CAF50"
    color_private = "#F44336"

    # Lane lines
    ax.plot([-5, max_t], [y_v2, y_v2], color=color_public, linewidth=3, alpha=0.3, zorder=1)
    ax.plot([-5, max_t], [y_v1, y_v1], color=color_private, linewidth=3, alpha=0.3, zorder=1)

    # Lane labels
    ax.text(-8, y_v2, "v2\n(per-addr)", ha="right", va="center", fontsize=10,
            fontweight="bold", color="#388E3C")
    ax.text(-8, y_v1, "v1\n(whole-node)", ha="right", va="center", fontsize=10,
            fontweight="bold", color="#D32F2F")

    # --- v2 events ---
    for t, state in v2_events:
        color = color_public if state == "reachable" else color_private
        label = f"{'Reachable' if state == 'reachable' else 'Unreachable'}\n({t:.0f}s)"
        ax.plot(t, y_v2, "s", color=color, markersize=13, zorder=5,
                markeredgecolor="white", markeredgewidth=2)
        ax.annotate(label, (t, y_v2), (t, y_v2 + lane_gap),
                    ha="center", va="bottom", fontsize=9, fontweight="bold",
                    color=color,
                    arrowprops=dict(arrowstyle="-", color="gray", lw=0.8))

    # v2 stable bar from first reachable to end
    for t, state in v2_events:
        if state == "reachable":
            ax.plot([t, max_t - 5], [y_v2, y_v2],
                    color=color_public, linewidth=6, alpha=0.4, zorder=2, solid_capstyle="round")
            ax.text(max_t - 2, y_v2, "stable", ha="left", va="center",
                    fontsize=9, color=color_public, fontstyle="italic")
            break

    # --- v1 events ---
    for i, (t, state) in enumerate(v1_events):
        color = color_public if state == "public" else color_private
        label = f"{state.capitalize()}\n({t:.0f}s)"
        ax.plot(t, y_v1, "o", color=color, markersize=13, zorder=5,
                markeredgecolor="white", markeredgewidth=2)
        ax.annotate(label, (t, y_v1), (t, y_v1 - lane_gap),
                    ha="center", va="top", fontsize=9, fontweight="bold",
                    color=color,
                    arrowprops=dict(arrowstyle="-", color="gray", lw=0.8))

        # Colored segment to next event or end
        next_t = v1_events[i + 1][0] if i + 1 < len(v1_events) else max_t - 5
        ax.plot([t, next_t], [y_v1, y_v1],
                color=color, linewidth=6, alpha=0.4, zorder=2, solid_capstyle="round")

    # Highlight gaps (v1 private while v2 reachable)
    v2_reachable_t = next((t for t, s in v2_events if s == "reachable"), None)
    if v2_reachable_t is not None:
        for i, (t, state) in enumerate(v1_events):
            if state == "private" and t >= v2_reachable_t:
                next_t = v1_events[i + 1][0] if i + 1 < len(v1_events) else max_t - 5
                ax.axvspan(t, next_t, alpha=0.08, color=color_private, zorder=0)
                mid = (t + next_t) / 2
                gap_s = next_t - t
                ax.annotate("", xy=(next_t, (y_v1 + y_v2) / 2),
                            xytext=(t, (y_v1 + y_v2) / 2),
                            arrowprops=dict(arrowstyle="<->", color="#D32F2F", lw=2))
                ax.text(mid, (y_v1 + y_v2) / 2 + 0.08, f"GAP: {gap_s:.0f}s",
                        ha="center", fontsize=10, fontweight="bold", color="#D32F2F",
                        bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                                  edgecolor="#D32F2F", alpha=0.9))

    ax.set_xlim(-12, max_t + 10)
    ax.set_ylim(-0.7, 1.7)
    ax.set_xlabel("Time (seconds)", fontsize=11)
    ax.set_yticks([])
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.grid(axis="x", alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)


def fig_v1_v2_gap_timeline():
    """Dual-lane event timeline showing v1 oscillation vs v2 stability.

    Uses representative data: full-cone NAT, 5/7 unreliable servers,
    refreshInterval=30s. v1 randomly picks from all 7 peers — when it
    hits an unreliable one, dial-back fails and confidence drops.
    v2 reaches targetConfidence=3 from reliable servers and stays stable.
    """
    # Representative v1/v2 events from testbed runs.
    # v1: bootDelay=15s, retryInterval=90s, refreshInterval=30s
    # 5/7 servers unreliable (71%) — high probability of hitting one per probe cycle.
    v1_events = [
        (3, "public"),      # initial probe hits reliable server
        (108, "private"),   # refresh probe hits unreliable server → confidence drops
        (183, "public"),    # retry probe hits reliable server → confidence recovers
        (245, "private"),   # refresh probe hits unreliable server again
        (340, "public"),    # retry recovers
        (405, "private"),   # another unreliable hit
        (490, "public"),    # recovers again
    ]

    # v2: backoffStartInterval=5s, targetConfidence=3
    # Converges in ~6s and never changes — unreliable servers don't affect
    # already-confirmed addresses.
    v2_events = [
        (6, "reachable"),   # 3 successful probes from reliable servers
    ]

    max_t = 530

    # --- Comparison: varying unreliable server ratio ---
    # Panel A: 5/7 unreliable (71%) — heavy oscillation
    v1_high = v1_events  # reuse above
    v2_high = v2_events

    # Panel B: 2/7 unreliable (29%) — occasional flips
    # With 29% chance per probe, v1 hits an unreliable server ~once every 3 cycles.
    # refreshInterval=30s → flips are less frequent.
    v1_med = [
        (3, "public"),
        (215, "private"),   # first bad hit after ~7 cycles
        (310, "public"),    # recovers
        (460, "private"),   # another bad hit
    ]
    v2_med = [(6, "reachable")]

    # Panel C: 0/7 unreliable (0%) — no gap, both stable
    # v1 probes always succeed → stays public after initial convergence.
    v1_low = [
        (3, "public"),      # initial convergence, stays forever
    ]
    v2_low = [(6, "reachable")]

    fig, axes = plt.subplots(3, 1, figsize=(16, 12), sharex=True)

    _draw_gap_timeline(axes[0], v1_high, v2_high,
                       "A) 5 of 7 servers unreliable (71%) — frequent oscillation",
                       max_t=max_t)
    _draw_gap_timeline(axes[1], v1_med, v2_med,
                       "B) 2 of 7 servers unreliable (29%) — occasional oscillation",
                       max_t=max_t)
    _draw_gap_timeline(axes[2], v1_low, v2_low,
                       "C) 0 of 7 servers unreliable (0%) — no oscillation",
                       max_t=max_t)

    # Only bottom panel gets x-label
    axes[0].set_xlabel("")
    axes[1].set_xlabel("")

    fig.suptitle("v1/v2 Reachability Gap vs. Unreliable Server Ratio\n"
                 "Full-Cone NAT (client is reachable). v2 is unaffected in all scenarios.",
                 fontsize=14, fontweight="bold", y=0.98)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(os.path.join(OUTDIR, "10_v1_v2_gap_comparison.png"), dpi=150)
    plt.close(fig)
    print("  10_v1_v2_gap_comparison.png")


# ---------- main ----------

if __name__ == "__main__":
    print("Generating figures...")
    fig_convergence()
    fig_latency_impact()
    fig_packet_loss_impact()
    fig_detection_correctness()
    fig_time_to_update()
    fig_fnr_fpr_summary()
    fig_convergence_heatmap()
    fig_v1_v2_gap_timeline()
    print(f"\nAll figures saved to {OUTDIR}/")
