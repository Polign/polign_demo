#!/usr/bin/env python3
"""Run read-only phases on Server X; control only the two tagged test nodes."""
import argparse
import datetime
import json
from pathlib import Path
import random
import shlex
import subprocess
import time
import urllib.request

ROOT=Path("/opt/wikibench")
NODES={"small":"172.31.21.194","medium":"172.31.23.209","large":"172.31.22.94"}
# A later series of instances is addressed from a file next to the suite.
if (ROOT/"nodes.json").exists():
    NODES.update(json.loads((ROOT/"nodes.json").read_text()))
PAIR=("small","medium")
KEY=ROOT/"benchmark-key"
EVENTS=ROOT/"events.jsonl"

def event(kind,**values):
    row={"time":datetime.datetime.now(datetime.timezone.utc).isoformat(),"event":kind,**values}
    with EVENTS.open("a") as f:f.write(json.dumps(row)+"\n")
    print(json.dumps(row),flush=True)

def remote(node,command,input=None):
    return subprocess.run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=8","-o","StrictHostKeyChecking=accept-new","-i",str(KEY),"ec2-user@"+NODES[node],command],input=input,text=True,capture_output=True,check=True).stdout

def snapshot(node):
    result={}
    for name in ["overview","collections","benchmark-metrics"]:
        try:result[name]=json.loads(remote(node,"curl --fail --silent --max-time 3 http://127.0.0.1:23002/api/"+name))
        except Exception as e:result[name]={"error":str(e)}
    try:result["build"]=json.loads(remote(node,"cat /opt/wikibench/current-build.json"))
    except Exception:pass
    return result

def configure(node,variant="instrumented",fresh=False,tuned=False,rate=0,disk_cache=False,hedge_ms=150,cache=0,limit="",hard="",disk_cache_bytes=6442450944):
    cache=cache or (2147483648 if tuned else 268435456)
    limit=limit or ("5120MiB" if tuned else "1000MiB")
    hard=hard or ("6144M" if tuned else "1536M")
    command=f"/opt/wikibench/polign-server-{variant} -segment-stores s3://polign-demo-wiki-en-uw1/polign-v4 -cold-first=true -restore-stores \"\" -log-stores \"\" -persist=false -maintain 0 -hot-max 0 -tail-fresh=false -split-qps 0 -placement-refresh 0 -http {NODES[node]}:23000 -grpc 127.0.0.1:23001 -admin 127.0.0.1:23002 -disk-cache-dir /var/lib/polign/benchmark-cache -disk-cache-bytes {disk_cache_bytes} -segment-cache-bytes {cache} -segment-refresh 5m -hedge-reads 150ms -rate-limit {rate}"
    if not disk_cache:command=command.replace(f"-disk-cache-dir /var/lib/polign/benchmark-cache -disk-cache-bytes {disk_cache_bytes}",'-disk-cache-dir "" -disk-cache-bytes 0')
    command=command.replace("-hedge-reads 150ms",f"-hedge-reads {hedge_ms}ms")
    content=f"[Service]\nExecStart=\nExecStart={command}\nEnvironment=GOMEMLIMIT={limit}\nEnvironment=POLIGN_BENCH_TELEMETRY=1\nMemoryMax={hard}\n"
    script="import pathlib,subprocess,shutil,os\nsubprocess.run(['systemctl','stop','wikibench'],check=True)\n"
    if fresh:
        script+="p=pathlib.Path('/var/lib/polign/benchmark-cache')\nassert not p.is_symlink()\nfor c in p.iterdir():\n    shutil.rmtree(c) if c.is_dir() and not c.is_symlink() else c.unlink()\nshutil.chown(p,user='ec2-user',group='ec2-user')\n"
    script+="p=pathlib.Path('/etc/systemd/system/wikibench.service.d')\np.mkdir(exist_ok=True)\n(p/'benchmark.conf').write_text("+repr(content)+")\nsubprocess.run(['systemctl','daemon-reload'],check=True)\nsubprocess.run(['systemctl','start','wikibench'],check=True)\n"
    started=time.monotonic()
    remote(node,"sudo python3.11 -",script)
    binary_hash=remote(node,"sha256sum /opt/wikibench/polign-server-"+variant).split()[0]
    build={"release":"0.5.0","variant":variant,"binary_sha256":binary_hash,"command":command,"gomemlimit":limit,"memory_max":hard,"tuned":tuned,"disk_cache":disk_cache,"hedge_ms":hedge_ms,"segment_cache_bytes":cache,"disk_cache_bytes":disk_cache_bytes if disk_cache else 0}
    remote(node,"python3.11 -","from pathlib import Path\nPath('/opt/wikibench/current-build.json').write_text("+repr(json.dumps(build))+")\n")
    for _ in range(60):
        try:
            with urllib.request.urlopen("http://"+NODES[node]+":23000/healthz",timeout=1) as r:
                if r.status==200:break
        except Exception:time.sleep(1)
    else:raise RuntimeError("server did not become healthy: "+node)
    event("configured",node=node,variant=variant,fresh=fresh,tuned=tuned,disk_cache=disk_cache,hedge_ms=hedge_ms,rate_limit=rate,health_seconds=time.monotonic()-started)

