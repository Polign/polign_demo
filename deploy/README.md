# Deploying the demo

Two hosts, because they have opposite needs:

| | build host | serving host |
|---|---|---|
| job | embed + load 12.5M passages, publish the IVF-PQ generation | answer queries |
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
| load rate | 4,658 passages/s (12 workers, batches of 512) (256-dim model; the shipped 512-dim model runs slower) |
| store size | 330 MB for 60k → **~60 GB projected for 12.5M at 512 dims** (WAL, segments, cells, BM25) |
| serving node RSS, idle | **15 MiB** |
| serving node RSS, under queries | ~278 MiB (transient per-query allocations; cap with `GOMEMLIMIT`) |
| cold query, `nprobe=16` | 14 ms |
| cold query, `nprobe=32` | 22 ms |

RSS does not grow with the corpus — that is the entire point of the cold path.
What *does* grow is the bytes one query fetches: cells hold ~√N vectors each,
so a cell at 12.5M is ~14× a cell at 60k. That is why `-nprobe` is pinned
explicitly in `polign-demo.service` instead of using the node's `nlist/4`
default, which at 12.5M would probe roughly a quarter of the corpus per query.

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

# Load in parallel lanes: each lane is its own node plus its own slice of the
# shards, restarting per shard. Do not substitute one long `cmd/load` run at
# this scale — see "Why the load is shaped this way" below.
python3 ./deploy/split-dataset.py 3        # data-p0, data-p1, data-p2
for lane in 0 1 2; do
    persist=false; [ $lane = 0 ] && persist=true    # only lane 0 persists
    STORE="s3://$BUCKET/polign" setsid nohup \
        ./deploy/load-lane.sh $lane $((23000 + lane*10)) $((23001 + lane*10)) $persist \
        > ~/logs/lane$lane.log 2>&1 &
done
# ~4 h for 12.5M at 875 passages/s on 8 vCPU. Watch:
#   tail -f ~/logs/lane*.log

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
`data/corpus.json` are needed afterwards (~130 MB) — copy them to the serving
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

## Why the load is shaped this way

A node applies every write to its in-memory HNSW index as well as appending it
to the durable write log. For a bulk import that is pure overhead — the serving
node reads segments from object storage and never touches this index — and it
costs on two axes:

- **Memory.** One process holding 12.5M passages needs ~25 GB.
- **Time.** HNSW insertion slows as the graph grows: ~1,700/s into an empty
  index, ~400/s at a million vectors. Insert is single-writer per node, so one
  node cannot use more than a core or so of the machine for it.

Two knobs follow from that, and `load-lane.sh` uses both:

1. **Restart per shard** (`cmd/load -shards 1`, node restarted with
   `-restore-stores ""`). The in-memory index starts empty every time, so
   insertion stays at its fast rate. The write log is the source of truth, so
   nothing is lost; the loader's checkpoint resumes exactly where it stopped.
2. **Several lanes.** Since the limit is per node, run N nodes against the same
   bucket, each loading a disjoint slice of shards (`split-dataset.py`). They
   all append to the shared write log, which is built for a fleet; only lane 0
   runs the persistor (`-persist=true`), the rest are pure writers.

Measured on `c7g.2xlarge` (8 vCPU) with the 512-dim model: one lane ~390/s,
three lanes **~875/s**. Three lanes put load average at ~14 on 8 cores, so a
fourth would only add contention — the right lane count is roughly
`vCPU / 3`. Verify concurrency is healthy before trusting a long run:

```sh
grep -icE "error|conflict|failed" ~/logs/node-p*.log   # expect 0 everywhere
grep -c "wrote segment" ~/logs/node-p0.log             # only lane 0 persists
```

## Gotchas

- **A cold searcher can outrun its segments.** Right after a load, the node may
  hold a manifest whose cells have since been compacted away, and queries fail
  with `object not found` until the next refresh (`-segment-refresh`). It is
  self-healing; wait one refresh interval before concluding anything is broken.
- **`-restore-stores ""` is load-bearing.** Drop it and the serving node tries
  to rebuild all 12.5M vectors in RAM at boot, and dies on a 2 GB box.
- **Do not enable the hot tier here.** Promotion transiently materializes the
  full index, which is exactly what this host cannot afford.
- **Query knobs are not public inputs.** `-public` fixes `nprobe` at the
  operator's value; without it, a browser could ask for the most expensive
  query the node can run.
