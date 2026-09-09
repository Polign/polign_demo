#!/usr/bin/env python3
"""Collect cloud counters from the operator machine; never put AWS keys on X."""
import datetime
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
state=json.loads((ROOT/"artifacts/infrastructure.json").read_text())
metrics=["CPUUtilization","CPUCreditUsage","CPUCreditBalance","CPUSurplusCreditBalance","CPUSurplusCreditsCharged","EBSIOBalance%","EBSByteBalance%","NetworkIn","NetworkOut","EBSReadBytes","EBSWriteBytes"]
queries=[]
for node,info in state["instances"].items():
    for n,metric in enumerate(metrics):
        stat="Sum" if metric in ["CPUCreditUsage","CPUSurplusCreditsCharged","NetworkIn","NetworkOut","EBSReadBytes","EBSWriteBytes"] else "Average"
        queries.append({"Id":node+str(n),"Label":node+":"+metric,"MetricStat":{"Metric":{"Namespace":"AWS/EC2","MetricName":metric,"Dimensions":[{"Name":"InstanceId","Value":info["id"]}]},"Period":300,"Stat":stat},"ReturnData":True})
now=datetime.datetime.now(datetime.timezone.utc)
cmd=["aws","--profile",state["profile"],"--region",state["region"],"cloudwatch","get-metric-data","--metric-data-queries",json.dumps(queries),"--start-time","2026-09-06T05:30:00Z","--end-time",now.isoformat(),"--scan-by","TimestampAscending","--output","json"]
data=json.loads(subprocess.check_output(cmd))
(ROOT/"artifacts/cloud-metrics.json").write_text(json.dumps(data,indent=2))
print("Saved",sum(len(x.get("Values",[])) for x in data["MetricDataResults"]),"cloud metric observations")
