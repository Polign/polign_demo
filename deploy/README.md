# Deploying the demo

Two hosts, because they have opposite needs:

| | build host | serving host |
|---|---|---|
| job | embed + load 6M passages, publish the IVF-PQ generation | answer queries |
| lives for | a few hours | indefinitely |
| size | 8 vCPU / 16 GB (`c7g.2xlarge`, ~$0.29/hr) | 2 vCPU / 2 GB (`t4g.small`, ~$12/mo) |
| holds the corpus | yes, while building | **no** |

They meet at an S3 bucket. Nothing else is shared: the serving host never sees
the dataset files, and the build host is terminated when it is done.

## Measurements

Production, `t4g.small` serving node against S3, 12.5M-passage corpus,
`nprobe=24`:

| | |
|---|---|
| node RSS, idle | **28 MiB** |
| node RSS, after queries | ~420 MiB (bounded by `GOMEMLIMIT=1400MiB`) |
| first query for a topic (cold from S3) | ~950 ms |
| repeat query (local disk cache) | 40–65 ms |
| corpus prepare (41 parquet files) | ~50 min on `c7g.2xlarge` |
| load rate into S3-backed node | ~1,000 passages/s |

The gap between 950 ms and 45 ms is the whole cold-storage story: the first
query for a region of the corpus pays an S3 round trip for each cell it probes,
and everything after that is served from the local disk cache. Prewarming the
example queries after a deploy (see below) means the demo's front page is warm
before anyone touches it.

From a 60,000-passage slice on an M-series Mac, `fs:` store — useful because it
isolates the engine from S3 latency:

| | |
|---|---|
| load rate | 4,658 passages/s (12 workers, batches of 512) → **~21 min for 6M** |
| store size | 330 MB for 60k → **~33 GB projected for 6M** (WAL, segments, cells, BM25) |
| serving node RSS, idle | **15 MiB** |
| serving node RSS, under queries | ~278 MiB (transient per-query allocations; cap with `GOMEMLIMIT`) |
| cold query, `nprobe=16` | 14 ms |
| cold query, `nprobe=32` | 22 ms |

RSS does not grow with the corpus — that is the entire point of the cold path.
What *does* grow is the bytes one query fetches: cells hold ~√N vectors each,
so a cell at 6M is ~10× a cell at 60k. That is why `-nprobe` is pinned
explicitly in `polign-demo.service` instead of using the node's `nlist/4`
default, which at 6M would probe roughly a quarter of the corpus per query.

## 1. Bucket

```sh
BUCKET=polign-demo-wiki-en           # must be globally unique
aws s3 mb "s3://$BUCKET" --region us-east-1
aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

The bucket stays private. Both hosts reach it with an instance profile, never
with keys on disk.

## 2. Build host

Launch `c7g.2xlarge` (arm64, Amazon Linux 2023) with the instance profile and
at least a 120 GB gp3 volume — `prepare.py` needs room for parquet downloads
plus ~4 GB of JSONL.

```sh
sudo dnf install -y docker git golang && sudo systemctl start docker
git clone https://github.com/Polign/polign_demo && cd polign_demo

# ~20 GB of parquet in, ~4 GB of JSONL out. Resumable: rerun after any
# interruption and it picks up at the next shard.
mkdir -p data work
sudo docker run --rm -v "$PWD/data:/out" -v "$PWD/work:/work" -v "$PWD/prepare:/prep" \
    python:3.12-slim sh -c 'pip install -q pyarrow && python /prep/prepare.py --out /out --work /work'

# Load one shard at a time, restarting the node between shards. Do not
# substitute a single long `cmd/load` run at this scale: a node applies every
# write to its in-memory index as well as its write log, so one process would
# need ~25 GB of RAM, and HNSW insertion decays from ~1,700/s into an empty
# index to ~400/s once it holds a million vectors. The driver's restarts keep
# it at the fast end — see the comment at the top of the script.
STORE="s3://$BUCKET/polign" ./deploy/load-chunked.sh    # ~2.5 h for 12.5M

