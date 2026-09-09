#!/usr/bin/env python3
"""Regenerate a standalone report and scientific plots from captured run data."""
import argparse
import csv
import datetime as dt
import gzip
import hashlib
import html
import json
from pathlib import Path
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ROOT=Path(__file__).resolve().parents[1]
COLORS={"small":"#176bca","medium":"#e77920","large":"#6a3d9a","nvme-c7gd":"#0d9488","nvme-r7gd":"#b45309"}
def node_of(name):
    for node in sorted(COLORS,key=len,reverse=True):
        if name.startswith(node+"-"):return node
    return None
# Sustained and burst network allowance per instance type, in MiB/s. Sustained
# capacity is set by these, because each query reads whole IVF cells from S3.
NETWORK={"small":(16,596),"medium":(234,1490),"large":(938,1788)}

def timestamp(s):
    # Fractional seconds vary in width between runs; fromisoformat here
    # accepts only exactly three or six digits.
    s=re.sub(r"(T\d{2}:\d{2}:\d{2})\.(\d+)",lambda m:m.group(1)+"."+m.group(2)[:6].ljust(6,"0"),s)
    return dt.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()
def jsonfile(p,default=None):
    try:return json.loads(p.read_text())
    except (OSError,json.JSONDecodeError):return {} if default is None else default

def resource_rows(node):
    p=ROOT/"artifacts"/(node+"-resources.jsonl")
    if not p.exists():return []
    rows=[];previous=None;lastgo={}
    with p.open() as f:
        for line in f:
            try:r=json.loads(line)
            except json.JSONDecodeError:continue
            t=r["timestamp"]
            if previous and previous.get("pid")!=r.get("pid"):lastgo={}
            lastgo=r.get("admin",{}).get("benchmark-metrics",{}).get("go",lastgo)
            cg=r.get("cgroup",{})
            out={"timestamp":t,"node":node,"pid":r.get("pid",0),"rss_mib":r.get("process_status",{}).get("VmRSS",0)/1024,
                 "pss_mib":r.get("smaps_rollup",{}).get("Pss",0)/1024,"host_available_mib":r.get("memory_kib",{}).get("MemAvailable",0)/1024,
                 "heap_mib":lastgo.get("heap_alloc",0)/2**20,"gc_count":lastgo.get("num_gc",0),"cpu_pct":0.0,"network_rx_mib_s":0.0,
                 "disk_read_mib_s":0.0,"disk_write_mib_s":0.0,"disk_busy_pct":0.0,"cgroup_mib":0.0}
            try:out["cgroup_mib"]=int(cg.get("memory.current","0"))/2**20
            except ValueError:pass
            if previous:
                elapsed=t-previous["timestamp"]
                if elapsed>0:
                    if r.get("pid") and r.get("pid")==previous.get("pid") and r.get("process_cpu_ticks") and previous.get("process_cpu_ticks"):
                        ticks=sum(r["process_cpu_ticks"][k]-previous["process_cpu_ticks"][k] for k in ["user","system"])
                        out["cpu_pct"]=max(0,100*ticks/(r["clock_ticks"]*elapsed*r["cpu_count"]))
                    now=sum(v[0] for k,v in r.get("network",{}).items() if k!="lo")
                    old=sum(v[0] for k,v in previous.get("network",{}).items() if k!="lo")
                    out["network_rx_mib_s"]=max(0,(now-old)/elapsed/2**20)
                    disks={d[2]:d for d in r.get("diskstats",[]) if re.fullmatch(r"nvme\d+n\d+",d[2])}
                    for d in previous.get("diskstats",[]):
                        if d[2] not in disks:continue
                        nxt=disks[d[2]]
                        out["disk_read_mib_s"]+=max(0,(int(nxt[5])-int(d[5]))*512/elapsed/2**20)
                        out["disk_write_mib_s"]+=max(0,(int(nxt[9])-int(d[9]))*512/elapsed/2**20)
                        out["disk_busy_pct"]+=max(0,(int(nxt[12])-int(d[12]))/(elapsed*10))
            rows.append(out);previous=r
    return rows

