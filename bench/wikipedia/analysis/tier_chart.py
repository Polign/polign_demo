#!/usr/bin/env python3
"""Inline SVG for the blog: cost, throughput, memory and CPU per instance tier.

Reads report/tier-table.json (from analysis/tiers.py) and writes the figure as
report/tier-chart.svg. With --inject PATH it also replaces whatever sits between
the <!-- tier-chart:start --> and <!-- tier-chart:end --> markers in that HTML
file, so the published chart is regenerated from the data rather than edited.

The SVG carries no colors of its own. Marks and text use CSS classes, and the
page maps those to its light and dark tokens, so the figure follows the theme.
"""
import argparse
import html
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "report/tier-table.json"
OUT = ROOT / "report/tier-chart.svg"

# Three color slots, the most an all-pairs chart carries with this palette.
# c7g.xlarge and c7gd.xlarge share one: the same 4-vCPU box, with or without
# its local disk, and the marker shape already separates the two rows.
SLOT = {"t4g.small": 1, "c7g.xlarge": 2, "c7gd.xlarge": 2, "r7gd.xlarge": 3, "c7g.4xlarge": 3}
LEGEND = [(1, "t4g.small"), (2, "c7g / c7gd.xlarge"), (3, "r7gd.xlarge")]
WIDTH, TOP = 760, 44
LABEL_W, PLOT_W, VALUE_W = 146, 160, 62
PANEL_W, GAP = 372, 16
ROW, HEAD = 24, 30


def panel_height(n):
    return HEAD + n * ROW + 10
RADIUS, RING = 5, 2

# One panel per measure. Log panels span orders of magnitude; the two resource
# panels are linear so a bar's worth of distance means the same thing everywhere.
PANELS = [
    ("Monthly cost", "on-demand, us-west-1, log scale", "total_on_demand", "log", (10, 1000),
     lambda v: f"${v:,.0f}"),
    ("Searches per second", "nprobe=4, held for ten minutes, log scale", "qps", "log", (1, 3000),
     lambda v: f"{v:,.1f}" if v < 100 else f"{v:,.0f}"),
    ("Peak memory", "process RSS, the budget it was given", "rss_peak_mib", "linear", (0, 9 * 1024),
     lambda v: f"{v / 1024:.1f} GiB"),
    ("CPU", "share of the whole host while serving", "cpu_mean_pct", "linear", (0, 100),
     lambda v: f"{v:.0f}%"),
]


def position(value, scale, bounds):
    low, high = bounds
    if scale == "log":
        t = (math.log10(value) - math.log10(low)) / (math.log10(high) - math.log10(low))
    else:
        t = (value - low) / (high - low)
    return max(0.0, min(1.0, t))


def label(row):
    return f"{row['instance']} · {'S3' if row['path'] == 'S3' else 'cache'}"


def mark(x, y, slot, hollow, title):
    cls = f"hollow s{slot}" if hollow else f"dot s{slot}"
    return (f'<g class="mark" tabindex="0"><title>{html.escape(title)}</title>'
            f'<circle class="hit" cx="{x:.1f}" cy="{y}" r="12"/>'
            f'<circle class="ring" cx="{x:.1f}" cy="{y}" r="{RADIUS + RING}"/>'
            f'<circle class="{cls}" cx="{x:.1f}" cy="{y}" r="{RADIUS}"/></g>')


def panel(index, spec, rows):
    title, subtitle, key, scale, bounds, fmt = spec
    x0 = (index % 2) * (PANEL_W + GAP)
    y0 = TOP + (index // 2) * (panel_height(len(rows)) + 12)
    px0 = x0 + LABEL_W
    parts = [f'<text class="t" x="{x0}" y="{y0 + 12}">{html.escape(title)}</text>',
             f'<text class="st" x="{x0}" y="{y0 + 25}">{html.escape(subtitle)}</text>']
    top = y0 + HEAD + ROW / 2
    parts.append(f'<line class="axis" x1="{px0}" y1="{top - 8}" x2="{px0}" y2="{top + (len(rows) - 1) * ROW + 8}"/>')
    for i, row in enumerate(rows):
        y = top + i * ROW
        value = row[key]
        x = px0 + position(value, scale, bounds) * PLOT_W
        text = fmt(value)
        if key == "qps" and row["errors"]:
            text += "†"
        readout = f"{label(row)}: {text} {title.lower()}"
        parts.append(f'<text class="lbl" x="{x0}" y="{y + 4}">{html.escape(label(row))}</text>')
        parts.append(f'<line class="stem" x1="{px0}" y1="{y}" x2="{x:.1f}" y2="{y}"/>')
        parts.append(mark(x, y, SLOT[row["instance"]], row["path"] != "S3", readout))
        parts.append(f'<text class="val" x="{x + RADIUS + RING + 6:.1f}" y="{y + 4}">{html.escape(text)}</text>')
    return "\n".join(parts)


def legend(rows):
    parts = []
    x = 0
    present = {SLOT[r["instance"]] for r in rows}
    for slot, name in LEGEND:
        if slot not in present:
            continue
        parts.append(f'<circle class="dot s{slot}" cx="{x + 6}" cy="12" r="{RADIUS}"/>')
        parts.append(f'<text class="lg" x="{x + 16}" y="16">{html.escape(name)}</text>')
        x += 16 + 7 * len(name) + 22
    x = WIDTH - 268
    parts.append(f'<circle class="dot lgk" cx="{x + 6}" cy="12" r="{RADIUS}"/>')
    parts.append(f'<text class="lg" x="{x + 16}" y="16">straight from S3</text>')
    x += 132
    parts.append(f'<circle class="hollow lgk" cx="{x + 6}" cy="12" r="{RADIUS}"/>')
    parts.append(f'<text class="lg" x="{x + 16}" y="16">with a local cache</text>')
    return "\n".join(parts)


def render(rows):
    height = TOP + 2 * panel_height(len(rows)) + 12
    # Kept to one sentence: text extractors run title and desc together, and
    # the tables under the figure already carry every number.
    desc = (f"Four instance profiles compared on monthly cost, searches per second, peak memory and CPU. "
            f"The tables below carry the same numbers for all {len(rows)} boxes.")
    body = [f'<svg class="tier-chart" viewBox="0 0 {WIDTH} {height}" role="img" '
            f'aria-labelledby="tier-chart-title tier-chart-desc">',
            '<title id="tier-chart-title">Cost, throughput, memory and CPU for one S3 index on four boxes.</title>',
            f'<desc id="tier-chart-desc">{html.escape(desc)}</desc>',
            legend(rows)]
    for index, spec in enumerate(PANELS):
        body.append(panel(index, spec, rows))
    body.append("</svg>")
    return "\n".join(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inject", help="HTML file whose tier-chart markers receive the SVG")
    arguments = parser.parse_args()
    rows = json.loads(TABLE.read_text())
    rows.sort(key=lambda r: r["total_on_demand"])
    svg = render(rows)
    OUT.write_text(svg + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(svg):,} bytes, {len(rows)} rows)")
    if arguments.inject:
        page = Path(arguments.inject)
        text = page.read_text()
        pattern = re.compile(r"(<!-- tier-chart:start -->)(.*?)(<!-- tier-chart:end -->)", re.S)
        if not pattern.search(text):
            raise SystemExit("markers not found in " + str(page))
        page.write_text(pattern.sub(lambda m: m.group(1) + "\n" + svg + "\n      " + m.group(3), text, count=1))
        print(f"injected into {page}")


if __name__ == "__main__":
    main()
