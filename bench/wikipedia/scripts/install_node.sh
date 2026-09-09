#!/bin/bash
set -eu
systemctl disable --now polign-demo polign-embedserve polign-node caddy || true
BENCH_BIND_IP=$(hostname -I | awk '{print $1}')
chmod 0755 /opt/wikibench/polign-server-*
cat >/etc/systemd/system/wikibench.service <<EOF
[Unit]
Description=Isolated Wikipedia read benchmark - Polign 0.5.0
After=network-online.target
[Service]
User=ec2-user
Environment=AWS_REGION=us-west-1
Environment=GOMEMLIMIT=1000MiB
Environment=POLIGN_BENCH_TELEMETRY=1
ExecStart=/opt/wikibench/polign-server-instrumented -segment-stores s3://polign-demo-wiki-en-uw1/polign-v4 -cold-first=true -restore-stores "" -log-stores "" -persist=false -maintain 0 -hot-max 0 -tail-fresh=false -split-qps 0 -placement-refresh 0 -http $BENCH_BIND_IP:23000 -grpc 127.0.0.1:23001 -admin 127.0.0.1:23002 -disk-cache-dir /var/lib/polign/benchmark-cache -disk-cache-bytes 6442450944 -segment-cache-bytes 268435456 -segment-refresh 5m -hedge-reads 150ms
MemoryMax=1536M
MemorySwapMax=0
Restart=no
LimitNOFILE=65536
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
EOF
chown -R ec2-user:ec2-user /var/lib/polign/benchmark-cache
systemctl daemon-reload
systemctl start wikibench
systemd-run --unit=wikibench-collector /usr/bin/python3.11 /opt/wikibench/collect.py --out /opt/wikibench/resources.jsonl
systemctl show wikibench -p MainPID -p ActiveState -p MemoryMax
