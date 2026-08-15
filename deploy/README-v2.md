# Deploying the v2 demo (bge-small)

v2 is the same corpus, the same cold-first serving posture, and the same 2 GB
node as [README.md](README.md). One thing changed: the embedding model. That one
change is why this file exists, because it moves work across process and host
boundaries that v1 kept in one place.

## Why v2 exists

The v1 demo embedded with `potion-retrieval-32M`, a model2vec static embedder: a
sentence vector is the normalized mean of its token vectors. That is what made
v1's app so small — no inference runtime, no cgo, pure Go — and it is also the
thing that capped its accuracy. A static model has no attention, so it cannot
represent a relation between words, only their average. Measured on the live v1
deployment:

| query | v1 top hit |
|---|---|
| `capital of france` | History of Rennes — "administrative capital of the French department of Ille-et-Vilaine" |
| `who invented the telephone` | Kazuo Hashimoto — a Japanese answering-machine inventor |
| `causes of world war one` | the correct article ranked 5th |

Those are not retrieval-recall failures. Raising `nprobe` from 1 to 16 on v1 left
the top hits identical, which is the signature of the ranking being as good as
the embedding allows. `capital-ish + France-ish` genuinely does score Rennes
above Paris under a bag-of-token-vectors model.

v2 embeds with `BAAI/bge-small-en-v1.5`: a real 12-layer encoder, 384 dims, run
as int8 ONNX. It costs ~150x more compute per passage, which is what the build
pipeline below is shaped around.

## What that changes structurally

| | v1 | v2 |
|---|---|---|
| query embedding | in Go, in the app process | ONNX sidecar on loopback (`embedserve.py`) |
| passage embedding | `cmd/load` (Go) | `prepare/embed.py` on a 48-vCPU host |
| dims | 512 | 384 |
| app RSS | ~400 MiB (129 MB matrix resident) | ~30 MiB app + ~380 MiB sidecar |
| corpus embed cost | minutes | ~7 h on `c7g.12xlarge` |

The demo is still self-hosted end to end: no model API, no GPU, no key. It is no
longer *in-process*, and the footer says so rather than repeating v1's claim.

**The pairing is not optional.** A collection embedded by one model can only be
queried by that same model — a bge query vector against the potion collection
(or the reverse) is searching a different space and returns near-noise. The two
deployments are therefore fully separate: different collection, different host,
different service files. `polign-demo` refuses to start if `-embed-addr` is set
and the sidecar is not answering, so the mismatch cannot happen silently.

The asymmetric prefix is the subtle half of that contract. bge embeds passages
bare and queries behind `"Represent this sentence for searching relevant
passages: "`. `embedserve.py` owns the prefix and `embed.py` deliberately does
not apply it; getting this backwards costs real recall and nothing errors.

## Build host

`c7g.12xlarge` (48 vCPU, arm64) — memory is irrelevant here, cores are
everything. Measured throughput, int8, length-bucketed batches:

| | |
|---|---|
| per vCPU, 8-vCPU box | 15.8 passages/s |
| per vCPU, 48-vCPU box | ~11 passages/s (memory-bandwidth bound) |
| full corpus (12.5M) | ~7 h, ~$12 |

Two things that did *not* help, so nobody repeats them: x86 with AVX-512 VNNI
(`c7i`) measured 127 passages/s against Graviton3's 119 on the same 8 vCPU — a
wash at higher cost. And N single-threaded ONNX sessions matched one N-threaded
session almost exactly (126 vs 127 passages/s at 8 cores); the work is genuinely
compute-bound, not synchronization-bound.

What *did* help, 1.6x: sorting each window by length before batching. Passages
run p50=105 tokens against a 256 cap, so a naive batch spends most of its FLOPs
on padding.

```sh
# Prepared shards live in the bucket (prepare.py output, model-independent),
# so a build host needs no parquet download and no 50-minute prepare step.
aws s3 sync s3://polign-demo-wiki-en/prepared/ ~/data/

STORE=s3://polign-demo-wiki-en/polign COLLECTION=wikipedia_bge \
MODEL=~/bge-small WORKERS=48 ./build-index-v2.sh
```

### Two lanes, and why the writer one is serial