# Publish the IVF-PQ collection generation and its CodesOnly structure
# generation. This is the expensive, RAM-hungry step the serving host must
# never run.
polign-maintain -store "s3://$BUCKET/polign"
```

For a small slice (a smoke test, not the real corpus) a single run is fine:

```sh
polign-server -store "s3://$BUCKET/polign" -maintain 0 &
go run ./cmd/load -dir ./data -addr http://127.0.0.1:23000
```

Then **terminate the instance**. Everything durable is in S3.

Only `data/model.json`, `data/vocab.txt`, `data/embeddings.f32` and
`data/corpus.json` are needed afterwards (~30 MB) — copy them to the serving
host, or keep a copy in the bucket.

## 3. Serving host

Launch `t4g.small` (arm64, Amazon Linux 2023, 20 GB gp3 — the disk cache is
budgeted 6 GB) with the same instance profile.

```sh
sudo useradd --system --home /var/lib/polign polign
sudo mkdir -p /opt/polign/bin /opt/polign/data /var/lib/polign/disk-cache
sudo chown -R polign:polign /var/lib/polign /opt/polign

# Cross-compiled locally: CGO_ENABLED=0 GOOS=linux GOARCH=arm64
#   go build -o polign-demo ./cmd/demo          (this repo)
#   go build -tags cloud -o polign-server ./cmd/server   (polign_db)
# The "cloud" build tag is required for s3:// support.
scp polign-server polign-demo   ec2-user@HOST:/tmp/
scp data/{model.json,vocab.txt,embeddings.f32,corpus.json} ec2-user@HOST:/tmp/data/

sudo install -m 0755 /tmp/polign-{server,demo} /opt/polign/bin/
sudo cp /tmp/data/* /opt/polign/data/

sudo cp deploy/polign-node.service deploy/polign-demo.service /etc/systemd/system/
sudo sed -i "s/REPLACE_BUCKET/$BUCKET/" /etc/systemd/system/polign-node.service
sudo systemctl daemon-reload
sudo systemctl enable --now polign-node polign-demo
```

Caddy in front:

```sh
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

Point the DNS A record at the host — **DNS-only / grey cloud** on Cloudflare,
or Caddy's HTTP-01 challenge fails.

## Checks

```sh
curl -s localhost:23100/healthz
curl -s "localhost:23100/demo/search?q=why+do+volcanoes+erupt&mode=hybrid" | head -40
systemctl status polign-node polign-demo
ps -o rss= -C polign-server   # expect well under GOMEMLIMIT
```

## Prewarm after a deploy

The first query touching a region of the corpus pays an S3 round trip per
probed cell (~950 ms); everything after is served from the local disk cache
(~45 ms). Run the curated examples once so the front page is warm:

```sh
python3 - <<'EOF'
import json, urllib.parse, urllib.request
qs = json.load(urllib.request.urlopen("http://localhost:23100/demo/meta"))["examples"]
for q in qs:
    for mode in ("semantic", "keyword", "hybrid"):
        u = f"http://localhost:23100/demo/search?q={urllib.parse.quote(q)}&mode={mode}"
        print(mode, q, json.load(urllib.request.urlopen(u))["took_ms"], "ms")
EOF
```

## Gotchas

- **A cold searcher can outrun its segments.** Right after a load, the node may
  hold a manifest whose cells have since been compacted away, and queries fail
  with `object not found` until the next refresh (`-segment-refresh`). It is
  self-healing; wait one refresh interval before concluding anything is broken.
- **`-restore-stores ""` is load-bearing.** Drop it and the serving node tries
  to rebuild all 6M vectors in RAM at boot, and dies on a 2 GB box.
- **Do not enable the hot tier here.** Promotion transiently materializes the
  full index, which is exactly what this host cannot afford.
- **Query knobs are not public inputs.** `-public` fixes `nprobe` at the
  operator's value; without it, a browser could ask for the most expensive
  query the node can run.