def phase(name):
    if "warmup" in name:return "warmup"
    for tag in ["formal","arrival","overhead","soak","burst","restart","tuning","quality","regression","rate-limit","tuned","first-touch","pilot","direct-s3","large-memory","baseline","ceiling","sustained","fastdisk","tier","optimize"]:
        if tag in name:return tag
    return "setup"

def run_rows(resources):
    rows=[]
    for p in sorted((ROOT/"runs").glob("*/summary.json")):
        s=jsonfile(p);c=s["config"];node=node_of(p.parent.name)
        if node is None:continue
        before=jsonfile(p.parent/"node-before.json");after=jsonfile(p.parent/"node-after.json")
        bs3=before.get("benchmark-metrics",{}).get("s3",{});as3=after.get("benchmark-metrics",{}).get("s3",{})
        start=timestamp(s["started_at"]);end=start+s["elapsed_seconds"]
        rr=[r for r in resources.get(node,[]) if start<=r["timestamp"]<=end]
        build=before.get("build",{})
        row={"run":p.parent.name,"node":node,"phase":phase(p.parent.name),"mix":c["Distribution"],"concurrency":c["Concurrency"],
             "rate":c["Rate"],"mode":c["Mode"],"k":c["K"],"nprobe":c["NProbe"],"start":start,"end":end,"seconds":s["elapsed_seconds"],
             "scheduled":s["scheduled"],"success":s["success"],"errors":s["errors"],"error_pct":100*s["errors"]/max(1,s["scheduled"]),
             "qps":s["successful_qps"],"p50_ms":s["success_latency_ms"]["p50"],"p95_ms":s["success_latency_ms"]["p95"],"p99_ms":s["success_latency_ms"]["p99"],
             "schedule_p99_ms":s["schedule_latency_ms"]["p99"],"dispatch_p99_ms":s["dispatch_delay_ms"]["p99"],"goodput_1s_qps":s["goodput_1000ms_qps"],
             "hit1":s["hit_at"].get("1",0),"hit3":s["hit_at"].get("3",0),"hit10":s["hit_at"].get("10",0),"mrr":s["mrr_at_k"],
             "rss_peak_mib":max((r["rss_mib"] for r in rr),default=0),"cgroup_peak_mib":max((r["cgroup_mib"] for r in rr),default=0),
             "cpu_mean_pct":float(np.mean([r["cpu_pct"] for r in rr])) if rr else 0,"disk_busy_mean_pct":float(np.mean([r["disk_busy_pct"] for r in rr])) if rr else 0,
             "disk_read_mean_mib_s":float(np.mean([r["disk_read_mib_s"] for r in rr])) if rr else 0,"disk_write_mean_mib_s":float(np.mean([r["disk_write_mib_s"] for r in rr])) if rr else 0,
             "s3_attempts":max(0,as3.get("attempts",0)-bs3.get("attempts",0)),"s3_bytes":max(0,as3.get("bytes",0)-bs3.get("bytes",0)),
             "s3_canceled":max(0,as3.get("canceled_attempts",0)-bs3.get("canceled_attempts",0)),"s3_retries":max(0,as3.get("sdk_retry_attempts",0)-bs3.get("sdk_retry_attempts",0)),
             "s3_hedges":max(0,as3.get("hedge_launches",0)-bs3.get("hedge_launches",0)),"mutations":as3.get("blocked_mutations",0),
             "disk_cache":build.get("disk_cache",before.get("overview",{}).get("config",{}).get("has_disk_cache")),"memory_max":build.get("memory_max","1536M"),
             "binary_sha256":build.get("binary_sha256",""),"aborted":s.get("aborted",False),"abort_reason":s.get("abort_reason","")}
        row["s3_attempts_per_query"]=row["s3_attempts"]/max(1,row["success"])
        row["s3_mib_per_query"]=row["s3_bytes"]/2**20/max(1,row["success"])
        rows.append(row)
    return rows