`build-index.sh` (v1) ran embed → import → embed → import in series. That was
fine when the two costs were comparable. They are not any more: embedding a
305k-passage shard takes ~400 s on 48 vCPU while importing the resulting JSONL
takes ~30 s at 8,775 vec/s. In series the 48-core box idles through every
import.

`build-index-v2.sh` runs them as concurrent lanes, so the entire write cost
disappears under the following embed. The embed lane writes `NNN.jsonl` and then
`NNN.ready` as a completion fence; the writer lane only opens a shard whose
fence exists, so it can never read a half-written file. The on-disk queue is
bounded (`QUEUE_MAX`), which matters only if the writer ever falls behind — it
does not, by an order of magnitude. The log shows the overlap directly:

```
[embed 001/41] passages-00000.jsonl
[embed 002/41] passages-00001.jsonl     <- next shard already embedding
[write 001] importing 1.7G              <- while the last one is written
```

The writer lane is internally serial, and that is a correctness constraint, not
a missed optimization. `segindex.Builder` is a collection's single manifest
writer: it owns the generation counter and the in-memory manifest, so a `Flush`
and a `Compact` *within one process* are serialized by its mutex and can never
race. Two concurrent `polign-import` processes are two Builders over one
collection — a corrupt-manifest race, not a speedup. Since the lane is idle for
~370 s of every 400 s cycle anyway, serializing it costs nothing.

Compaction runs **once, at the end**. A query reads every segment in the cells it
probes (`GETs/query = nprobe × segments-per-cell`), so the index must end
compacted — but `Compact()` rescans and rewrites every cell collection-wide, so
each pass costs the whole corpus. Compacting every K shards would move roughly
`(N/K + 1)/2` times the corpus instead of once (~140 GB against ~56 GB at K=10),
and buys nothing, because nothing queries the collection while it is being built.

It also would not fit in the embed lane's shadow, which is what an earlier
version of this pipeline assumed. `segindex.Compact` walked its cells serially,
making a full pass a multi-hour single-core operation against a ~12 min embed
cycle — it would have stalled the writer, filled the queue, and then blocked the
embed lane behind it. That asymmetry is now fixed upstream (`Compact` rebuilds
cells in waves of `GOMAXPROCS`, 5.9x on 12 cores, byte-identical output), but
the placement argument above holds regardless, so `COMPACT_EVERY` defaults to 0.

One other knob follows from the same reasoning: `IMPORT_BATCH=400000` gives one
flush per shard instead of `polign-import`'s default 100k batches. Each flush
publishes a generation and drops a segment into every cell it touched, and a
100k batch spread over ~3,540 cells leaves ~28 vectors per segment — ~545k tiny
objects for the final compaction to scan, against ~145k at one flush per shard.

## Serving host

`t4g.small`, same cold-first flags as v1. Three processes now share 2 GB:

| | |
|---|---|
| node, idle | 27 MiB |
| node, under queries | ~420 MiB (`GOMEMLIMIT=1000MiB`) |
| embedserve sidecar | ~380 MiB resident |
| demo app | ~30 MiB (no model of its own) |

```sh
sudo /tmp/provision-v2.sh          # installs binaries, venv, model, units
sudo systemctl enable --now polign-node-v2 polign-embedserve polign-demo-v2
sudo cp deploy/Caddyfile-v2 /etc/caddy.Caddyfile && sudo systemctl reload caddy
```

Query-side embedding measured **8–18 ms** on the 2-vCPU box — small against the
cold read it precedes, which is the point of choosing `small` over `base`.

## Known gaps, unchanged from v1

- **Keyword and hybrid modes stay off.** `segindex.SearchText` fetches every live
  lexical segment per query and then iterates every document in each to build
  corpus statistics — ~10 GB at this corpus size, which OOM-kills the node. This
  is measured, not theoretical. Fixing it means partitioning postings by term and
  precomputing per-segment stats into the manifest, at which point hybrid becomes
  affordable here and fixes the keyword-shaped queries an embedder never will.
- **The cold path reads full f32 segments.** The engine has `internal/ivfpq` with
  `CodesOnly` generations and exact rescore, but only the hot tier uses them; a
  cold ADC pass over PQ codes would cut bytes-per-query ~50x and let `nprobe` go
  up rather than down.
