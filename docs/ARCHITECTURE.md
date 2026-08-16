# How the demo is put together

A deployment note, not a design document for polign_db itself. It explains the
choices that let 12.5M passages be served from a 2 GB box — and, just as
usefully, the ones that would have quietly broken it.

## Two hosts with opposite problems

**The build host** needs a lot of everything, briefly: it downloads ~20 GB of
parquet, writes ~7 GB of JSONL, embeds 12.5M passages, and builds the index. It
is a 48-vCPU machine for a few hours and then it is terminated.

**The serving host** needs almost nothing, indefinitely. It answers queries out
of object storage. It is a `t4g.small` and it stays up.

They share a bucket and nothing else. Almost every decision below follows from
wanting the expensive machine to be temporary.

## The index is written directly, not through a node

The obvious way to load a corpus is to upsert it into a running node. That is
the wrong shape for a bulk import, because a node applies every write to its
in-memory index *as well as* appending it to the durable log. For an import that
costs twice:

- **Memory.** One process holding 12.5M passages needs tens of GB.
- **Time.** Graph insertion slows as the graph grows — roughly 1,700/s into an
  empty index, ~400/s at a million vectors — and insertion is single-writer, so
  one node cannot use more than about a core for it.

`polign-import` writes segments straight to object storage instead: memory is
O(batch), there is no log, and throughput is bounded by bulk segment building
rather than per-vector graph inserts (~5,000/s here). A cold-serving node picks
the result up on its next segment refresh.

The one rule: **a collection has a single index writer.** Two concurrent
`polign-import` processes over one collection race the manifest. That is why
`index/build-index.sh` overlaps embedding with importing but keeps the importing
itself serial.

## What the serving node does not do

`-store` is a convenience preset: point it at a bucket and you get a full
read-write deployment — write log, persistence, hot tier, maintenance, adoption.
That is the opposite of what this host wants. Every flag in
`deploy/polign-node.service` switches a piece of it off:

| flag | what it prevents |
|---|---|
| `-restore-stores ""` | rebuilding every vector in RAM at boot |
| `-hot-max 0` | promotion, which transiently materialises the whole index |
| `-persist=false` | consuming the write log (the build host already did) |
| `-maintain 0` | index maintenance on the serving path |
| `-tail-fresh=false`, `-log-stores ""` | the write-log follower, worth hundreds of MB |

What is left is the cold read path: manifest and centroids resident, cells
fetched per query and cached on local disk. **27 MiB at rest.**

Each of those was measured, not guessed. Dropping `-restore-stores ""` alone is
enough to kill the box at boot.

## Query cost is bytes, not corpus size

A cold query reads the cells it probes. Cells hold ~√N vectors, so the same
`nprobe` costs more as the corpus grows — that, not memory, is what scales.

Three things bound it:

**PQ codes.** The collection is built with `-pqm 96`: each cell also carries
product-quantization codes, 96 bytes per vector against 1,536 for the exact one.
A query scans codes to pick candidates, then re-ranks those candidates against
exact vectors fetched by ranged read. **The distances returned are exact** — the
compression only decides which records get re-ranked, so the rescore pool is a
recall knob rather than a fidelity setting.

**Metadata is not read to score.** A segment stores each record's metadata —
here, the passage text itself — after everything the search traverses. The node
reads the prefix it needs and fetches metadata only for records that become
hits.

**`nprobe` is pinned.** The node's default probes `nlist/4`, which at this scale
means reading a quarter of the corpus per query. The deployment sets it
explicitly.

## nprobe is not a quality knob here

The natural assumption is that probing more cells finds better results. Measured
on this corpus, quality is **flat from nprobe 8 to 64** — identical MRR at every
value. IVF recall is not the limiting factor; the embedding model is.

So `nprobe` is purely a cost decision here, and the demo uses the cheapest
setting that loses nothing. Do not inherit that conclusion — measure whether
recall binds on your corpus before paying for it.

## The embedding model is the retrieval model

The largest quality lever by a wide margin is which model embeds the text.
Swapping a static token-averaging model for a sentence-transformer tripled MRR
on a fixed query set; no index tuning came close. See [../eval/](../eval/).

Two consequences worth internalising:

**A collection can only be searched by the model that wrote it.** A vector from
another model is a point in a different space; the nearest neighbours it finds
are meaningless, and nothing errors. `index/embed.py` and `serve/embedserve.py`
share one model directory for that reason, and the app refuses to start when the
sidecar is unreachable.

**The prefix asymmetry is real.** This model expects passages embedded bare and
queries embedded behind an instruction prefix. The sidecar owns that prefix so a
caller cannot forget it — and `index/embed.py` deliberately does not apply it.

## Why L2 and not cosine

The embedder L2-normalizes its output, and for unit vectors squared-L2 and
cosine rank identically (`‖a−b‖² = 2 − 2·cos`). Using L2 keeps the collection on
the metric the index is most direct about, and the app converts to cosine only
for display.

## Hybrid search is not free quality

polign_db fuses a vector leg and a BM25 leg server-side with reciprocal-rank
fusion. The usual advice is that hybrid beats either alone. **On this corpus it
does not** — semantic scores roughly twice hybrid's MRR.

The reason is worth understanding before copying the pattern: BM25 here surfaces
disambiguation and list pages, because they are short and dense in the query
terms, and the analyzer has no stop-word handling or title-field weighting. RRF
then drags a good semantic ranking toward a poor lexical one. Fusion amplifies
whatever the weaker leg is doing.

The demo keeps all three modes selectable, and defaults to the first entry of
`-modes` so the operator chooses rather than inheriting an assumption.

## Failure modes worth knowing

- **A cold searcher can outrun its segments.** Right after a load the node may
  hold a manifest whose cells have been compacted away; queries fail with
  `object not found` until the next refresh. Self-healing.
- **The text leg needs a compaction cadence.** A text query touches every live
  lexical segment chain, so without compaction its per-query cost grows with the
  number of flushes.
- **Text freshness is persist-lag.** Newly written text becomes searchable when
  it is persisted, not when it is acked.
- **The app must restart with the node.** `BindsTo` alone propagates a stop but
  not a restart, which is how this demo once went dark after a routine binary
  upgrade. `PartOf` is what fixes it.