def run(node,label,queries="load",concurrency=1,duration=0,requests=0,rate=0,distribution="uniform",mode="semantic",k=10,nprobe=8,timeout=10,seed=20260906,allow_errors=False,metadata_k=0,binary=None):
    out=ROOT/"runs"/(node+"-"+label)
    if (out/"summary.json").exists():
        return json.loads((out/"summary.json").read_text())
    if out.exists():raise RuntimeError("Incomplete run requires review: "+str(out))
    before=snapshot(node)
    cmd=[str(binary or ROOT/"benchmark"),"-endpoint","http://"+NODES[node]+":23000","-queries",str(ROOT/"fixtures"/(queries+".vectors.jsonl")),"-out",str(out),"-concurrency",str(concurrency),"-mode",mode,"-distribution",distribution,"-k",str(k),"-nprobe",str(nprobe),"-timeout",str(timeout)+"s","-seed",str(seed)]
    if metadata_k:cmd+=["-metadata-k",str(metadata_k)]
    if duration:cmd+=["-duration",str(duration)+"s"]
    if requests:cmd+=["-requests",str(requests)]
    if rate:cmd+=["-rate",str(rate)]
    event("run-start",node=node,label=label,command=shlex.join(cmd))
    with (ROOT/"driver-output.log").open("a") as log:
        proc=subprocess.Popen(cmd,stdout=log,stderr=log)
        (ROOT/"driver.pid").write_text(str(proc.pid))
        aborted=""
        unavailable=0
        pressure_ticks=0
        while proc.poll() is None:
            try:
                proc.wait(timeout=10)
                break
            except subprocess.TimeoutExpired:
                pass
            try:
                sample=json.loads(remote(node,"tail -n 1 /opt/wikibench/resources.jsonl"))
                unavailable=unavailable+1 if not sample.get("pid") else 0
                if time.time()-sample.get("timestamp",0)>30:unavailable+=1
                cg=sample.get("cgroup",{})
                stat={line.split()[0]:int(line.split()[1]) for line in cg.get("memory.stat","").splitlines()}
                hard=int(cg.get("memory.max","0"))
                nonreclaimable=stat.get("anon",0)+stat.get("kernel",0)
                pressure_ticks=pressure_ticks+1 if hard and nonreclaimable>hard*.95 else 0
                if unavailable>=2:aborted="database unavailable or telemetry stale"
                if pressure_ticks>=3:aborted="nonreclaimable memory above 95% budget for 30 seconds"
                if sample.get("disk_free_bytes",0)<2*1024**3:aborted="less than 2 GiB disk space remaining"
            except Exception:
                unavailable+=1
                if unavailable>=3:aborted="unable to monitor database"
            if aborted:
                event("run-abort",node=node,label=label,reason=aborted)
                proc.terminate()
                try:proc.wait(timeout=15)
                except subprocess.TimeoutExpired:proc.kill()
                break
        code=proc.wait()
        (ROOT/"driver.pid").write_text("0")
    if code:raise RuntimeError(f"driver exit {code}: {label}")
    result=json.loads((out/"summary.json").read_text())
    if aborted:
        result["aborted"]=True
        result["abort_reason"]=aborted
        (out/"summary.json").write_text(json.dumps(result,indent=2))
    after=snapshot(node)
    (out/"node-before.json").write_text(json.dumps(before))
    (out/"node-after.json").write_text(json.dumps(after))
    event("run-done",node=node,label=label,qps=result["successful_qps"],p99=result["success_latency_ms"]["p99"],success=result["success"],errors=result["errors"],seconds=result["elapsed_seconds"])
    s3=after.get("benchmark-metrics",{}).get("s3",{})
    if s3.get("blocked_mutations",0):raise RuntimeError("S3 mutation attempted")
    generations=[s["generation"] for s in after.get("collections",{}).get("searchers",[])]
    if generations and any(g!=45 for g in generations):raise RuntimeError("source generation changed")
    if not allow_errors and result["errors"]>max(1,result["scheduled"]*.05):raise RuntimeError("failure threshold exceeded: "+label)
    if aborted:
        if not allow_errors:raise RuntimeError(aborted)
        configure(node)
    return result

