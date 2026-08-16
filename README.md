# polign_demo

A worked example of [polign_db](https://github.com/Polign/polign): semantic,
keyword, and hybrid search over **all of English Wikipedia — 12.5M passages —
served from a 2 GB box**.

It is small on purpose. The corpus lives in object storage, the serving node
keeps a manifest and a centroid table resident and nothing else, and every query
is a cold read. Read it to see what a polign_db deployment actually looks like:
how an index gets built, how a query gets served, and which knobs matter.

Live at **[demo.polign.com](https://demo.polign.com)**.

This repo depends on **no polign_db Go packages**. It talks to the database over
the documented HTTP `/v1` API, exactly as any other service would — so
[`internal/polign/client.go`](internal/polign/client.go) (~200 lines, standard
library only) is the shortest complete description of the wire protocol.

## The two paths

Everything here is one of two things: building the index, or serving a query.

```
WRITE PATH  (a build host, for a few hours, then thrown away)

  index/prepare.py      Wikipedia dump ──> passage shards (id, title, url, text)
  index/embed.py        shards ──────────> JSONL with a vector per passage
  polign-import         JSONL ───────────> segments in object storage
  index/build-index.sh  runs the three as two concurrent lanes

                                  │
                                  ▼
                        ┌───────────────────┐
                        │   object storage  │   segments, manifest, centroids,
                        │   (S3 / fs / GCS) │   BM25 index, PQ codes
                        └─────────┬─────────┘
                                  │  ranged reads, per query
                                  ▼
READ PATH  (one small box, indefinitely)

  serve/embedserve.py   query text ──> vector          (the same model as above)
  cmd/demo              vector + text ──> polign_db node ──> results
  polign-server         cold-first node: no corpus in RAM
```

The write path runs once on a big machine. The read path runs forever on a small
one. Keeping them apart is the whole design: the expensive part is temporary.

## What is in here

| | |
|---|---|
| [`index/`](index/) | **write path** — prepare the corpus, embed it, build the index |
| [`serve/`](serve/) | **read path** — the query-embedding sidecar |
| [`cmd/demo/`](cmd/demo/) | **read path** — the search UI and `/demo/search` |
| [`internal/polign/`](internal/polign/) | the polign_db HTTP client — start here |
| [`internal/embedder/`](internal/embedder/) | client for the embedding sidecar |
| [`internal/corpus/`](internal/corpus/) | the dataset manifest the UI reports |
| [`deploy/`](deploy/) | systemd units, Caddyfile, and the runbook |
| [`eval/`](eval/) | a retrieval-quality harness and the numbers it produced |

## Try it locally

Needs a `polign-server` and `polign-import` binary from polign_db, Python 3.11+,
and Docker (only for `pyarrow` during corpus prep).

```sh
# 1. A small slice of Wikipedia — two parquet files, ~250k passages.
mkdir -p data work
docker run --rm -v "$PWD/data:/out" -v "$PWD/work:/work" -v "$PWD/index:/idx" \
    python:3.12-slim sh -c 'pip install -q pyarrow && python /idx/prepare.py --out /out --work /work --files 2'

# 2. Export the embedding model, then embed the passages.
python3 -m venv venv && ./venv/bin/pip install -q onnxruntime transformers optimum[onnxruntime] numpy
./venv/bin/python index/export_model.py bge-small
./venv/bin/python index/embed.py --model bge-small --shard data/passages-00000.jsonl --out shard.jsonl

# 3. Build the index. No running node is involved — this writes segments directly.
polign-import -stores fs:/tmp/polign-store -collection wikipedia \
    -metric l2 -dim 384 -pqm 96 -compact shard.jsonl

# 4. Serve. The node holds no corpus; the sidecar holds the model.
polign-server -store fs:/tmp/polign-store -restore-stores "" -hot-max 0 \
    -persist=false -maintain 0 -http 127.0.0.1:23000 &
./venv/bin/python serve/embedserve.py --model bge-small --port 23200 &
go run ./cmd/demo -dir ./data -node http://127.0.0.1:23000 \
    -embed-addr http://127.0.0.1:23200 -collection wikipedia -http :24000
# open http://localhost:24000
```

## Three things worth understanding

**The embedding model is the contract.** A collection can only be searched by
the model that wrote it — a vector from any other model is a point in a
different space, and its nearest neighbours are meaningless. `index/embed.py`
and `serve/embedserve.py` deliberately share one model directory, and the app
refuses to start if the sidecar is unreachable rather than serving nonsense.

The model is also the dominant factor in result quality — far more than any
index tuning. See [eval/](eval/) for the measurements.

**The serving node never restores the corpus.** Left alone, `-store` expands
into a full read-write deployment that rebuilds the index in RAM at boot.
[`deploy/polign-node.service`](deploy/polign-node.service) disables restore, the
hot tier, persistence, maintenance, and the write-log follower, leaving only the
cold read path. Measured: **27 MiB resident at rest, ~290 MiB under a burst of
distinct cold queries.**

**Query cost is bytes fetched, not corpus size.** A cold query reads the cells
it probes. That is why the collection is built with PQ codes (`-pqm 96`): the
scan reads compressed codes and only the final candidates are re-ranked against
exact vectors, so a probe costs a fraction of what full-precision vectors would.
Distances returned are still exact — the compression only decides which records
get re-ranked.

## Honest numbers

Measured on the live deployment (12.5M passages, `t4g.small`, S3):

| | |
|---|---|
| node RSS, idle | 27 MiB |
| node RSS, 40 distinct cold queries, 4 concurrent | 290 MiB peak |
| warm query | 25–50 ms |
| cold query (first touch of a region) | 0.4–2 s |
| corpus build | ~7 h on 48 vCPU, then the host is terminated |

Cold search is not free: a query reads real bytes out of object storage, and
cells grow as √N, so the same `nprobe` costs more at 12.5M than at 60k. What
stays flat is memory. If you want single-digit milliseconds at this scale you
want the hot tier and a bigger box — that is a different deployment, and
polign_db supports it with different flags, not different code.

## Attribution

Text: [English Wikipedia](https://en.wikipedia.org) via the
[wikimedia/wikipedia](https://huggingface.co/datasets/wikimedia/wikipedia)
`20231101.en` dump, CC BY-SA 4.0. Embeddings:
[BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5), MIT.
