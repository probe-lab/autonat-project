#!/usr/bin/env python3
"""
Generate AutoNAT analysis charts from Nebula crawl data of the IPFS Amino DHT.

Reads CSVs in ./data/ and produces PNGs in this directory.

Each CSV is the output of one ClickHouse query against the public Nebula
dataset. The source table for each chart is:

  01_clients.csv               nebula_ipfs_amino.visits  (latest crawl)
  02_autonat_protocols.csv     nebula_ipfs_amino.visits  (latest crawl, kubo)
  03_server_mode.csv           nebula_ipfs_amino.visits  (latest crawl, kubo)
  04_oscillation.csv           nebula_ipfs_amino_silver.peer_logs_protocols
                               JOIN peer_logs_agent_version  (last 7 days)
  05_dialable_over_time.csv    nebula_ipfs_amino.crawls  (last 30 days)
  06_oscillation_refined.csv   nebula_ipfs_amino_silver.peer_logs_protocols
                               JOIN peer_logs_agent_version  (last 7 days)
                               kad-only vs AutoNAT-driven (kad+autonat v1) by version
  07_joint_state.csv           nebula_ipfs_amino.visits
                               JOIN nebula_ipfs_amino_silver.peer_logs_protocols
                               joint state (dialability × kad pattern) for Kubo peers

Full query text and column documentation are in
docs/nebula-autonat-analysis.md
"""

import csv
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick

ROOT = Path(__file__).parent
DATA = ROOT / "data"

plt.rcParams.update({
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.labelsize": 10,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "figure.titlesize": 14,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def read_csv(name):
    """Read a CSV with header into list of dicts."""
    with open(DATA / name) as f:
        return list(csv.DictReader(f))


# ----------------------------------------------------------------------
# Chart 1: Client distribution
# ----------------------------------------------------------------------
def chart_clients():
    rows = read_csv("01_clients.csv")
    # Sort: Kubo first, then alphabetical
    rows.sort(key=lambda r: (r["client"] != "kubo", -int(r["total"])))

    clients = [r["client"] for r in rows]
    totals = [int(r["total"]) for r in rows]
    dialable = [int(r["dialable"]) for r in rows]

    fig, ax = plt.subplots(figsize=(11, 6))
    y = range(len(clients))
    ax.barh(y, totals, color="#cccccc", label="Visible (peer ID known)")
    ax.barh(y, dialable, color="#3b82f6", label="Dialable (Nebula could connect)")
    ax.set_yticks(list(y))
    ax.set_yticklabels(clients)
    ax.invert_yaxis()
    ax.set_xlabel("Number of peers")
    ax.set_title("IPFS Amino DHT — Client distribution (single recent crawl)\n"
                 "Kubo dominates the dialable population; ~54% of visible peer IDs are unreachable",
                 loc="left")
    ax.legend(loc="lower right", frameon=False)

    for i, (t, d) in enumerate(zip(totals, dialable)):
        ax.text(t + max(totals) * 0.005, i, f"{t} ({d} dialable)", va="center", fontsize=8)

    fig.tight_layout()
    fig.savefig(ROOT / "01_clients.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote 01_clients.png")


# ----------------------------------------------------------------------
# Chart 2: AutoNAT protocols supported
# ----------------------------------------------------------------------
def chart_autonat_protocols():
    rows = read_csv("02_autonat_protocols.csv")
    order = ["v1 only", "v1 + v2", "v2 only", "neither"]
    rows.sort(key=lambda r: order.index(r["category"]))

    cats = [r["category"] for r in rows]
    cnts = [int(r["cnt"]) for r in rows]
    total = sum(cnts)

    colors = {
        "v1 only": "#94a3b8",
        "v1 + v2": "#3b82f6",
        "v2 only": "#10b981",
        "neither": "#ef4444",
    }
    bar_colors = [colors[c] for c in cats]

    fig, ax = plt.subplots(figsize=(9, 5.5))
    bars = ax.bar(cats, cnts, color=bar_colors)
    ax.set_ylabel("Number of dialable Kubo nodes")
    ax.set_title("AutoNAT server protocols advertised by Kubo nodes\n"
                 "Almost every v2 server also runs v1 (only 9 v2-only). "
                 "Half of dialable Kubo runs v2.",
                 loc="left")
    for bar, c in zip(bars, cnts):
        pct = c * 100.0 / total
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + total * 0.01,
                f"{c}\n({pct:.1f}%)", ha="center", fontsize=10)
    ax.set_ylim(0, max(cnts) * 1.18)
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(lambda x, _: f"{int(x):,}"))

    fig.tight_layout()
    fig.savefig(ROOT / "02_autonat_protocols.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote 02_autonat_protocols.png")


