#!/usr/bin/env python3
"""Throughput analysis: queries per second are bounded by bytes read per query.

Every cold query reads whole IVF cells out of the object store, so a node's
query rate is its network allowance divided by the bytes one query costs. This
script measures both halves from the recorded runs and reports the ratio.
"""
import argparse
import csv
import datetime as dt
import gzip
import json
from pathlib import Path
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "report"
COLORS = {"small": "#176bca", "medium": "#e77920", "large": "#6a3d9a", "nvme-c7gd": "#0d9488", "nvme-r7gd": "#b45309"}
# Sustained and burst network allowance in MiB/s, from the published EC2
# figures for each instance type. Sustained is what a run settles at once the
# burst allowance is spent.
NETWORK = {
    "small": ("t4g.small", 0.128, 5.0),
    "medium": ("c7g.xlarge", 1.875, 12.5),
    "large": ("c7g.4xlarge", 7.5, 15.0),
    "nvme-c7gd": ("c7gd.xlarge", 1.875, 12.5),
    "nvme-r7gd": ("r7gd.xlarge", 1.875, 12.5),
}


def node_of(run_name):
    """The node a run directory belongs to: the longest known prefix, since
    node names may themselves contain the separator."""
    for node in sorted(NETWORK, key=len, reverse=True):
        if run_name.startswith(node + "-"):
            return node
    return None
MIB = 2 ** 20


def gbps_to_mibs(gbps):
    return gbps * 1e9 / 8 / MIB