def save_chart(fig,out,name):
    fig.savefig(out/"charts"/(name+".svg"),bbox_inches="tight")
    fig.savefig(out/"charts"/(name+".png"),dpi=170,bbox_inches="tight")
    plt.close(fig)

def charts(rows,resources,out):
    (out/"charts").mkdir(exist_ok=True)
    figs=[]
    for group,label in [("pilot","Original disk-cache configuration"),("direct-s3","Direct-S3 diagnostic"),("formal","Formal repeated measurements")]:
        candidates=[r for r in rows if r["phase"]==group and r["seconds"]>=30]
        if not candidates:continue
        fig,axes=plt.subplots(1,2,figsize=(11,4))
        for node in COLORS:
            for mix,style in [("uniform","-"),("zipf","--")]:
                rs=[r for r in candidates if r["node"]==node and r["mix"]==mix]
                if not rs:continue
                grid=sorted({r["concurrency"] for r in rs})
                for ax,metric,ylabel in [(axes[0],"qps","Successful queries / second"),(axes[1],"p99_ms","p99 latency (ms)")]:
                    vals=[[r[metric] for r in rs if r["concurrency"]==c] for c in grid]
                    means=[np.median(v) for v in vals]
                    ax.plot(grid,means,style+"o",color=COLORS[node],label=node+" / "+mix)
                    ax.fill_between(grid,[min(v) for v in vals],[max(v) for v in vals],color=COLORS[node],alpha=.14)
                    ax.set_xlabel("Concurrent requests");ax.set_ylabel(ylabel);ax.grid(alpha=.2)
        axes[0].legend(fontsize=8);axes[1].axhline(1000,color="#666",ls=":",label="1 s target")
        fig.suptitle(label);fig.tight_layout();name=group+"-capacity";save_chart(fig,out,name);figs.append(name)
    fig,axes=plt.subplots(4,1,figsize=(11,11),sharex=True)
    earliest=min((r["start"] for r in rows),default=0)
    for node,color in COLORS.items():
        rs=resources[node]
        if not rs:continue
        # Plot short-window averages for legibility; raw one-second rows are retained.
        for ax,key,label in zip(axes,["cpu_pct","rss_mib","cgroup_mib","disk_write_mib_s"],["Process CPU (% of host)","Process RSS (MiB)","Cgroup memory (MiB)","Disk writes (MiB/s)"]):
            stride=max(1,len(rs)//1500)
            xs=[];ys=[]
            for i in range(0,len(rs),stride):
                block=rs[i:i+stride];xs.append((block[0]["timestamp"]-earliest)/3600);ys.append(np.mean([r[key] for r in block]))
            ax.plot(xs,ys,color=color,label=node,linewidth=.9);ax.set_ylabel(label);ax.grid(alpha=.2)
    axes[0].legend();axes[-1].set_xlabel("Hours since first recorded test")
    fig.suptitle("Resource utilization throughout the tests");fig.tight_layout();save_chart(fig,out,"resources");figs.append("resources")
    qa=[r for r in rows if r["phase"]=="quality" and "baseline" in r["run"]]
    if qa:
        fig,ax=plt.subplots(figsize=(8,4));x=np.arange(3)
        for i,r in enumerate(qa):ax.bar(x+(i-.5)*.32,[r["hit1"],r["hit3"],r["hit10"]],width=.32,label=r["node"],color=COLORS[r["node"]])
        ax.set_xticks(x,["Answer presence@1","@3","@10"]);ax.set_ylim(0,1);ax.set_ylabel("Fraction of 300 questions");ax.legend();ax.set_title("NQ-Open answer-containing passage retrieval")
        fig.tight_layout();save_chart(fig,out,"accuracy");figs.append("accuracy")
    return figs

def table(rows,columns):
    result="<table><thead><tr>"+"".join("<th>"+html.escape(label)+"</th>" for _,label in columns)+"</tr></thead><tbody>"
    for row in rows:
        result+="<tr>"
        for key,_ in columns:
            v=row.get(key,"")
            if isinstance(v,float):v=f"{v:,.2f}"
            result+="<td>"+html.escape(str(v))+"</td>"
        result+="</tr>"
    return result+"</tbody></table>"

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--final",action="store_true");args=ap.parse_args()
    out=ROOT/"report";out.mkdir(exist_ok=True)
    events=[json.loads(s) for s in (ROOT/"artifacts/events.jsonl").read_text().splitlines()] if (ROOT/"artifacts/events.jsonl").exists() else []
    complete=any(e["event"]=="formal-complete" for e in events)
    if args.final and not complete:raise SystemExit("The formal suite is not complete; only a preliminary report may be generated.")
    resources={node:resource_rows(node) for node in ["small","medium","large","driver","nvme-c7gd","nvme-r7gd","driver2"]}
    rows=run_rows(resources)
    if not rows:raise SystemExit("No completed runs")
    for node,rs in resources.items():
        if rs:pq.write_table(pa.Table.from_pylist(rs),out/(node+"-resources.parquet"),compression="zstd")
    with (out/"summary.csv").open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    pq.write_table(pa.Table.from_pylist(rows),out/"summary.parquet",compression="zstd")
    figs=charts(rows,resources,out)
    state="Completed" if complete else "Preliminary — tests are still running"
    headline=[]
    for node in COLORS:
        candidates=[r for r in rows if r["node"]==node and r["phase"] in ["formal","arrival","soak"] and not r["aborted"] and r["error_pct"]<=.1 and r["schedule_p99_ms"]<=1000]
        if candidates:headline.append(max(candidates,key=lambda r:r["qps"]))
    content=f"<p class='status'>{state}</p><h1>Wikipedia read benchmark · Polign 0.5.0</h1><p>12,519,135 existing passages · 384 dimensions · squared L2 · generation 45 · us-west-1</p>"
    content+="<p>Separate c7g.2xlarge client → private HTTP database endpoint → unchanged Wikipedia S3 bucket. Query embedding is excluded from database timing. Full result metadata is included.</p>"
    content+="<h2>Capacity within a 1-second p99 target</h2>"
    if headline:content+=table(headline,[("node","Node"),("run","Evidence run"),("qps","Successful QPS"),("p50_ms","p50 ms"),("schedule_p99_ms","Scheduled p99 ms"),("error_pct","Errors %"),("rss_peak_mib","Peak RSS MiB"),("cpu_mean_pct","CPU % of host")])
    else:content+="<p>No completed formal run has yet established capacity within this target. Pilot measurements are shown separately below.</p>"
    content+="<h2>Hardware and measurement controls</h2><p>Small: t4g.small, 2 vCPU / 2 GiB, Graviton2, unlimited CPU credits. Medium: c7g.xlarge, 4 vCPU / 8 GiB, Graviton3. Matched runs use a 1,536 MiB cgroup memory budget and 256 MiB segment cache; tuned runs explicitly increase the medium node's budgets. The source index has no PQ codebook. CPU, RAM, and instance I/O limits differ between sizes.</p>"
    content+="<p>Queries use nprobe=8, k=10, cold=true unless the run name and CSV state otherwise. cold=true selects the storage-backed path; it does not imply a cache miss. HTTP latency spans send through full response-body receipt; scheduled latency additionally includes dispatch/queue delay. Failed requests count against success rate and goodput. Short runs have limited evidence for p99; consult sample counts and repeat/soak results.</p>"
    content+="<h2>Measured curves and resource timelines</h2>"
    for name in figs:
        svg=(out/"charts"/(name+".svg")).read_text();svg=svg[svg.index("<svg"):]
        content+="<figure>"+svg+"</figure>"
    qualities=[r for r in rows if r["phase"] in ["quality","regression"]]
    content+="<h2>Retrieval accuracy</h2><p>The larger evaluation uses 300 published NQ-Open dev questions. A hit means a returned passage contains a token-bounded annotated answer; this is a retrieval proxy, not generated-answer accuracy or a complete relevance judgment. Short/common answers may produce false positives, and older questions can disagree with the 2023 corpus. The original 30-query regression instead matches acceptable article titles. Exact ANN recall and graded nDCG were not measured.</p>"
    content+=table(qualities,[("run","Run"),("scheduled","Questions"),("hit1","Hit@1"),("hit3","Hit@3"),("hit10","Hit@10"),("mrr","MRR@k"),("errors","Errors")])
    operational=[r for r in rows if r["phase"] in ["soak","burst","restart","rate-limit","tuned"]]
    content+="<h2>Operational behavior</h2>"+table(operational,[("run","Run"),("seconds","Seconds"),("qps","QPS"),("schedule_p99_ms","Scheduled p99 ms"),("error_pct","Errors %"),("rss_peak_mib","Peak RSS MiB"),("aborted","Aborted")])
    content+="<h2>Reproducibility and limits</h2><p>Both servers use release 0.5.0 plus a separately recorded telemetry patch; paired checks against the official release quantify overhead. S3 counters are recorded below hedging/retries, with canceled attempts separated from failures. The instance role denies bucket mutations, and the driver has no write operation. Source and build fingerprints are recorded in dataset.json and per-run node snapshots. This fixed-corpus read test does not establish write durability, larger-corpus scaling, HA, or uninterrupted failover.</p>"
    content+="<p>Artifacts: summary.csv, summary.parquet, per-host resource Parquet, vector/query checksums, request logs, node snapshots, and SVG/PNG charts. The raw run directory contains setup failures and diagnostics as well as formal results; these are labeled separately.</p>"
    content+="<p>Sources: <a href='https://github.com/google-research-datasets/natural-questions/tree/master/nq_open'>NQ-Open</a>; <a href='https://github.com/Polign/polign/releases/tag/v0.5.0'>Polign 0.5.0</a>; <a href='https://aws.amazon.com/ec2/instance-types/t4/'>T4g</a>; <a href='https://aws.amazon.com/ec2/instance-types/c7g/'>C7g</a>.</p>"
    content+="<h2>All completed runs</h2>"+table(rows,[("run","Run"),("phase","Phase"),("seconds","Seconds"),("success","Successes"),("qps","QPS"),("p99_ms","p99 ms"),("error_pct","Errors %"),("s3_attempts_per_query","S3 attempts/query"),("s3_mib_per_query","S3 MiB/query")])
    css="body{font:15px/1.6 system-ui,sans-serif;max-width:1200px;margin:40px auto;padding:0 24px;color:#172234}h1{font-size:34px}h2{margin-top:36px}table{border-collapse:collapse;width:100%;font-size:12px;display:block;overflow:auto}td,th{padding:7px;border-bottom:1px solid #ddd;text-align:left}th{background:#edf2f7}figure{margin:25px 0}svg{max-width:100%;height:auto}.status{font-weight:700;color:#9a4b00}@media print{body{margin:0;max-width:none;font-size:10pt}figure{break-inside:avoid}table{font-size:8pt}h2{break-after:avoid}}"
    (out/"index.html").write_text("<!doctype html><html lang='en'><meta charset='utf-8'><title>Wikipedia read benchmark — Polign 0.5.0</title><style>"+css+"</style><body>"+content+"</body></html>")
    findings=["# Wikipedia read benchmark — Polign 0.5.0","",state,"",f"{len(rows)} completed runs; 12,519,135 existing passages; no corpus vectors regenerated.","","See index.html and summary.csv for the measured curves and complete run table.","","Capacity claims require the stated latency/error target, run duration, cache configuration, and sample count. Pilot figures are provisional."]
    (out/"findings.md").write_text("\n".join(findings)+"\n")
    (out/"checksums.sha256").write_text("\n".join(hashlib.sha256(p.read_bytes()).hexdigest()+"  "+str(p.relative_to(out)) for p in sorted(out.rglob("*")) if p.is_file() and p.name!="checksums.sha256")+"\n")
    print("Report generated:",out/"index.html",";",len(rows),"completed runs")

if __name__=="__main__":main()
