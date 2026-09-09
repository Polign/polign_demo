#!/usr/bin/env python3
"""Archive the benchmark artifacts locally before temporary instance teardown."""
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
KEY=ROOT/"artifacts/benchmark-key"
SSH=["ssh","-i",str(KEY),"-o","BatchMode=yes"]
DRIVER="ec2-user@52.53.23.129"  # driver2; the first series' driver is terminated and its artifacts are already local
ROOT.joinpath("runs").mkdir(exist_ok=True)
subprocess.run(["rsync","-az","--quiet","-e","ssh -i "+str(KEY),DRIVER+":/opt/wikibench/runs/",str(ROOT/"runs")+"/"],check=True)
# The second driver keeps its own event log; the first series' events.jsonl stays as archived.
result=subprocess.run(SSH+[DRIVER,"cat /opt/wikibench/events.jsonl"],capture_output=True)
if result.returncode==0:(ROOT/"artifacts"/"events-series2.jsonl").write_bytes(result.stdout)
for role,ip in [("nvme-c7gd","18.145.230.105"),("nvme-r7gd","54.215.27.246"),("driver2","52.53.23.129")]:
    subprocess.run(["rsync","-az","--quiet","-e","ssh -i "+str(KEY),"ec2-user@"+ip+":/opt/wikibench/resources.jsonl",str(ROOT/"artifacts"/(role+"-resources.jsonl"))],check=True)
    result=subprocess.run(SSH+["ec2-user@"+ip,"sudo journalctl -u wikibench --no-pager -o short-iso"],capture_output=True,check=True)
    (ROOT/"artifacts"/(role+"-server.log")).write_bytes(result.stdout)
subprocess.run(["rsync","-az","--quiet","-e","ssh -i "+str(KEY),DRIVER+":/opt/wikibench/fixtures/",str(ROOT/"fixtures")+"/"],check=True)
print("Archived",len(list((ROOT/"runs").glob("*/summary.json"))),"completed runs and resource timelines.")