# ----------------------------------------------------------------------
# Chart 3: Server mode (kad presence) by Kubo version
# ----------------------------------------------------------------------
def chart_server_mode():
    rows = read_csv("03_server_mode.csv")
    versions = [r["version"] for r in rows]
    pct = [float(r["pct_server"]) for r in rows]
    totals = [int(r["total"]) for r in rows]

    fig, ax = plt.subplots(figsize=(11, 5.5))
    bars = ax.bar(versions, pct, color="#3b82f6", edgecolor="#1e40af")

    # Mark 0.34 (where v2 was added)
    if "0.34" in versions:
        idx = versions.index("0.34")
        ax.axvline(idx - 0.5, color="#ef4444", linestyle="--", linewidth=1.2,
                   label="v2 introduced (Kubo 0.34, May 2024)")

    ax.set_ylabel("% of dialable Kubo nodes advertising /ipfs/kad/1.0.0")
    ax.set_xlabel("Kubo version")
    ax.set_ylim(90, 102)
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(decimals=0))
    ax.set_title("DHT server mode (kad protocol advertisement) by Kubo version\n"
                 "Snapshot view: ~99% of dialable Kubo nodes are in DHT server mode at any moment",
                 loc="left")

    for bar, p, t in zip(bars, pct, totals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.2,
                f"{p:.1f}%\nn={t}", ha="center", fontsize=8)

    ax.legend(loc="lower left", frameon=False)
    fig.tight_layout()
    fig.savefig(ROOT / "03_server_mode.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote 03_server_mode.png")


# ----------------------------------------------------------------------
# Chart 4: Oscillation rate by Kubo version
# ----------------------------------------------------------------------
def chart_oscillation():
    rows = read_csv("04_oscillation.csv")
    versions = [r["version"] for r in rows]
    pct = [float(r["pct"]) for r in rows]
    totals = [int(r["total"]) for r in rows]

    # Color: pre-v2 grey, post-v2 red
    pre_v2 = {"0.1x", "0.2x", "0.30", "0.31", "0.32", "0.33"}
    colors = ["#94a3b8" if v in pre_v2 else "#ef4444" for v in versions]

    fig, ax = plt.subplots(figsize=(11, 6))
    bars = ax.bar(versions, pct, color=colors, edgecolor="#1f2937", linewidth=0.6)

    # v2 introduction marker
    if "0.34" in versions:
        idx = versions.index("0.34")
        ax.axvline(idx - 0.5, color="#1f2937", linestyle="--", linewidth=1.2,
                   label="v2 introduced (Kubo 0.34, May 2024)")

    ax.set_ylabel("% of stable peers exhibiting kad protocol toggling")
    ax.set_xlabel("Kubo version")
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(decimals=0))
    ax.set_title("AutoNAT v1 oscillation in production: kad protocol toggle rate\n"
                 "Observed over 7 days; v2 versions show ~3x higher oscillation than v1-only versions",
                 loc="left")

    for bar, p, t in zip(bars, pct, totals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.15,
                f"{p:.1f}%\nn={t}", ha="center", fontsize=8)

    # Legend
    from matplotlib.patches import Patch
    legend_handles = [
        Patch(facecolor="#94a3b8", label="v1 only (Kubo < 0.34)"),
        Patch(facecolor="#ef4444", label="v2 available (Kubo ≥ 0.34)"),
    ]
    ax.legend(handles=legend_handles + [
        plt.Line2D([0], [0], color="#1f2937", linestyle="--", label="v2 introduced (Kubo 0.34)")
    ], loc="upper left", frameon=False)

    ax.set_ylim(0, max(pct) * 1.25)
    fig.tight_layout()
    fig.savefig(ROOT / "04_oscillation.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote 04_oscillation.png")