def pilot():
    for node in PAIR:
        configure(node,fresh=True,disk_cache=True)
        run(node,"first-touch",queries="regression",requests=1,distribution="sequential",timeout=60)
        run(node,"regression-semantic",queries="regression",requests=30,distribution="sequential")
        run(node,"pilot-warmup",duration=60,concurrency=2)
        points=[]
        for c in [1,2,4,8,16,32,64]:
            r=run(node,f"pilot-c{c}",duration=60,concurrency=c,allow_errors=True)
            points.append({"concurrency":c,"qps":r["successful_qps"],"p99":r["success_latency_ms"]["p99"],"errors":r["errors"],"requests":r["scheduled"]})
            if r["errors"]>r["scheduled"]*.05 or (len(points)>2 and points[-1]["qps"]<points[-2]["qps"]*.8):break
        (ROOT/(node+"-pilot.json")).write_text(json.dumps(points,indent=2))
    event("pilot-complete")

def formal():
    protocol={"date":datetime.datetime.now(datetime.timezone.utc).isoformat(),"version":"0.5.0","repetitions":3,"warmup_seconds":120,"measurement_seconds":600,"soak_seconds":7200,"primary_profile":{"disk_cache":False,"segment_cache_mib":256,"memory_max_mib":1536},"profile_rationale":"Pilot isolated disk-cache I/O bottleneck; primary direct-S3 profile selected before formal measurements. Original disk-cache measurements retained with additional ten-minute controls.","targets_ms":{"repeated":250,"broad_primary":1000,"broad_secondary":[2000,5000]},"nodes":{}}
    for node in PAIR:
        points=[]
        for path in (ROOT/"runs").glob(node+"-direct-s3-c*/summary.json"):
            r=json.loads(path.read_text())
            points.append({"concurrency":r["config"]["Concurrency"],"qps":r["successful_qps"],"p99":r["success_latency_ms"]["p99"],"errors":r["errors"],"requests":r["scheduled"]})
        points.sort(key=lambda p:p["concurrency"])
        good=[p for p in points if p["requests"] and p["errors"]/p["requests"]<=.001]
        if not good:raise RuntimeError("No successful pilot configuration: "+node)
        best=max(good,key=lambda p:p["qps"])
        knee=best["concurrency"]
        grid=[1,2,4,8] if node=="small" else [1,4,8,16]
        protocol["nodes"][node]={"concurrencies":grid,"pilot_capacity_qps":best["qps"],"knee":knee,"arrival_concurrency":16 if node=="small" else 32}
    frozen=ROOT/"frozen-protocol.json"
    if frozen.exists():protocol=json.loads(frozen.read_text())
    else:frozen.write_text(json.dumps(protocol,indent=2))
    event("formal-protocol",protocol=protocol)
    # Paired official/instrumented checks use identical warmed query fixtures.
    for node in PAIR:
        for repeat in range(3):
            variants=["official","instrumented"] if repeat%2==0 else ["instrumented","official"]
            for variant in variants:
                configure(node,variant=variant)
                run(node,f"overhead-r{repeat}-{variant}-warmup",queries="regression",duration=30,concurrency=2)
                run(node,f"overhead-r{repeat}-{variant}",queries="regression",duration=60,concurrency=2)
        configure(node)
        run(node,"quality-baseline",queries="quality",requests=300,distribution="sequential")
        for mode in ["keyword","hybrid"]:
            run(node,"regression-"+mode,queries="regression",requests=30,distribution="sequential",mode=mode,allow_errors=True)
        configure(node,disk_cache=True)
        run(node,"disk-cache-control-warmup",duration=120,concurrency=2,allow_errors=True)
        run(node,"disk-cache-control",duration=600,concurrency=2,allow_errors=True)
        configure(node)
    for repeat in range(protocol["repetitions"]):
        order=list(PAIR) if repeat%2==0 else list(reversed(PAIR))
        jobs=[]
        for node in order:
            for mix in ["uniform","zipf"]:
                for c in protocol["nodes"][node]["concurrencies"]:jobs.append((node,mix,c))
        random.Random(20260906+repeat).shuffle(jobs)
        for node,mix,c in jobs:
            label=f"formal-r{repeat}-{mix}-c{c}"
            run(node,label+"-warmup",duration=120,concurrency=c,distribution=mix,seed=20260906+repeat,allow_errors=True)
            run(node,label,duration=600,concurrency=c,distribution=mix,seed=20260906+repeat,allow_errors=True)
    for node in PAIR:
        base=protocol["nodes"][node]
        # Arrival-rate sweeps expose queues and refusals without coordinated omission.
        for repeat in range(3):
            for ratio in [.5,.75,.9,1,1.25]:
                run(node,f"arrival-r{repeat}-{ratio}",duration=300,concurrency=base["arrival_concurrency"],rate=base["pilot_capacity_qps"]*ratio,seed=20260906+repeat,allow_errors=True)
        accepted=[]
        for path in (ROOT/"runs").glob(node+"-arrival-*/summary.json"):
            r=json.loads(path.read_text())
            if not r.get("aborted") and r["errors"]<=r["scheduled"]*.001 and r["schedule_latency_ms"]["p99"]<=5000:
                accepted.append(r["successful_qps"])
        capacity=max(accepted,default=base["pilot_capacity_qps"]*.5)
        (ROOT/(node+"-capacity.json")).write_text(json.dumps({"qps_at_secondary_5s_target":capacity,"primary_1s_target_evaluated_separately":True}))
        for repeat in range(3):
            run(node,f"burst-r{repeat}-baseline",duration=60,concurrency=base["arrival_concurrency"],rate=capacity*.5,allow_errors=True)
            run(node,f"burst-r{repeat}-peak",duration=30,concurrency=base["arrival_concurrency"],rate=capacity*2,allow_errors=True)
            run(node,f"burst-r{repeat}-recovery",duration=300,concurrency=base["arrival_concurrency"],rate=capacity*.5,allow_errors=True)
        run(node,"soak",duration=7200,concurrency=base["arrival_concurrency"],rate=capacity*.75,allow_errors=True)
        run(node,"quality-after-soak",queries="quality",requests=300,distribution="sequential",concurrency=base["knee"])
        for repeat in range(3):
            for fresh in [False,True]:
                configure(node,fresh=fresh,disk_cache=True)
                run(node,f"restart-r{repeat}-fresh{fresh}",queries="regression",requests=30,distribution="sequential",timeout=60)
        configure(node)
        for probe in [1,2,4,8,16,32,64]:
            run(node,f"tuning-nprobe{probe}",queries="tuning",requests=100,distribution="sequential",nprobe=probe,allow_errors=True)
        configure(node,rate=max(1,int(capacity*.5)))
        run(node,"rate-limit-control",duration=60,concurrency=32,rate=capacity,allow_errors=True)
        configure(node)
    configure("medium",tuned=True)
    for mix in ["uniform","zipf"]:
        run("medium","tuned-"+mix+"-warmup",duration=120,concurrency=protocol["nodes"]["medium"]["knee"],distribution=mix)
        run("medium","tuned-"+mix,duration=600,concurrency=protocol["nodes"]["medium"]["knee"],distribution=mix)
    event("formal-complete")

