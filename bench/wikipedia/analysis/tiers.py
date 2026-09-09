#!/usr/bin/env python3
"""Per-tier cost, throughput and resource table for the three instance sizes.

Reads report/throughput.csv and report/tier-resources.json, applies the
us-west-1 rates recorded in September 2026, and writes report/tier-table.json
for the blog chart. Run analysis/throughput.py first so the CSV is current.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOURS = 730
# us-west-1, September 2026: on-demand from artifacts/ec2-prices.json and the
# pricing API, spot from describe-spot-price-history in us-west-1b.
ON_DEMAND = {"t4g.small": 0.02, "c7g.xlarge": 0.1802, "c7g.4xlarge": 0.7208, "c7gd.xlarge": 0.2268, "r7gd.xlarge": 0.3062}
SPOT = {"t4g.small": 0.0093, "c7g.xlarge": 0.0697, "c7g.4xlarge": 0.2222, "c7gd.xlarge": 0.0696, "r7gd.xlarge": 0.1268}
GP3_GB, GP3_IOPS, GP3_THROUGHPUT = 0.096, 0.006, 0.048
S3_GB, S3_GET_PER_1000 = 0.026, 0.0004
INDEX_GB = 81.7 * 1.073741824

S3_STORAGE = INDEX_GB * S3_GB
EBS_DEFAULT = 20 * GP3_GB
EBS_FAST = 120 * GP3_GB + (1000 - 125) * GP3_THROUGHPUT + (16000 - 3000) * GP3_IOPS
EBS_FAST_NO_IOPS = 120 * GP3_GB + (1000 - 125) * GP3_THROUGHPUT

# One row per configuration, nprobe=4 throughout so the rows compare.
# The instance-store rows carry only the 20 GiB root volume: the cache lives
# on the NVMe that comes with the instance. The provisioned-volume rows from the
# first series are kept out of the published table on purpose (the volume alone
# cost more than the instance on spot); their runs remain in runs/.
ROWS = [
    ("t4g.small", "S3", "small-sustained-nprobe4", EBS_DEFAULT, "2 vCPU / 2 GiB"),
    ("c7g.xlarge", "S3", "medium-sustained-cache-nprobe4", EBS_DEFAULT, "4 vCPU / 8 GiB"),
    # The page-cache-friendly budget (2 GiB Go limit, 512 MiB segment cache) is
    # the better of the two c7gd configurations by a small margin.
    ("c7gd.xlarge", "cache", "nvme-c7gd-nvme2-sustained-nprobe4-c16", EBS_DEFAULT, "4 vCPU / 8 GiB, 237 GB NVMe"),
    ("r7gd.xlarge", "cache", "nvme-r7gd-nvme-sustained-nprobe4-c32", EBS_DEFAULT, "4 vCPU / 32 GiB, 237 GB NVMe"),
]


def searches(n):
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    return f"{n / 1e6:.0f}M" if n >= 100e6 else f"{n / 1e6:.1f}M"


def money(v):
    return f"${v:,.3f}" if v < 1 else f"${v:,.2f}"


def html_rows(rows):
    lines = []
    for r in rows:
        label = f"{r['instance']}, {'S3' if r['path'] == 'S3' else 'local cache'}"
        dagger = "&dagger;" if r["errors"] else ""
        lines.append(
            f"<tr><td>{label}</td>"
            f"<td>${r['total_on_demand']:,.2f}<br /><small>${r['ec2_on_demand']:,.2f} + ${r['ebs']:,.2f} + S3</small>"
            f"<br /><small>spot ${r['total_spot']:,.2f}</small></td>"
            f"<td>{searches(r['searches_per_month'])}{dagger}<br /><small>{r['qps']:,.1f} / sec</small></td>"
            f"<td>${r['s3_requests_month']:,.0f}<br /><small>${r['usd_per_million_queries']:.2f} per 1M</small></td>"
            f"<td><strong>${r['all_in_on_demand']:,.0f}</strong><br /><small>spot ${r['all_in_spot']:,.0f}</small></td>"
            f"<td><strong>{money(r['per_million_on_demand'])}</strong><br /><small>per 1M</small><br /><small>spot {money(r['per_million_spot'])}</small></td></tr>")
    return "\n".join("            " + line for line in lines)


def inject(page, rows):
    import re
    text = page.read_text()
    pattern = re.compile(r"(<!-- cost-table:start -->)(.*?)(<!-- cost-table:end -->)", re.S)
    if not pattern.search(text):
        raise SystemExit("cost-table markers not found in " + str(page))
    page.write_text(pattern.sub(lambda m: m.group(1) + "\n" + html_rows(rows) + "\n      " + m.group(3), text, count=1))
    print(f"injected cost table into {page}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--inject", help="HTML file whose cost-table markers receive the rows")
    arguments = parser.parse_args()
    throughput = {r["run"]: r for r in csv.DictReader((ROOT / "report/throughput.csv").open())}
    resources = json.loads((ROOT / "report/tier-resources.json").read_text())
    out = []
    for instance, path, run, ebs, shape in ROWS:
        if run not in throughput or run not in resources:
            print(f"skipping {run}: not archived yet")
            continue
        t, r = throughput[run], resources[run]
        gets = float(t["s3_per_query"])
        row = {
            "instance": instance, "shape": shape, "path": path, "run": run,
            "seconds": float(t["seconds"]), "qps": float(t["qps"]), "p50_ms": float(t["p50_ms"]),
            "p99_ms": float(t["p99_ms"]), "hit10": float(t["hit10"]), "errors": int(t["errors"]),
            "rss_peak_mib": r["rss_peak_mib"], "cpu_mean_pct": r["cpu_mean_pct"],
            "s3_gets_per_query": gets, "usd_per_million_queries": gets * 1e6 / 1000 * S3_GET_PER_1000,
            "ec2_on_demand": ON_DEMAND[instance] * HOURS, "ec2_spot": SPOT[instance] * HOURS,
            "ebs": ebs, "s3_storage": S3_STORAGE,
        }
        row["total_on_demand"] = row["ec2_on_demand"] + ebs + S3_STORAGE
        row["total_spot"] = row["ec2_spot"] + ebs + S3_STORAGE
        # What the box gives you if it runs flat out at the rate it held, and
        # what that many searches cost all in, S3 requests included.
        row["searches_per_month"] = row["qps"] * HOURS * 3600
        row["s3_requests_month"] = row["usd_per_million_queries"] * row["searches_per_month"] / 1e6
        row["all_in_on_demand"] = row["total_on_demand"] + row["s3_requests_month"]
        row["all_in_spot"] = row["total_spot"] + row["s3_requests_month"]
        row["per_million_on_demand"] = row["all_in_on_demand"] / row["searches_per_month"] * 1e6
        row["per_million_spot"] = row["all_in_spot"] / row["searches_per_month"] * 1e6
        out.append(row)
    (ROOT / "report/tier-table.json").write_text(json.dumps(out, indent=1))
    print(f"S3 storage {INDEX_GB:.1f} GB = ${S3_STORAGE:.2f}/mo; EBS default ${EBS_DEFAULT:.2f}; "
          f"fast ${EBS_FAST:.2f} (without extra IOPS ${EBS_FAST_NO_IOPS:.2f})")
    print(f"{'instance':12}{'path':6}{'qps':>8}{'fixed od':>9}{'spot':>8}{'searches/mo':>12}{'S3 req/mo':>10}{'all-in od':>10}{'$/1M od':>8}{'$/1M spot':>10}")
    for r in out:
        print(f"{r['instance']:12}{r['path']:6}{r['qps']:8.1f}{r['total_on_demand']:9.2f}{r['total_spot']:8.2f}"
              f"{searches(r['searches_per_month']):>12}{r['s3_requests_month']:10.0f}{r['all_in_on_demand']:10.0f}"
              f"{r['per_million_on_demand']:8.3f}{r['per_million_spot']:10.3f}")
    if arguments.inject:
        inject(Path(arguments.inject), out)


if __name__ == "__main__":
    main()