# ----------------------------------------------------------------------
# Chart 5: Dialable rate over time
# ----------------------------------------------------------------------
def chart_dialable_over_time():
    rows = read_csv("05_dialable_over_time.csv")
    days = [r["day"] for r in rows]
    crawled = [float(r["avg_crawled"]) for r in rows]
    dialable = [float(r["avg_dialable"]) for r in rows]
    pct = [float(r["pct_dialable"]) for r in rows]

    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                              gridspec_kw={"height_ratios": [2, 1]})

    ax1 = axes[0]
    ax1.fill_between(days, crawled, color="#cbd5e1", label="Total visible peers", alpha=0.8)
    ax1.fill_between(days, dialable, color="#3b82f6", label="Dialable peers", alpha=0.9)
    ax1.set_ylabel("Peers per crawl (avg)")
    ax1.set_title("IPFS Amino DHT — visibility vs reachability (last 30 days)\n"
                  "~46% of visible peer IDs are dialable; ~54% are stale routing-table entries",
                  loc="left")
    ax1.legend(loc="upper right", frameon=False)
    ax1.yaxis.set_major_formatter(mtick.FuncFormatter(lambda x, _: f"{int(x):,}"))

    ax2 = axes[1]
    ax2.plot(days, pct, color="#1e40af", marker="o", markersize=3, linewidth=1.5)
    ax2.set_ylabel("% dialable")
    ax2.yaxis.set_major_formatter(mtick.PercentFormatter(decimals=0))
    ax2.set_ylim(min(pct) - 2, max(pct) + 2)
    ax2.set_xlabel("Date")

    # Limit x ticks for readability
    n = len(days)
    step = max(1, n // 10)
    ax2.set_xticks(days[::step])
    plt.setp(ax2.get_xticklabels(), rotation=45, ha="right")

    fig.tight_layout()
    fig.savefig(ROOT / "05_dialable_over_time.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote 05_dialable_over_time.png")


# ----------------------------------------------------------------------
# Chart 6: Refined oscillation — kad-only vs AutoNAT-driven (kad+autonat v1)
# ----------------------------------------------------------------------
def chart_oscillation_refined():
    rows = read_csv("06_oscillation_refined.csv")
    versions = [r["version"] for r in rows]
    pct_kad = [float(r["pct_kad"]) for r in rows]
    pct_autonat = [float(r["pct_autonat"]) for r in rows]
    totals = [int(r["total"]) for r in rows]

    fig, ax = plt.subplots(figsize=(12, 6))
    x = list(range(len(versions)))
    width = 0.38

    bars_kad = ax.bar([i - width/2 for i in x], pct_kad, width,
                      color="#94a3b8", edgecolor="#1f2937", linewidth=0.6,
                      label="kad protocol toggles (any cause)")
    bars_aut = ax.bar([i + width/2 for i in x], pct_autonat, width,
                      color="#ef4444", edgecolor="#1f2937", linewidth=0.6,
                      label="kad + autonat v1 toggle in lockstep (Public ↔ non-Public)")

    if "0.34" in versions:
        idx = versions.index("0.34")
        ax.axvline(idx - 0.5, color="#1f2937", linestyle="--", linewidth=1.2,
                   label="v2 introduced (Kubo 0.34, May 2024)")

    ax.set_xticks(x)
    ax.set_xticklabels(versions)
    ax.set_xlabel("Kubo version")
    ax.set_ylabel("% of stable peers in 7-day window")
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(decimals=0))
    ax.set_title("Refined: kad toggling vs AutoNAT-driven flip rate by Kubo version\n"
                 "Most kad toggles ARE AutoNAT-driven (kad and autonat v1 server toggle together)",
                 loc="left")
    ax.legend(loc="upper left", frameon=False, fontsize=9)

    for i, (pa, t) in enumerate(zip(pct_autonat, totals)):
        if pa > 0:
            ax.text(i + width/2, pa + 0.2, f"{pa:.1f}%", ha="center", fontsize=7)
        ax.text(i, -0.5, f"n={t}", ha="center", fontsize=7, color="#666")

    ax.set_ylim(-1.5, max(max(pct_kad), max(pct_autonat)) * 1.25)
    fig.tight_layout()
    fig.savefig(ROOT / "06_oscillation_refined.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote 06_oscillation_refined.png")