def diagnostics():
    for node in PAIR:
        configure(node,disk_cache=False)
        run(node,"direct-s3-warmup",duration=30,concurrency=2,allow_errors=True)
        for c in [1,2,4,8]:
            run(node,f"direct-s3-c{c}",duration=60,concurrency=c,allow_errors=True)
        configure(node,disk_cache=True)
    configure("medium",tuned=True,disk_cache=True)
    run("medium","large-memory-warmup",duration=60,concurrency=2,allow_errors=True)
    for c in [1,2,4,8]:
        run("medium",f"large-memory-c{c}",duration=60,concurrency=c,allow_errors=True)
    configure("medium")
    event("diagnostics-complete")

def optimize():
    # Read-only runtime interventions; identical seeds and fixtures within pairs.
    for node in PAIR:
        c=4 if node=="small" else 8
        for hedge in [150,0]:
            configure(node,hedge_ms=hedge)
            run(node,f"optimize-hedge{hedge}-warmup",duration=30,concurrency=c,allow_errors=True)
            run(node,f"optimize-hedge{hedge}",duration=120,concurrency=c,allow_errors=True)
        configure(node)
        for probe in [1,2,4,8]:
            run(node,f"optimize-nprobe{probe}-quality",queries="tuning",requests=100,distribution="sequential",nprobe=probe,allow_errors=True)
            run(node,f"optimize-nprobe{probe}-load",duration=60,concurrency=c,nprobe=probe,allow_errors=True)
    configure("medium",tuned=True)
    for mix in ["uniform","zipf"]:
        run("medium",f"optimize-memory-{mix}-warmup",duration=60,concurrency=8,distribution=mix,allow_errors=True)
        for c in [4,8,16]:
            run("medium",f"optimize-memory-{mix}-c{c}",duration=120,concurrency=c,distribution=mix,allow_errors=True)
    configure("medium")
    run("medium","direct-s3-c16",duration=60,concurrency=16,allow_errors=True)
    event("optimize-complete")