def timestamp(text):
    # Fractional seconds vary in width between runs; datetime.fromisoformat on
    # this Python only accepts exactly three or six digits.
    text = re.sub(r"(T\d{2}:\d{2}:\d{2})\.(\d+)",
                  lambda m: m.group(1) + "." + m.group(2)[:6].ljust(6, "0"), text)
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def jsonfile(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def runs():
    rows = []
    for path in sorted((ROOT / "runs").glob("*/summary.json")):
        directory = path.parent
        summary = jsonfile(path)
        if not summary:
            continue
        config = summary["config"]
        node = node_of(directory.name)
        if node is None:
            continue
        before = jsonfile(directory / "node-before.json")
        after = jsonfile(directory / "node-after.json")
        b = before.get("benchmark-metrics", {}).get("s3", {})
        a = after.get("benchmark-metrics", {}).get("s3", {})
        build = before.get("build", {})
        success = max(1, summary["success"])
        seconds = max(1e-9, summary["elapsed_seconds"])
        byts = max(0, a.get("bytes", 0) - b.get("bytes", 0))
        sustained, burst = (gbps_to_mibs(v) for v in NETWORK[node][1:])
        rows.append({
            "run": directory.name, "node": node, "concurrency": config["Concurrency"],
            "nprobe": config["NProbe"], "mix": config["Distribution"], "seconds": seconds,
            "qps": summary["successful_qps"], "p50_ms": summary["success_latency_ms"]["p50"],
            "p99_ms": summary["success_latency_ms"]["p99"], "errors": summary["errors"],
            "hit10": summary.get("hit_at", {}).get("10", 0), "mrr": summary["mrr_at_k"],
            "mib_per_query": byts / MIB / success,
            "s3_per_query": max(0, a.get("attempts", 0) - b.get("attempts", 0)) / success,
            "hedges_per_query": max(0, a.get("hedge_launches", 0) - b.get("hedge_launches", 0)) / success,
            "mib_s": byts / MIB / seconds,
            "pct_sustained": 100 * (byts / MIB / seconds) / sustained,
            "pct_burst": 100 * (byts / MIB / seconds) / burst,
            "segment_cache_mib": build.get("segment_cache_bytes", 0) / MIB,
            "disk_cache": bool(build.get("disk_cache")),
            "disk_cache_gib": build.get("disk_cache_bytes", 0) / 2 ** 30,
            "hedge_ms": build.get("hedge_ms"), "memory_max": build.get("memory_max", ""),
        })
    return rows


def timeline(name, window=15):
    """Completed queries per second over a run, in fixed windows."""
    path = ROOT / "runs" / name / "requests.jsonl.gz"
    if not path.exists():
        return [], []
    finished = []
    with gzip.open(path, "rt") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == 200:
                finished.append(timestamp(row["completed_at"]))
    if not finished:
        return [], []
    finished.sort()
    start = finished[0]
    counts = {}
    for value in finished:
        counts[int((value - start) // window)] = counts.get(int((value - start) // window), 0) + 1
    keys = sorted(counts)[:-1]  # drop the partial trailing window
    return [k * window for k in keys], [counts[k] / window for k in keys]


def bandwidth_chart(rows):
    """QPS against bytes per query, with each instance's allowance as a line."""
    points = [r for r in rows if r["mib_per_query"] > 0.5 and r["qps"] > 0.2 and r["errors"] <= r["qps"] * r["seconds"] * .05]
    if not points:
        return None
    figure, axes = plt.subplots(figsize=(9.5, 6))
    grid = np.logspace(np.log10(3), np.log10(320), 100)
    for node, (label, sustained_gbps, burst_gbps) in NETWORK.items():
        if not any(r["node"] == node for r in points):
            continue
        for allowance, style, tag in [(gbps_to_mibs(sustained_gbps), "-", "sustained"),
                                      (gbps_to_mibs(burst_gbps), ":", "burst")]:
            axes.plot(grid, allowance / grid, style, color=COLORS[node], alpha=.55, linewidth=1.2,
                      label=f"{label} {tag} ({allowance:,.0f} MiB/s)")
    # Runs that read through a disk cache fall below their network line: they
    # were limited by the volume, not by the network.
    for node in NETWORK:
        for cached, marker, size, tag in [(False, "o", 26, "direct object store"), (True, "x", 34, "through disk cache")]:
            selected = [r for r in points if r["node"] == node and r["disk_cache"] == cached]
            if not selected:
                continue
            axes.scatter([r["mib_per_query"] for r in selected], [r["qps"] for r in selected],
                         s=size, marker=marker, color=COLORS[node],
                         edgecolor="white" if not cached else None, linewidth=.5, zorder=3,
                         label=f"{NETWORK[node][0]}, {tag}")
    axes.set_xscale("log")
    axes.set_yscale("log")
    axes.set_xlabel("Object-store bytes read per query (MiB)")
    axes.set_ylabel("Successful queries per second")
    axes.set_title("Query rate is the network allowance divided by bytes per query")
    axes.grid(alpha=.2, which="both")
    axes.legend(fontsize=7.5, loc="lower left")
    figure.tight_layout()
    save(figure, "bandwidth-ceiling")
    return "bandwidth-ceiling"


def decay_chart(names):
    """Long runs settle from the burst allowance onto the sustained one."""
    series = [(n, *timeline(n)) for n in names]
    series = [s for s in series if s[1]]
    if not series:
        return None
    figure, axes = plt.subplots(figsize=(9.5, 4.6))
    palette = ["#0d9488", "#4a3aa7", "#b45309", "#c73a68", "#2a78d6", "#176bca", "#6a3d9a"]
    for index, (name, xs, ys) in enumerate(series):
        axes.plot(xs, ys, linewidth=1.2, color=palette[index % len(palette)],
                  linestyle="--" if name.startswith("small") else "-", label=name)
    axes.set_xlabel("Seconds into the run")
    axes.set_ylabel("Completed queries per second")
    axes.set_title("Burst allowance is spent within the first minutes of a sustained run")
    axes.grid(alpha=.2)
    axes.legend(fontsize=7.5)
    figure.tight_layout()
    save(figure, "burst-decay")
    return "burst-decay"


def nprobe_chart(rows):
    """The accuracy bought by each extra probe, against what it costs."""
    # Medium only, and only runs that completed cleanly: the small node's
    # nprobe=8 pass lost 9 of 100 questions to timeouts and understates quality.
    quality = {r["nprobe"]: r for r in rows
               if r["run"].endswith("-quality") and "nprobe" in r["run"] and r["node"] == "medium" and not r["errors"]}
    load = {r["nprobe"]: r for r in rows
            if r["run"].endswith("-load") and r["node"] == "medium" and not r["errors"]}
    probes = sorted(set(quality) & set(load))
    if not probes:
        return None
    figure, axes = plt.subplots(1, 2, figsize=(10.5, 4))
    axes[0].plot(probes, [quality[p]["hit10"] for p in probes], "-o", color="#0d9488", label="Hit@10")
    axes[0].plot(probes, [quality[p]["mrr"] for p in probes], "--s", color="#4a3aa7", label="MRR@10")
    axes[0].set_xlabel("nprobe")
    axes[0].set_ylabel("Retrieval quality")
    axes[0].set_ylim(0, .7)
    axes[0].legend(fontsize=8)
    axes[1].plot(probes, [load[p]["qps"] for p in probes], "-o", color="#e77920", label="QPS (burst)")
    axes[1].set_xlabel("nprobe")
    axes[1].set_ylabel("Successful queries per second")
    twin = axes[1].twinx()
    twin.plot(probes, [load[p]["mib_per_query"] for p in probes], "--^", color="#b45309", label="MiB per query")
    twin.set_ylabel("MiB read per query")
    axes[1].legend(fontsize=8, loc="upper right")
    twin.legend(fontsize=8, loc="center right")
    for axis in axes:
        axis.grid(alpha=.2)
        axis.set_xticks(probes)
    figure.suptitle("Probe count sets both the accuracy and the bytes each query costs")
    figure.tight_layout()
    save(figure, "nprobe-tradeoff")
    return "nprobe-tradeoff"


def save(figure, name):
    (OUT / "charts").mkdir(parents=True, exist_ok=True)
    figure.savefig(OUT / "charts" / (name + ".svg"), bbox_inches="tight")
    figure.savefig(OUT / "charts" / (name + ".png"), dpi=170, bbox_inches="tight")
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--decay", nargs="*", default=[], help="runs to draw on the decay chart")
    arguments = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    rows = runs()
    if not rows:
        raise SystemExit("No completed runs")
    with (OUT / "throughput.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    charts = [bandwidth_chart(rows), nprobe_chart(rows)]
    decay = arguments.decay or [r["run"] for r in rows if r["seconds"] >= 500]
    charts.append(decay_chart(decay))
    header = f"{'run':34}{'np':>3}{'c':>4}{'MiB/q':>8}{'qps':>8}{'MiB/s':>9}{'%sust':>7}{'%burst':>8}{'p99ms':>9}{'err':>6}"
    print(header)
    print("-" * len(header))
    for row in sorted(rows, key=lambda r: (r["node"], r["run"])):
        print(f"{row['run'][:33]:34}{row['nprobe']:>3}{row['concurrency']:>4}"
              f"{row['mib_per_query']:8.1f}{row['qps']:8.1f}{row['mib_s']:9.1f}"
              f"{row['pct_sustained']:7.0f}{row['pct_burst']:8.0f}{row['p99_ms']:9.1f}{row['errors']:6d}")
    print("\nCharts:", ", ".join(c for c in charts if c))
    print("Table:  report/throughput.csv")


if __name__ == "__main__":
    main()
