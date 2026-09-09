# Wikipedia read benchmark: what limits queries per second

Companion to [the benchmark plan](WIKIPEDIA_READ_BENCHMARK_PLAN.md). The plan
describes how the measurements are taken. This document records what they say
about throughput and which changes actually raise it.

Corpus: 12,519,135 passages, 384 dimensions, generation 45, 3,538 IVF cells,
full-precision vectors and no PQ codebook. The index occupies 81.7 GiB across
142,653 objects in `s3://polign-demo-wiki-en-uw1/polign-v4`. All numbers below
come from `bench/wikipedia/runs`; regenerate the tables and charts with
`analysis/throughput.py`. The per-tier cost, memory and CPU table behind the
blog post is `analysis/tiers.py` (rates are in the file), and
`analysis/tier_chart.py --inject <post.html>` regenerates the inline SVG figure
between the `tier-chart` markers in the post from that table.

## 1. The limit is bytes per query, not CPU

Every cold query reads whole IVF cells out of the object store. At `nprobe=8`
one query pulls roughly 65 MiB. So a node's query rate is simply

```
queries per second  =  network allowance (MiB/s)  /  bytes read per query (MiB)
```

and every measurement lands on that line. On the `c7g.xlarge` node:

| nprobe | MiB per query | QPS | Achieved | Share of the 12.5 Gb/s burst |
| --- | --- | --- | --- | --- |
| 8 | 65.4 | 20.3 | 11.1 Gb/s | 89% |
| 4 | 28.5 | 43.7 | 10.4 Gb/s | 83% |
| 2 | 14.2 | 59.0 | 7.0 Gb/s | 56% |
| 1 | 7.1 | 74.3 | 4.5 Gb/s | 36% |

CPU never became the constraint. With the byte cost cut to `nprobe=1`, the same
node served **146.7 QPS at concurrency 16 and 211.7 QPS at concurrency 32**,
still without saturating its processors. The earlier "about 20 QPS" ceiling was
entirely a bytes-per-query effect.

## 2. Most published numbers were spending burst credit

EC2 instances have a sustained network allowance and a much larger burst
allowance drawn from a credit balance. Short runs measure the burst; long runs
settle onto the baseline.

| Instance | Sustained | Burst |
| --- | --- | --- |
| `t4g.small` | 0.128 Gb/s (15 MiB/s) | 5 Gb/s (596 MiB/s) |
| `c7g.xlarge` | 1.875 Gb/s (224 MiB/s) | 12.5 Gb/s (1,490 MiB/s) |
| `c7g.4xlarge` | 7.5 Gb/s (938 MiB/s) | 15 Gb/s (1,788 MiB/s) |

The four `optimize-memory-zipf-*` runs are the clearest evidence. They report
8.8, 8.8, 9.1 and 8.8 QPS at concurrency 4, 8, 16 and 16, flat, because all
four were pinned at **1.88 to 1.94 Gb/s**, the `c7g.xlarge` baseline. They were not
measuring the query mix; they were measuring the network floor after the
preceding runs had drained the credit balance. Read them as the honest sustained
figures, not as a zipf result.

Consequence: at `nprobe=8` with a 256 MiB segment cache, sustained capacity on
`c7g.xlarge` is about **3.6 QPS**, against the ~20 QPS a 60-second run reports.

The `t4g.small` node is the extreme case. Its runs sustained 16 to 35 times its
own baseline allowance while credits lasted, then collapsed: the `nprobe=8` load
run returned 0 successful queries and 24 errors, and a 100-question quality pass
took 583 seconds. A 15 MiB/s allowance cannot carry a 65 MiB query.

## 3. Concurrency above the memory budget kills the process

Each in-flight query holds its probed cells in memory, so transient footprint is
roughly `concurrency × nprobe × 8.1 MiB`. At `nprobe=8` and concurrency 16 that
is about 1 GiB of buffers on top of the segment cache, which exceeds a 1,536 MiB
cgroup budget. The `ceiling-nprobe8-c16` and `-c32` runs did not merely degrade:

```
wikibench.service: A process of this unit has been killed by the OOM killer.
wikibench.service: Main process exited, code=killed, status=9/KILL
```

The driver then recorded 1.38M and 2.03M connection failures against a dead
server. A usable rule for sizing:

```
safe concurrency  ≈  (MemoryMax − segment cache − 200 MiB) / (nprobe × 8.1 MiB)
```

which gives 16 for the 1,536 MiB profile, exactly where it died, and about 60
for the 6 GiB profile, which survived concurrency 16 with degradation but no
kill. Bounding in-flight cold queries by admission control, rather than letting
concurrency translate directly into buffer memory, would turn this hard failure
into backpressure.

## 4. The disk cache lost for a fixable reason

In the pilot, enabling the disk cache made everything 5 to 9 times slower, and
the tuned large-memory profile was worse still (3.0 to 3.7 QPS at every
concurrency, with the volume 90 to 97% busy). That looked like a cache design
problem. It was not.