def throughput():
    # Round two: separate burst capacity from sustained capacity. Short runs
    # spend the instance's network burst allowance; ten-minute runs settle at
    # the baseline allowance, which is what a real deployment sees.
    # Burst block runs first, while the idle period has restored the allowance.
    configure("medium",hedge_ms=0)
    for c in [8,16,32,64]:
        run("medium",f"ceiling-nprobe1-c{c}",duration=90,concurrency=c,nprobe=1,allow_errors=True)
    for probe in [4,8]:
        for c in [16,32]:
            run("medium",f"ceiling-nprobe{probe}-c{c}",duration=90,concurrency=c,nprobe=probe,allow_errors=True)
    # Sustained block: each ten-minute run ends with the burst allowance spent,
    # so the profiles are compared against each other in the same state.
    for name,cfg,probe in [("base",{"hedge_ms":150},8),("hedge0",{"hedge_ms":0},8),
                           ("cache",{"tuned":True,"hedge_ms":0},8),
                           ("cache-nprobe4",{"tuned":True,"hedge_ms":0},4),
                           ("cache-nprobe2",{"tuned":True,"hedge_ms":0},2)]:
        configure("medium",**cfg)
        run("medium",f"sustained-{name}-warmup",duration=60,concurrency=8,nprobe=probe,allow_errors=True)
        run("medium",f"sustained-{name}",duration=600,concurrency=8,nprobe=probe,allow_errors=True)
    configure("medium")
    # One sustained small-node run to quantify its baseline network allowance.
    configure("small",hedge_ms=0)
    run("small","sustained-nprobe4-warmup",duration=60,concurrency=4,nprobe=4,allow_errors=True)
    run("small","sustained-nprobe4",duration=600,concurrency=4,nprobe=4,allow_errors=True)
    configure("small")
    event("throughput-complete")

