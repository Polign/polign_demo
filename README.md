# polign_demo

A public search demo over **all of English Wikipedia**, built the way a
production deployment of [polign_db](https://github.com/Polign/polign) is meant
to be built: the corpus lives in object storage, the serving node holds almost
nothing in memory, and every query is a cold read.

The point is not that it searches Wikipedia. The point is what it costs to
serve: **~12M passages on a 2 GB box**, because the node's resident set is a
manifest and a centroid table rather than a corpus.

```
                 build host (hours, then terminated)
   ┌──────────────────────────────────────────────────────┐
   │  prepare.py ──> passage shards ──> cmd/load ──> node │
   └────────────────────────────────────────────┬─────────┘
                                                │ write log, segments,
                                                │ IVF-PQ generation
                                                ▼
                                        ┌───────────────┐
                                        │      S3       │
                                        └───────┬───────┘
                                                │ cold reads (per query)
                 serving host (2 GB)            │
   ┌────────────────────────────────────────────┴─────────┐
   │  cmd/demo ──HTTP /v1──> polign_db node (cold-first)   │
   │   UI + /demo/search        manifest + centroids only  │
   └──────────────────────────────────────────────────────┘
```

## What is in here

| | |
|---|---|
| `prepare/prepare.py` | streams the `wikimedia/wikipedia 20231101.en` dump into passage shards and exports the static embedding model |
| `cmd/load` | embeds passages and upserts them into a running node; resumable, ~4,700/s |
| `cmd/demo` | the public UI and `/demo/search`; stateless apart from a 129 MB embedding model |
| `internal/embedder` | model2vec inference in pure Go, pinned to the reference implementation by golden tests |
| `internal/polign` | a small client for the node's HTTP `/v1` API — standard library only |
| `deploy/` | systemd units, Caddyfile, and the runbook |

This repo depends on **no polign_db packages**. It talks to the database over
the documented wire API, exactly as any other service would.

## Try it locally

Needs a `polign-server` binary and Docker (for `pyarrow`).

```sh
# A small slice — two parquet files, ~250k passages.
mkdir -p data work
docker run --rm -v "$PWD/data:/out" -v "$PWD/work:/work" -v "$PWD/prepare:/prep" \
    python:3.12-slim sh -c 'pip install -q pyarrow && python /prep/prepare.py --out /out --work /work --files 2'

# A node with the whole durable stack behind one flag.
polign-server -store fs:/tmp/polign-store -http 127.0.0.1:23000 &

go run ./cmd/load -dir ./data -addr http://127.0.0.1:23000
go run ./cmd/demo -dir ./data -node http://127.0.0.1:23000 -http :24000
# open http://localhost:24000
```

Search three ways over the same corpus: **semantic** (vector), **keyword**
(BM25), **hybrid** (both legs fused server-side with RRF), plus a compare view
that shows where each result ranked in the single-leg searches.

## The three things that make it small

**Nothing is embedded at query time by a model server.** The embedder is
model2vec (`potion-retrieval-32M`) — a static token-embedding matrix,
mean-pooled and L2-normalized. Pure Go, no inference runtime, no GPU, no API
key. The same code embeds the corpus and the queries, so the two can never
disagree on tokenization; `internal/embedder` is pinned to the Python reference
by golden tests at ~1e-8.

The *retrieval*-distilled model matters more than it sounds: on the general
`potion-base-8M`, "why is the sky blue" ranked "Diffuse sky radiation" second
behind an article literally named "Blue", by 0.001 — and among 12.5M passages
that margin turns into a wrong answer. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**The serving node never restores the corpus.** Left alone, `-store` expands
into a full read-write deployment that rebuilds the index in RAM at boot.
`deploy/polign-node.service` disables restore, the hot tier, persistence,
maintenance, and the write-log tail follower — leaving only the cold read path.
Measured: **15 MiB resident at rest.**

**Query cost is bounded by `nprobe`, not by corpus size.** The node's default
probes `nlist/4` cells, which at this scale means reading a quarter of the
corpus for every query. The deployment pins it. That is the real knob: recall
against IO.

## Honest numbers

Measured on a 60k slice locally (see `deploy/README.md`); the production
figures are in that runbook.

| | |
|---|---|
| load rate | 4,658 passages/s |
| serving node RSS, idle | 15 MiB |
| serving node RSS, under load | ~278 MiB (transient allocations; capped with `GOMEMLIMIT`) |
| cold query, `nprobe=16` | 14 ms |

Cold search is not free: a query reads real bytes out of object storage, and
cells grow as √N, so the same `nprobe` costs more at 12M than at 60k. What
stays flat is memory. If you want single-digit milliseconds on a corpus this
size, you want the hot tier and a bigger box — that is a different deployment,
and polign_db supports it with different flags, not different code.

## Attribution

Text: [English Wikipedia](https://en.wikipedia.org) via the
[wikimedia/wikipedia](https://huggingface.co/datasets/wikimedia/wikipedia)
`20231101.en` dump, CC BY-SA 4.0. Embeddings:
[potion-retrieval-32M](https://huggingface.co/minishlab/potion-retrieval-32M), MIT.