The cache was working: S3 bytes per query fell from about 65 MiB to 12 MiB. The
problem is that the volumes were gp3 at the default **125 MiB/s**, roughly half
the instance's own sustained network allowance and one twelfth of its burst. The
cache was serving from storage slower than the network it replaced.

## 5. What raises throughput

Ordered by value, with the quality cost stated.

**Lower `nprobe` to 4.** Quality on 100 held-out NQ-Open tuning questions:

| nprobe | Hit@1 | Hit@3 | Hit@10 | MRR |
| --- | --- | --- | --- | --- |
| 1 | 0.220 | 0.310 | 0.420 | 0.277 |
| 2 | 0.310 | 0.400 | 0.500 | 0.368 |
| 4 | 0.350 | 0.430 | 0.580 | 0.413 |
| 8 | 0.380 | 0.480 | 0.600 | 0.446 |

Going from 8 to 4 costs 2 points of Hit@10 (0.600 to 0.580, 3.3% relative) and
buys 2.3× fewer bytes and 2.15× the query rate. Going to 2 costs 10 points of
Hit@10 and is not worth it.

**Turn hedged reads off.** Hedging spends bandwidth to cut tails, which is the
wrong trade when bandwidth is the constraint. On the medium node it cost 19% of
throughput (20.8 QPS with 150 ms hedging, 24.8 QPS without) at 3.3 hedges per
query. On the small node under stress it reached 170 hedges per query and turned
a slowdown into a collapse.

**Give the segment cache real memory.** Raising it from 256 MiB to 2 GiB cut
bytes per query from 65 MiB to 43 MiB and lifted burst throughput from 20.9 to
32.6 QPS at concurrency 8.

**Match storage to the network.** A cache only helps if it is faster than the
network it replaces. That means a volume provisioned well above 125 MiB/s, or
local NVMe.

**Buy network, not cores.** Sustained capacity scales directly with the
instance's baseline allowance, so a tier with 4× the sustained bandwidth serves
about 4× the queries at the same bytes per query.

## 6. Measured results

All phases complete. The `throughput` phase confirmed the model precisely: the
four ten-minute runs on `c7g.xlarge` landed at 98, 98, 99 and 99 percent of that
instance's sustained network allowance.

Sustained on `c7g.xlarge`, 2 GiB segment cache, hedging off, concurrency 8:

| nprobe | MiB/query | QPS | Hit@10 |
| --- | --- | --- | --- |
| 8 | 36.5 | 6.0 | 0.600 |
| 4 | 17.3 | 12.7 | 0.580 |
| 2 | 8.2 | 26.8 | 0.500 |

`sustained-base` reported 6.7 QPS but ran first and still held burst credit
(208% of the sustained allowance), so it is not comparable with the rest. Its
request log is the clearest artifact in the series: it holds above 20 QPS for
130 seconds and then falls to 2.8 when the credit runs out.

**A warm local cache on a fast volume is the largest single lever.** With the
volume at 1,000 MiB/s and the cache sized to hold the index, the object store
leaves the request path (measured S3 traffic per query falls to zero) and the
workload stops being network-bound:

| Node | Path | Bound by | nprobe=8 | nprobe=4 |
| --- | --- | --- | --- | --- |
| `c7g.xlarge` | direct S3 | network, 224 MiB/s | 6.0 | 12.7 |
| `c7g.xlarge` | warm cache | volume, 1,000 MiB/s | 31.2 | 65.5 |
| `c7g.4xlarge` | direct S3 | network, 1,749 MiB/s | 50.6 | 104.5 |
| `c7g.4xlarge` | warm cache | processor | 1,027.0 | 2,033.5 |

The 4 vCPU node gains 5.2× and stops at its volume; the 16 vCPU node gains 20×
because its 30 GiB of RAM holds the working set in page cache. Accuracy is
unchanged in every pair.

Verification held each headline configuration for twenty minutes, reproducing
the ten-minute runs to within 0.2% with zero errors (2,440,234 queries at
nprobe=4, 1,232,458 at nprobe=8, 125,150 for the uncached comparison). The
`c7g.4xlarge` also held 1,749 MiB/s uncached for a full twenty minutes without
decay, so the burst-credit cliff that dominates `c7g.xlarge` does not appear at
that instance size within the window measured.

## 7. Instance-store NVMe instead of a provisioned volume

Second series, all spot, cache directory on the instance's own NVMe (237 GB
on both xlarge shapes), no EBS beyond the 20 GiB root. nprobe=4, hedging off,
2 GiB segment cache, ten-minute holds, zero errors.

| Node | RAM | Go limit | QPS | p99 | RSS | CPU | S3/query | Spot, all in | $/1M searches |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r7gd.xlarge | 32 GiB | 20 GiB | 861 (865 at c8, p99 28 ms) | 92 ms at c32 | 6.9 GiB | 91% | 0.00 | $96.76 | $0.043 |
| c7gd.xlarge | 8 GiB | 5 GiB | 18.8 | 2.8 s | ~5 GiB | 3% (95% iowait) | 0.8 falling | $55.01 | ~$1.4 |

