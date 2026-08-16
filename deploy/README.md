# Deploying

Two hosts, because the write path and the read path want opposite machines:

| | build host | serving host |
|---|---|---|
| job | embed 12.5M passages, build the index | answer queries |
| lives for | a few hours | indefinitely |
| size | 48 vCPU (`c7g.12xlarge`, ~$1.74/hr) | 2 vCPU / 2 GB (`t4g.small`, ~$12/mo) |
| holds the corpus | yes, while building | **no** |

They meet at an object store and share nothing else. The build host is
terminated when it is done; that is the point of paying for a big one.

## 1. Bucket

```sh
BUCKET=your-bucket-name
aws s3 mb "s3://$BUCKET" --region us-east-1
aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

The bucket stays private. Both hosts reach it with an instance profile, never
with keys on disk.

## 2. Build host

Launch a many-core arm64 box with the instance profile and ~200 GB of gp3.
Embedding is CPU-bound and scales with cores; nothing else about this host
matters, and it is deleted afterwards.

```sh
sudo dnf install -y python3.11 python3.11-pip docker git && sudo systemctl start docker
git clone https://github.com/Polign/polign_demo && cd polign_demo
python3.11 -m venv ~/venv
~/venv/bin/pip install -q onnxruntime transformers optimum[onnxruntime] numpy

# ~20 GB of parquet in, ~4 GB of JSONL out. Resumable: rerun after any
# interruption and it picks up at the next shard.
mkdir -p ~/data ~/work
sudo docker run --rm -v ~/data:/out -v ~/work:/work -v "$PWD/index:/idx" \
    python:3.12-slim sh -c 'pip install -q pyarrow && python /idx/prepare.py --out /out --work /work'

~/venv/bin/python index/export_model.py ~/bge-small

STORE=s3://$BUCKET/polign COLLECTION=wikipedia MODEL=~/bge-small ./index/build-index.sh
```

Then **terminate the instance**. Everything durable is in the bucket. Keep the
model directory and `data/corpus.json` — the serving host needs them.

### What build-index.sh is doing

Embedding a shard takes minutes; importing the result takes seconds. Run them in
series and the expensive machine idles through every import. The script runs two
lanes instead: an embed lane writing `NNN.jsonl` then an `NNN.ready` fence, and a
writer lane that only opens fenced files. The whole write cost disappears under
the following embed.

The writer lane is internally serial, and that is a correctness constraint rather
than a missed optimisation: a collection has a single index writer, so two
concurrent `polign-import` processes over one collection race the manifest. The
lane is idle ~95% of each cycle anyway, so serialising it costs nothing.

Compaction runs once, at the end. A query reads every segment in the cells it
probes, so the index must end compacted — but each pass rewrites the whole
collection, so compacting every K shards moves several times the corpus for no
benefit while nothing is querying it.

### Sizing

Measured, int8, length-bucketed batches:

| | |
|---|---|
| embedding throughput | ~11 passages/s per vCPU (memory-bandwidth bound above ~8 cores) |
| full corpus (12.5M) | ~7 h on 48 vCPU, ~$12 |
| import | ~5,000 vectors/s |
| final compaction | ~1.5 h |

Two things that did **not** help, so nobody repeats them: x86 with AVX-512 VNNI
measured no faster per core than Graviton at higher cost, and N single-threaded
ONNX sessions matched one N-threaded session almost exactly. What did help, 1.6x:
sorting each window by length before batching — passages run p50≈105 tokens
against a 256 cap, so a naive batch spends most of its FLOPs on padding.

## 3. Serving host

Launch a `t4g.small` (arm64, 20 GB gp3 — the disk cache is budgeted 6 GB) with
the same instance profile.

```sh
sudo useradd --system --home /var/lib/polign polign
sudo mkdir -p /opt/polign/bin /opt/polign/data /var/lib/polign/disk-cache /var/log/caddy
sudo chown -R polign:polign /var/lib/polign /opt/polign

# From polign_db, built with the "cloud" tag for s3:// support:
#   CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -tags cloud -o polign-server ./cmd/server
# From this repo:
#   CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o polign-demo ./cmd/demo
sudo install -m 0755 polign-server polign-demo /opt/polign/bin/
sudo install -m 0644 serve/embedserve.py /opt/polign/bin/
sudo cp -r bge-small data/corpus.json data/examples.txt /opt/polign/data/

sudo python3.11 -m venv /opt/polign/venv
sudo /opt/polign/venv/bin/pip install -q onnxruntime transformers numpy
sudo chown -R polign:polign /opt/polign

sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polign-node polign-embedserve polign-demo

sudo cp deploy/Caddyfile /etc/caddy.Caddyfile
sudo systemctl restart caddy
```

Point the DNS A record at the host — **DNS-only** (grey cloud on Cloudflare), or
Caddy's HTTP-01 challenge cannot reach it.

### Memory budget

Three processes share 2 GB:

| | |
|---|---|
| node, idle | 27 MiB |
| node, burst of distinct cold queries | ~290 MiB peak |
| embedding sidecar | ~380 MiB resident |
| demo app | ~30 MiB (it loads no model of its own) |

The node's RSS does not grow with the corpus — that is the entire point of the
cold path. What grows is the bytes one query fetches, which is why the
collection is built with PQ codes and why `-nprobe` is pinned rather than left
at the node's `nlist/4` default.

## Checks

```sh
curl -s localhost:23100/healthz
curl -s localhost:23200/healthz                       # embedding sidecar
curl -s "localhost:23100/demo/search?q=why+do+volcanoes+erupt" | head -40
systemctl status polign-node polign-embedserve polign-demo
ps -o rss= -C polign-server                           # expect well under GOMEMLIMIT
```

### Prewarm after a deploy

The first query touching a region pays an object-store round trip per probed
cell; everything after is served from the local disk cache. Run the curated
examples once so the front page is warm:

```sh
python3 - <<'EOF'
import json, urllib.parse, urllib.request
qs = json.load(urllib.request.urlopen("http://localhost:23100/demo/meta"))["examples"]
for q in qs:
    u = f"http://localhost:23100/demo/search?q={urllib.parse.quote(q)}"
    print(json.load(urllib.request.urlopen(u))["took_ms"], "ms", q)
EOF
```

## Gotchas

- **`-restore-stores ""` is load-bearing.** Drop it and the serving node tries
  to rebuild every vector in RAM at boot, and dies on a small box.
- **Do not enable the hot tier here.** Promotion transiently materialises the
  full index, which is exactly what this host cannot afford.
- **Query knobs are not public inputs.** `-public` fixes `nprobe` at the
  operator's value; without it a browser could ask for the most expensive query
  the node can run.
- **A cold searcher can outrun its segments.** Right after a load the node may
  hold a manifest whose cells have since been compacted away, and queries fail
  with `object not found` until the next refresh. It is self-healing; wait one
  `-segment-refresh` interval before concluding anything is broken.
- **`pkill -f <pattern>` over SSH kills your own session** when the pattern
  appears anywhere in the remote command line — including the real filename
  elsewhere in the same command. Put start/stop logic in a script on the host.
- **The app stops with the node.** `polign-demo.service` is `BindsTo` *and*
  `PartOf` the node unit, so restarting the node cycles the app with it. With
  `BindsTo` alone, a node restart stops the app and leaves it stopped — which is
  exactly how this demo once went dark after a routine binary upgrade.