# ----------------------------------------------------------------------
# Chart 7: Joint state — dialability × kad pattern × v2 advertised
# ----------------------------------------------------------------------
def chart_joint_state():
    rows = read_csv("07_joint_state.csv")
    # Sort by dial_state then kad_pattern
    dial_order = ["always dialable", "flipping dialability", "never dialable"]
    kad_order = [
        "always kad on (Public)",
        "kad toggles",
        "always kad off (Private/Client mode)",
        "no silver data",
    ]

    # Build a grid: rows = kad pattern, columns = dial state
    data = {ks: {ds: (0, 0) for ds in dial_order} for ks in kad_order}
    for row in rows:
        ds = row["dial_state"]
        kp = row["kad_pattern"]
        peers = int(row["peers"])
        v2 = int(row["with_v2"])
        if ds in dial_order and kp in kad_order:
            data[kp][ds] = (peers, v2)

    fig, ax = plt.subplots(figsize=(11, 7))

    ncols = len(dial_order)
    nrows = len(kad_order)
    bar_w = 0.35
    x = list(range(ncols))

    # Stacked: total peers (light), v2-advertising subset (dark)
    for row_idx, kp in enumerate(kad_order):
        totals = [data[kp][ds][0] for ds in dial_order]
        v2s = [data[kp][ds][1] for ds in dial_order]
        offset = (row_idx - (nrows - 1) / 2) * bar_w
        xs = [xi + offset for xi in x]
        ax.bar(xs, totals, bar_w * 0.9, label=kp, color=["#94a3b8", "#ef4444", "#f59e0b", "#e5e7eb"][row_idx])
        # Dark overlay for v2-advertising subset
        ax.bar(xs, v2s, bar_w * 0.9, color="black", alpha=0.35)
        # Label peers above each bar
        for xi, t, v in zip(xs, totals, v2s):
            if t > 0:
                label = f"{t}"
                if v > 0:
                    label += f"\n({v} v2)"
                ax.text(xi, t + 30, label, ha="center", fontsize=7)

    ax.set_xticks(x)
    ax.set_xticklabels(dial_order)
    ax.set_xlabel("Dialability in the 7-day window")
    ax.set_ylabel("Number of Kubo peers")
    ax.set_title("Kubo peer joint state: dialability × kad protocol pattern\n"
                 "Dark overlay = subset also advertising AutoNAT v2 server",
                 loc="left")
    ax.legend(loc="upper right", frameon=False, fontsize=8, title="kad protocol pattern")
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(lambda x, _: f"{int(x):,}"))

    fig.tight_layout()
    fig.savefig(ROOT / "07_joint_state.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote 07_joint_state.png")


if __name__ == "__main__":
    chart_clients()
    chart_autonat_protocols()
    chart_server_mode()
    chart_oscillation()
    chart_dialable_over_time()
    chart_oscillation_refined()
    chart_joint_state()
    print("\nDone. Charts in", ROOT)