GiB=1024**3

def storage():
    # The pilot's disk cache lost to plain S3 because the volume was capped at
    # 125 MiB/s, below the instance's own sustained network allowance. The
    # volume now supplies 1000 MiB/s, so the cache is measured on equal terms
    # and sized to hold the whole 81.7 GiB index.
    configure("medium",fresh=True,disk_cache=True,disk_cache_bytes=80*GiB,cache=2*GiB,limit="5120MiB",hard="6144M",hedge_ms=0)
    run("medium","fastdisk-warm",duration=600,concurrency=8,allow_errors=True)
    run("medium","fastdisk-nprobe8",duration=600,concurrency=8,allow_errors=True)
    run("medium","fastdisk-nprobe4",duration=600,concurrency=8,nprobe=4,allow_errors=True)
    for c in [16,32]:
        run("medium",f"fastdisk-nprobe8-c{c}",duration=300,concurrency=c,allow_errors=True)
    configure("medium")
    event("storage-complete")

def tier():
    # c7g.4xlarge carries 7.5 Gb/s sustained against c7g.xlarge's 1.875 Gb/s.
    # Segment cache and hedging match the medium profile so the instance's
    # network allowance is the only variable that moves.
    big=dict(hedge_ms=0,cache=2*GiB,limit="20GiB",hard="26G")
    configure("large",**big)
    for c in [8,16,32,64]:
        run("large",f"tier-nprobe8-c{c}",duration=90,concurrency=c,allow_errors=True)
    run("large","tier-sustained-warmup",duration=60,concurrency=32,allow_errors=True)
    run("large","tier-sustained-nprobe8",duration=600,concurrency=32,allow_errors=True)
    run("large","tier-sustained-nprobe4",duration=600,concurrency=32,nprobe=4,allow_errors=True)
    # Then the same node with the whole index cacheable on a 1000 MiB/s volume.
    configure("large",fresh=True,disk_cache=True,disk_cache_bytes=80*GiB,**big)
    run("large","tier-fastdisk-warm",duration=600,concurrency=32,allow_errors=True)
    run("large","tier-fastdisk-nprobe8",duration=600,concurrency=32,allow_errors=True)
    run("large","tier-fastdisk-nprobe4",duration=600,concurrency=32,nprobe=4,allow_errors=True)
    configure("large",**big)
    event("tier-complete")

def verify():
    # Hold the headline configurations for twenty minutes. The cached path
    # should be flat because it reads almost nothing from the object store;
    # the uncached path is the one that has to prove it is not riding burst
    # credit. The warm cache is deliberately not wiped here.
    big=dict(hedge_ms=0,cache=2*GiB,limit="20GiB",hard="26G")
    configure("large",disk_cache=True,disk_cache_bytes=80*GiB,**big)
    run("large","verify-fastdisk-nprobe4",duration=1200,concurrency=32,nprobe=4,allow_errors=True)
    run("large","verify-fastdisk-nprobe8",duration=1200,concurrency=32,nprobe=8,allow_errors=True)
    configure("large",**big)
    run("large","verify-direct-nprobe4",duration=1200,concurrency=32,nprobe=4,allow_errors=True)
    event("verify-complete")

def nvme():
    # Instance-store NVMe as the cache, on spot shapes, no provisioned volume.
    # Warm once, then hold each concurrency; cached rows were flat in the first
    # series, so five minutes is enough per point and ten for the headline.
    budgets={"nvme-c7gd":dict(cache=2*GiB,limit="5120MiB",hard="6144M"),
             "nvme-r7gd":dict(cache=2*GiB,limit="20GiB",hard="26G")}
    for node,grid in [("nvme-c7gd",[8,16]),("nvme-r7gd",[8,16,32])]:
        configure(node,fresh=True,disk_cache=True,disk_cache_bytes=100*GiB,hedge_ms=0,**budgets[node])
        run(node,"nvme-warm",duration=600,concurrency=8,nprobe=4,allow_errors=True)
        for c in grid:
            run(node,f"nvme-nprobe4-c{c}",duration=300,concurrency=c,nprobe=4,allow_errors=True)
        run(node,f"nvme-nprobe8-c{grid[-1]}",duration=300,concurrency=grid[-1],nprobe=8,allow_errors=True)
        run(node,f"nvme-sustained-nprobe4-c{grid[-1]}",duration=600,concurrency=grid[-1],nprobe=4,allow_errors=True)
    event("nvme-complete")