The r7gd result is the best cost per search of the whole study: less than half
the c7g.4xlarge-plus-EBS row's cost per search at a third of its monthly bill.
Its working set sits in page cache (about 22 GiB free after the Go heap), so
the NVMe is barely touched; the box is CPU-bound at four cores and c8 already
saturates it.

The c7gd result is an honest disappointment with a clear cause. An xlarge gets
one eighth of a physical drive, and that slice delivered ~300 MB/s at 100%
utilization with 11 ms per read: slower than the 1,000 MiB/s EBS volume it
replaced. With the Go heap budgeted at 5 GiB only ~1.8 GB of page cache
remained, so nearly every query went to the device. c16 gave the same rate with
twice the p99. It is still cheaper per search than the same box reading from S3
(S3 requests near zero at the same spot price), just not by the margin
projected. A rerun with the RAM given to the page cache instead of the heap
(`nvme2`: 2 GiB limit, 512 MiB segment cache) follows; the lesson either way is
that on small boxes the cache has to be in memory, and RAM per dollar is the
number to shop by.

## 8. Where the S3 requests go, and the metadata cap

Every S3 request a search makes is a ranged read (no HEADs, no LISTs). The
count is about 6.5 fixed per query plus 1.4 per probed cell: 8.0, 12.2 and
16.7 at nprobe 1, 4 and 8. The head-read path in `internal/segindex/searchhead.go`
explains the split. For a text collection the searcher reads each probed cell
only as far as its metadata directory (one ranged read per cell, coalesced
when co-probed cells sit adjacently in a pack), then issues one wave of ranged
reads for the winners' metadata records: one per hit, k of them, because
passages of a kilobyte or more inside cells of ~3,500 records sit far beyond
the 64 KiB coalescing gap. At nprobe=4 and k=10 that is roughly 7 of the 12
requests, about 60% of the $4.80 per million searches, spent fetching passage
text rather than searching.

Prototype (worktree `polign_db-metak`, v0.5.0 plus the telemetry patch): a
per-query `metadata_k` following the codebase's `rescore` convention, 0 = every
hit carries metadata (unchanged default), n > 0 = only the n closest, -1 =
none. Plumbed HTTP `metadata_k` -> `service.SearchOptions.MetadataK` ->
`Searcher.SearchFilteredRescoreExcludingMetadata` -> `searchHeads`, which trims
the record wave to the closest n (the merged hit list is sorted closest-first
before the wave). Hits beyond the cap keep id and distance. gRPC/proto not yet
extended. Unit test `TestHeadSearchMetadataCap` on a corpus with wide records:
reads per search 15 / 11 / 8 for all / top 3 / none at nprobe=8, i.e. the
eight head reads plus one per hydrated hit. The load generator gained
`-metadata-k` (and its validator had to learn that hits past the cap carry no
metadata: the first `metak` pass rejected every capped response client-side,
which is why `metak2` exists; the S3 counters of the first pass are still valid
per attempted query).

Measured on nvme-c7gd reading straight from S3, nprobe=4, 2 GiB segment cache,
server restarted before each setting:

| metadata_k | GETs/query, 100 held-out | GETs/query, load set | Hit@3 |
| --- | --- | --- | --- |
| 0 (all ten) | 11.42 | 10.59 | 0.430 |
| 3 | 6.13 | 5.29 | 0.430 |
| -1 (none) | 3.32 | 2.51 | n/a |

Hits and distances identical across settings. "None" lands below the naive
"4.5 head reads" estimate because co-probed cells' heads coalesce inside packs
and the segment cache serves some of them. On the S3 path this halves to
quarters the request line ($4.80 per million -> ~$2.20 -> ~$1.05); on the cached
boxes it changes nothing because they make no requests. Not merged; the
prototype lives in the `polign_db-metak` worktree and the gRPC/proto field is
still to do.

## 9. Open items

- Second series done (2026-09-06 evening, all spot): `nvme`, `nvme2`, `metak`,
  `metak2` phases; results in `runs/nvme-*` and `runs/nvme-c7gd-metak*`;
  `analysis/tiers.py` publishes the NVMe rows and keeps the provisioned-volume
  rows out of the post. c7gd.xlarge with the page-cache-friendly budget: 20.7 QPS
  (vs 18.8 heap-heavy); the xlarge NVMe slice is the ceiling either way.
- A PQ codebook would cut bytes per query by roughly an order of magnitude and
  is the largest remaining lever for the uncached path, but generation 45 has
  none and this benchmark does not modify the index.
- Admission control on in-flight cold queries would convert the concurrency
  OOM in section 3 into backpressure.
- Whether the `c7g.4xlarge` uncached path holds beyond twenty minutes is not
  established.

Published as [a blog post](https://polign.com/blog-wikipedia-throughput)
covering the best-case configuration and results.
