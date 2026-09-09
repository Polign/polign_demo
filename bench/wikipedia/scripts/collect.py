#!/usr/bin/env python3
"""One-second Linux telemetry, including the Polign loopback admin surface."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request

def read(path):
    try: return Path(path).read_text()
    except (OSError,ValueError): return ""

def numbers(path):
    result = {}
    for line in read(path).splitlines():
        fields = line.replace(":", "").split()
        if len(fields)>1:
            try: result[fields[0]] = int(fields[1])
            except ValueError: pass
    return result

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--out",required=True)
    p.add_argument("--service",default="wikibench")
    p.add_argument("--pid-file",default="")
    args=p.parse_args()
    Path(args.out).parent.mkdir(parents=True,exist_ok=True)
    stop=False
    def end(*_):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,end)
    pid=0
    with open(args.out,"a",buffering=1) as f:
        tick=0
        while not stop:
            start=time.monotonic()
            row={"timestamp":time.time(),"host":os.uname().nodename,"cpu_count":os.cpu_count(),"clock_ticks":os.sysconf("SC_CLK_TCK")}
            if args.pid_file:
                try: pid=int(read(args.pid_file).strip())
                except ValueError: pid=0
            elif tick%5==0:
                try: pid=int(subprocess.check_output(["systemctl","show",args.service,"-p","MainPID","--value"],text=True).strip())
                except (ValueError,subprocess.CalledProcessError): pid=0
            row["pid"]=pid
            row["memory_kib"]=numbers("/proc/meminfo")
            row["loadavg"]=read("/proc/loadavg").strip()
            row["cpu_ticks"]={parts[0]:[int(x) for x in parts[1:]] for line in read("/proc/stat").splitlines() if (parts:=line.split()) and parts[0].startswith("cpu")}
            row["network"]={parts[0]:[int(x) for x in parts[1:]] for line in read("/proc/net/dev").splitlines() if ":" in line for parts in [line.replace(":"," ").split()]}
            row["diskstats"]=[line.split() for line in read("/proc/diskstats").splitlines() if "loop" not in line]
            row["pressure"]={name:read("/proc/pressure/"+name).strip() for name in ["cpu","memory","io"]}
            if pid:
                row["process_status"]=numbers(f"/proc/{pid}/status")
                row["process_io"]=numbers(f"/proc/{pid}/io")
                row["smaps_rollup"]=numbers(f"/proc/{pid}/smaps_rollup")
                stat=read(f"/proc/{pid}/stat").split(") ")
                if len(stat)>1:
                    fields=stat[-1].split()
                    row["process_cpu_ticks"]={"user":int(fields[11]),"system":int(fields[12]),"threads":int(fields[17])}
                try: row["fd_count"]=len(os.listdir(f"/proc/{pid}/fd"))
                except OSError: pass
                cgroups=read(f"/proc/{pid}/cgroup").splitlines()
                if cgroups:
                    cg=Path("/sys/fs/cgroup")/cgroups[0].split(":",2)[-1].lstrip("/")
                    row["cgroup"]={name:read(cg/name).strip() for name in ["memory.current","memory.peak","memory.max","memory.events","memory.stat","cpu.stat","io.stat"]}
            fs=os.statvfs("/var/lib/polign/benchmark-cache" if Path("/var/lib/polign/benchmark-cache").exists() else "/opt/wikibench")
            row["disk_free_bytes"]=fs.f_bavail*fs.f_frsize
            if not args.pid_file and tick%5==0:
                row["admin"]={}
                for endpoint in ["overview","benchmark-metrics","collections"]:
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:23002/api/"+endpoint,timeout=1) as response:
                            row["admin"][endpoint]=json.load(response)
                    except Exception as e: row["admin"][endpoint]={"error":str(e)}
            row["collector_seconds"]=time.monotonic()-start
            f.write(json.dumps(row,separators=(",",":"))+"\n")
            tick+=1
            time.sleep(max(0,1-(time.monotonic()-start)))

if __name__=="__main__": main()