def metak():
    # The metadata cap, on a node reading straight from S3: how many requests
    # a search makes when the caller wants every passage, the top three, or
    # none, and what that does to the rate. The server restarts before each
    # setting so all three start from the same cold segment cache.
    node="nvme-c7gd"; binary=ROOT/"benchmark-metak"
    profile=dict(variant="metak",disk_cache=False,hedge_ms=0,cache=2*GiB,limit="5120MiB",hard="6144M")
    for cap,tag in [(0,"all"),(3,"top3"),(-1,"none")]:
        configure(node,**profile)
        run(node,f"metak-{tag}-quality",queries="tuning",requests=100,distribution="sequential",nprobe=4,metadata_k=cap,binary=binary,allow_errors=True)
        run(node,f"metak-{tag}-load",duration=300,concurrency=8,nprobe=4,metadata_k=cap,binary=binary,allow_errors=True)
    event("metak-complete")

def nvme2():
    # The 8 GiB NVMe box again, with the RAM given to the page cache instead of
    # the Go heap: the instance-store slice on an xlarge delivers ~300 MB/s, so
    # what matters is how much of the working set the kernel can keep in
    # memory. The warmed cache is kept (fresh=False).
    node="nvme-c7gd"
    configure(node,disk_cache=True,disk_cache_bytes=100*GiB,hedge_ms=0,cache=512*1024*1024,limit="2048MiB",hard="3072M")
    run(node,"nvme2-warm",duration=300,concurrency=8,nprobe=4,allow_errors=True)
    for c in [8,16]:
        run(node,f"nvme2-nprobe4-c{c}",duration=300,concurrency=c,nprobe=4,allow_errors=True)
    run(node,"nvme2-sustained-nprobe4-c16",duration=600,concurrency=16,nprobe=4,allow_errors=True)
    run(node,"nvme2-nprobe8-c16",duration=300,concurrency=16,nprobe=8,allow_errors=True)
    event("nvme2-complete")

def metak2():
    # The two capped settings again with a load generator that accepts hits
    # without metadata past the cap (the first client rejected them). Same
    # node, same direct-S3 profile, restart before each setting.
    node="nvme-c7gd"; binary=ROOT/"benchmark-metak2"
    profile=dict(variant="metak",disk_cache=False,hedge_ms=0,cache=2*GiB,limit="5120MiB",hard="6144M")
    for cap,tag in [(3,"top3"),(-1,"none")]:
        configure(node,**profile)
        run(node,f"metak2-{tag}-quality",queries="tuning",requests=100,distribution="sequential",nprobe=4,metadata_k=cap,binary=binary,allow_errors=True)
        run(node,f"metak2-{tag}-load",duration=300,concurrency=8,nprobe=4,metadata_k=cap,binary=binary,allow_errors=True)
    event("metak2-complete")

def main():
    parser=argparse.ArgumentParser();parser.add_argument("phase",choices=["pilot","formal","diagnostics","optimize","throughput","storage","tier","verify","nvme","metak","nvme2","metak2"])
    args=parser.parse_args()
    try:
        if args.phase=="pilot":pilot()
        if args.phase=="formal":formal()
        if args.phase=="diagnostics":diagnostics()
        if args.phase=="optimize":optimize()
        if args.phase=="throughput":throughput()
        if args.phase=="storage":storage()
        if args.phase=="tier":tier()
        if args.phase=="verify":verify()
        if args.phase=="nvme":nvme()
        if args.phase=="metak":metak()
        if args.phase=="nvme2":nvme2()
        if args.phase=="metak2":metak2()
    except Exception as e:
        event("suite-error",error=str(e));raise

if __name__=="__main__":main()
