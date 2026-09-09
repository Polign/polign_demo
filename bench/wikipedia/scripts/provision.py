#!/usr/bin/env python3
"""Create only the tagged, temporary Wikipedia benchmark infrastructure."""
import argparse
import json
import pathlib
import subprocess
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE = ROOT / "artifacts" / "infrastructure.json"
PROFILE, REGION = "default", "us-west-1"
NAME = "polign-wiki-read-20260906"

def aws(service, *args):
    cmd = ["aws", "--profile", PROFILE, "--region", REGION, service, *args, "--output", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    if not args.create:
        raise SystemExit("Specify --create to provision the reviewed benchmark infrastructure")
    ROOT.joinpath("artifacts").mkdir(exist_ok=True)
    key = ROOT / "artifacts" / "benchmark-key"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    pub = key.with_suffix(".pub").read_text().strip()
    if STATE.exists():
        state = json.loads(STATE.read_text())
        sg = state["security_group"]
    else:
        aws("ec2", "import-key-pair", "--key-name", NAME, "--public-key-material", "fileb://" + str(key.with_suffix(".pub")))
        trust = {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
        policy = {"Version":"2012-10-17","Statement":[
            {"Effect":"Allow","Action":["s3:GetObject","s3:GetObjectVersion"],"Resource":["arn:aws:s3:::polign-demo-wiki-en-uw1/polign-v4/wikipedia_bge/*","arn:aws:s3:::polign-demo-wiki-en-uw1/polign-v4/.encryption"]},
            {"Effect":"Allow","Action":"s3:ListBucket","Resource":"arn:aws:s3:::polign-demo-wiki-en-uw1","Condition":{"StringLike":{"s3:prefix":["polign-v4/wikipedia_bge/*"]}}},
            {"Effect":"Allow","Action":"s3:GetBucketLocation","Resource":"arn:aws:s3:::polign-demo-wiki-en-uw1"},
            {"Effect":"Deny","Action":["s3:Put*","s3:Delete*","s3:AbortMultipartUpload","s3:RestoreObject","s3:Create*"],"Resource":"*"}
        ]}
        for label, doc in [("trust",trust),("readonly-policy",policy)]:
            (ROOT / "artifacts" / f"{label}.json").write_text(json.dumps(doc, indent=2))
        aws("iam", "create-role", "--role-name", NAME, "--assume-role-policy-document", json.dumps(trust))
        aws("iam", "put-role-policy", "--role-name", NAME, "--policy-name", "WikipediaReadOnly", "--policy-document", json.dumps(policy))
        aws("iam", "create-instance-profile", "--instance-profile-name", NAME)
        aws("iam", "add-role-to-instance-profile", "--instance-profile-name", NAME, "--role-name", NAME)
        sg = aws("ec2", "create-security-group", "--group-name", NAME, "--description", "Temporary isolated Wikipedia read benchmark", "--vpc-id", "vpc-0cbe2569")["GroupId"]
        ip = urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip()
        permissions = [
            {"IpProtocol":"tcp","FromPort":22,"ToPort":22,"IpRanges":[{"CidrIp":ip+"/32"}]},
            {"IpProtocol":"tcp","FromPort":22,"ToPort":22,"UserIdGroupPairs":[{"GroupId":sg}]},
            {"IpProtocol":"tcp","FromPort":23000,"ToPort":23000,"UserIdGroupPairs":[{"GroupId":sg}]}
        ]
        aws("ec2", "authorize-security-group-ingress", "--group-id", sg, "--ip-permissions", json.dumps(permissions))
        state = {"name":NAME,"profile":PROFILE,"region":REGION,"security_group":sg,"role":NAME,"key_name":NAME,"instances":{},"max_instance_hours":24,"estimated_spend_cap_usd":100}
        STATE.write_text(json.dumps(state, indent=2))
    # size, iops and throughput are per role: the large node measures a higher
    # network tier, and its volume is fast enough to hold a real disk cache.
    # The second series (2026-09-06, after the first four were terminated) puts
    # the disk cache on instance-store NVMe instead of a provisioned EBS volume,
    # and runs everything on spot: the nodes hold nothing durable.
    roles = [("driver","c7g.2xlarge",40,3000,125,False),("small","t4g.small",20,3000,125,False),("medium","c7g.xlarge",20,3000,125,False),("large","c7g.4xlarge",120,16000,1000,True),
             ("driver2","c7g.xlarge",40,3000,125,True),("nvme-c7gd","c7gd.xlarge",20,3000,125,True),("nvme-r7gd","r7gd.xlarge",20,3000,125,True)]
    for role, instance_type, size, iops, throughput, spot in roles:
        if role in state["instances"]:
            continue
        userdata = """#!/bin/bash
set -eu
systemctl disable --now polign-demo polign-embedserve polign-node caddy || true
dnf install -y python3.11 python3.11-pip sysstat tar gzip
mkdir -p /opt/wikibench /var/lib/polign/benchmark-cache
chown -R ec2-user:ec2-user /opt/wikibench
systemd-run --unit=wikibench-deadline --on-active=24h /usr/sbin/shutdown -h now
"""
        if role.startswith("nvme-"):
            # The instance store is the cache. Pick it by model so the root EBS
            # volume is never formatted by mistake.
            userdata += """DEV=$(lsblk -dn -o NAME,MODEL | awk '/Instance Storage/{print "/dev/"$1; exit}')
test -n "$DEV"
mkfs.xfs -f "$DEV"
mount -o noatime "$DEV" /var/lib/polign/benchmark-cache
chown -R ec2-user:ec2-user /var/lib/polign
"""
        ud = ROOT / "artifacts" / f"userdata-{role}.sh"
        ud.write_text(userdata)
        tags = [{"ResourceType":"instance","Tags":[{"Key":"Name","Value":NAME+"-"+role},{"Key":"Benchmark","Value":NAME},{"Key":"ExpiresAfterHours","Value":"24"}]}]
        command = ["run-instances","--client-token",NAME+"-"+role,"--image-id","ami-00f8cb4082cb07bf9","--instance-type",instance_type,"--key-name",NAME,
                   "--subnet-id","subnet-e42e6581","--security-group-ids",sg,"--associate-public-ip-address",
                   "--metadata-options","HttpTokens=required,HttpEndpoint=enabled","--instance-initiated-shutdown-behavior","terminate",
                   "--block-device-mappings",json.dumps([{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":size,"VolumeType":"gp3","Iops":iops,"Throughput":throughput,"DeleteOnTermination":True}}]),
                   "--tag-specifications",json.dumps(tags),"--user-data","file://"+str(ud)]
        if not role.startswith("driver"):
            command += ["--iam-instance-profile", "Name="+NAME]
        if spot:
            command += ["--instance-market-options",json.dumps({"MarketType":"spot","SpotOptions":{"SpotInstanceType":"one-time","InstanceInterruptionBehavior":"terminate"}})]
        if instance_type.startswith("t4g"):
            command += ["--credit-specification","CpuCredits=unlimited"]
        result = aws("ec2", *command)["Instances"][0]
        state["instances"][role] = {"id":result["InstanceId"],"type":instance_type,"private_ip":result["PrivateIpAddress"]}
        STATE.write_text(json.dumps(state,indent=2))
        print(role, state["instances"][role], flush=True)

if __name__ == "__main__":
    main()
