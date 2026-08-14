# How the demo is put together

This is a deployment note, not a design document for polign_db itself. It
explains the choices that let ~12.5M passages be served from a 2 GB box, and —
just as usefully — the ones that would have quietly broken it.

## Two hosts with opposite problems

**The build host** needs a lot of everything, briefly: it downloads ~20 GB of
parquet, writes ~7 GB of JSONL, embeds 12.5M passages, and publishes an IVF-PQ
generation. It is a `c7g.2xlarge` for a few hours and then it is terminated.

**The serving host** needs almost nothing, indefinitely. It answers queries out
of object storage. It is a `t4g.small` and it stays up.

They share an S3 bucket and nothing else.

## The write path holds everything in RAM (and why that shaped the load)

A node applies every write to its in-memory index *as well as* appending it to
the durable write log. That is the right default — it is what makes a write
readable the moment it is acknowledged — but at this scale it costs twice:

- **Memory.** One long-lived process loading 12.5M passages needs roughly
  25 GB resident. The build host has 15 GB.
- **Time.** HNSW insertion slows as the graph grows. Measured on the build
  host: **~1,700 passages/s into an empty index, ~400/s once it held ~1M.**
  Left alone, the load decays as it runs.

Neither cost buys anything here, because the serving node never loads this
index — it reads segments from object storage. The write log is the source of
truth, so `deploy/load-chunked.sh` loads one shard at a time and restarts the
node between shards with `-restore-stores ""`. Each restart starts with an
empty in-memory index, so insertion stays at its fast rate, while the persistor
keeps consuming the log and writing the segments that actually get served.
`cmd/load` takes `-shards N` to bound one run, and its checkpoint makes the
next run resume exactly where the last stopped.

The restarts are not a workaround for a bug. They are what it looks like to
use a write path optimized for read-your-writes to do a bulk import — the
work it does for freshness is work this pipeline throws away.

This is worth stating plainly: **the demo's load is chunked because of a memory
property of the write path, not because of anything about the corpus.**

## What the serving node does not do

`-store` is a preset. On its own it expands into a complete read-write
deployment: write log, persistence, cold-first serving, hot tier, disk cache,
generation adoption, and periodic maintenance. Most of that is wrong for a
read-only demo on a small box, so `deploy/polign-node.service` switches it off
piece by piece:

| flag | why |
|---|---|
| `-restore-stores ""` | otherwise the node rebuilds all 12.5M vectors in RAM at boot and dies |
| `-hot-max 0` | hot promotion transiently materializes the full index |
| `-persist=false` | the build host already consumed the log |
| `-maintain 0` | publishing generations is the build host's job |
| `-tail-fresh=false` | a read-only demo has no writes to catch up on, and the tail follower cost ~450 MiB |
| `-log-stores ""` | same reason |

What remains is the cold read path: the manifest, the centroid table, and the
tombstone set stay resident; cell segments are fetched per query and cached on
local disk. **15 MiB at rest.**

## nprobe is the knob that matters

Cold search partitions the corpus into `nlist ≈ √N` cells and reads the
`nprobe` nearest ones per query. The node's default `nprobe` is `nlist/4`,
which is fine at small scale and ruinous at this one — a quarter of 12.5M
passages fetched from S3, per query.

So the deployment pins it. Measured on a 60k slice locally:

| nprobe | latency | top hit for "why do volcanoes erupt" |
|---|---|---|
| 4 | 6 ms | missed |
| 8 | 10 ms | missed |
| 16 | 14 ms | found |
| 32 | 22 ms | found |

Cost is roughly linear in `nprobe`, and a cell at 12.5M holds ~14× the vectors
a cell at 60k does, so the same `nprobe` moves ~14× the bytes. The production
value is set in `deploy/polign-demo.service` and tuned against the real corpus.

## Why L2 and not cosine

The embedder L2-normalizes every vector, and for unit vectors squared L2 and
cosine rank identically: `‖a−b‖² = 2 − 2·cos(a,b)`. The demo therefore leaves
the node on its default metric and converts for display
(`polign.Cosine`). This is not a compromise — IVF-PQ encodes residuals for L2,
which is the more accurate of the two paths.

## The embedder, and why it is the retrieval model

model2vec static embeddings: a token-embedding matrix, mean-pooled over
WordPiece tokens and L2-normalized. No inference runtime, no GPU, no API key —
just weights and arithmetic. `internal/embedder` is a Go port pinned to the
Python reference by golden tests at ~1e-8, including the load-bearing detail
that `[UNK]` tokens are dropped from the mean rather than contributing their
(non-zero, norm ~16) row.

The demo first ran on `potion-base-8M` (256-dim, general purpose) and the
results were the reason for the switch. Scaling the corpus from 100k to 12.5M
fixed *coverage* — hybrid search started finding Apollo 11 — but semantic
search still answered "why is the sky blue" with **Conair Group**. Ranking real
corpus passages with each model shows why:

| rank | potion-base-8M | potion-retrieval-32M |
|---|---|---|
| 1 | Blue (+0.345) | **Diffuse sky radiation** (+0.372) |
| 2 | Diffuse sky radiation (+0.344) | Blue (+0.343) |

The general model had the right answer *second, by 0.001* — a margin that
survives a 5-document test and vanishes among 12.5M distractors. The
retrieval-distilled model puts it first.

The cost is dimensionality: 512 instead of 256, so every vector is twice the
bytes in storage and twice the bytes a cold query fetches, and the weight
matrix is 129 MB instead of 30 MB. That is the trade this demo makes — a
corpus this large is not worth much if the top result is wrong.

Any model2vec static model works as a drop-in: `prepare.py` asserts the
tokenizer is WordPiece + BertNormalizer and writes the dimension into
`model.json`, and the Go embedder reads it. Changing `MODEL_REPO` requires
rebuilding the corpus, because vectors from two models cannot be compared.

The same code embeds the corpus in `cmd/load` and the queries in `cmd/demo`, so
the two cannot drift apart.

**One trap worth knowing:** model files all ship under the same names
(`model.safetensors`, `tokenizer.json`), so a flat download cache silently
serves the previous model's weights after `MODEL_REPO` changes. `prepare.py`
namespaces its cache per repo. The first switch here exported a matrix labelled
`potion-retrieval-32M` that was actually the 8M weights; the golden test caught
it, because the reference vectors were 512-dim and the exported matrix was 256.

## Failure modes worth knowing

- **`object not found` right after a load.** A cold searcher can hold a
  manifest whose cells have since been compacted away. It resolves itself at
  the next `-segment-refresh`.
- **Queries return nothing on the serving node.** The in-memory index is empty
  by design, so a query that is not `cold` finds nothing. `cmd/demo` always
  sets it.
- **`pkill -f polign-server` over SSH kills your own shell,** because the
  command line matches the pattern. Use `pkill -x`.
