#!/bin/bash
# Build the v2 (bge-small) collection from prepared passage shards, running the
# embedder and the index writer as two concurrent lanes.
#
# Why two lanes. build-index.sh runs embed -> import -> embed -> import strictly
# in series, which was tolerable when embedding a shard took about as long as
# importing it. bge-small changed that ratio completely: embedding a 305k-passage
# shard takes ~400 s on 48 vCPU, while importing the resulting JSONL takes ~30 s.
# In series, the 48-core box sits idle during every import and the writer sits
# idle during every embed. Overlapping them hides the entire write cost under the
# embed that follows it.
#
# Why the writer lane is internally serial. segindex.Builder is a collection's
# single manifest writer: it owns the generation counter and the in-memory
# manifest, so a Flush and a Compact inside one process are serialized by its
# mutex and can never race. Two concurrent polign-import processes are two
# Builders over one collection, which is a corrupt-manifest race, not a speedup.
# So this lane runs import and compaction one at a time -- and gets them for free
# anyway, because it is idle most of each embed cycle.
#
# Compaction runs once, at the end, and that is a deliberate choice rather than
# laziness. A query reads every segment in the cells it probes (GETs/query =
# nprobe x segments-per-cell), so the index must end compacted -- but Compact()
# rescans and rewrites every cell collection-wide, so each pass costs the whole
# corpus. Compacting every K shards would move roughly (N/K + 1)/2 times the
# corpus in total instead of once: at 41 shards and K=10 that is ~140 GB of
# rewrite against ~56 GB. Intermediate passes buy nothing either, because
# nothing queries the collection while it is being built.
#
# They also do not fit. Unlike Flush, segindex.Compact walks its cells strictly
# serially (internal/segindex/compact.go), so a full pass at this scale is a
# multi-hour single-core operation -- far longer than the ~12 min embed cycle it
# would have to hide under. It would stall the writer lane, fill the queue, and
# then block the embed lane behind it, which is the opposite of the point.
# Set COMPACT_EVERY to a positive number only for a run you expect to interrupt.
#
#   STORE=s3://bkt/polign MODEL=~/bge-small ./build-index-v2.sh
set -u

DATA=${DATA:-$HOME/data}
LOGS=${LOGS:-$HOME/logs}
WORK=${WORK:-$HOME/emit}
STORE=${STORE:?set STORE, e.g. s3://your-bucket/polign}
MODEL=${MODEL:-$HOME/bge-small}
COLLECTION=${COLLECTION:-wikipedia_bge}
PYTHON=${PYTHON:-$HOME/venv/bin/python}
EMBED=${EMBED:-$HOME/polign_demo/prepare/embed.py}
WORKERS=${WORKERS:-$(nproc)}
# How many embedded-but-unimported shards may sit on disk. Each is ~1.7 GB, and
# the queue only ever fills if the writer falls behind the embedder, which is
# not the expected regime -- it is a disk-space bound, not a tuning knob.
QUEUE_MAX=${QUEUE_MAX:-3}
# 0 = compact only at the end (see the note above). A positive value compacts
# every N shards as well.
COMPACT_EVERY=${COMPACT_EVERY:-0}
# One flush per shard rather than polign-import's default 100k batches. Each
# flush publishes a generation and drops a segment into every cell it touched,
# and a 100k batch spread over ~3,540 cells leaves ~28 vectors per segment: at
# 12.5M that is ~545k tiny objects for the final compaction to scan, against
# ~145k at one flush per shard. Costs ~1.5 GB of build-host RAM per flush, which
# a 96 GB box does not notice.
IMPORT_BATCH=${IMPORT_BATCH:-400000}
# Centroid training happens once, on the first import, and every query for the
# life of the collection is routed by the result. nlist here is ~sqrt(12.5M) =
# 3,538, and the usual guidance is at least ~39 samples per centroid; the tool's
# 100k default would give 28. 250k gives ~71 and costs about a minute of k-means
# on 48 cores. It is drawn from the first shard, so shard order must not be
# correlated with content -- Wikipedia's parquet ordering is by article id, which
# is close enough to arbitrary.
TRAIN_SAMPLE=${TRAIN_SAMPLE:-250000}
export AWS_REGION=${AWS_REGION:-us-east-1}

mkdir -p "$WORK" "$LOGS"
shards=$(ls "$DATA"/passages-*.jsonl | sort)
total=$(echo "$shards" | wc -l | tr -d ' ')
expected=$(python3 -c "import json;print(json.load(open('$DATA/corpus.json'))['passages'])")

echo "collection : $COLLECTION"
echo "store      : $STORE"
echo "shards     : $total ($expected passages)"
echo "embed lane : $WORKERS workers"
echo

# --- embed lane -------------------------------------------------------------
# Emits $WORK/NNN.jsonl, then $WORK/NNN.ready as the completion fence. The
# writer lane only ever opens a file whose .ready exists, so it can never read a
# half-written JSONL.
embed_lane() {
    local i=0
    for shard in $shards; do
        i=$((i + 1))
        local n; n=$(printf "%03d" $i)
        [ -f "$WORK/$n.done" ] && continue          # already imported
        [ -f "$WORK/$n.ready" ] && continue          # already embedded

        # Bound the on-disk queue.
        while [ "$(ls "$WORK"/*.ready 2>/dev/null | wc -l)" -ge "$QUEUE_MAX" ]; do
            sleep 10
        done

        echo "[embed $n/$total] $(basename "$shard")"
        if ! "$PYTHON" "$EMBED" --model "$MODEL" --shard "$shard" \
                --out "$WORK/$n.jsonl" --workers "$WORKERS" >> "$LOGS/embed.log" 2>&1; then
            echo "[embed $n] FAILED — see $LOGS/embed.log"
            touch "$WORK/EMBED_FAILED"
            return 1
        fi
        touch "$WORK/$n.ready"
    done
    touch "$WORK/EMBED_COMPLETE"
    echo "[embed] all $total shards embedded"
}

# --- writer lane ------------------------------------------------------------
# One process at a time against the collection: import, import, ..., compact.
writer_lane() {
    local imported=0
    while true; do
        local next=""
        for f in $(ls "$WORK"/*.ready 2>/dev/null | sort); do
            next=$(basename "$f" .ready); break
        done

        if [ -z "$next" ]; then
            if [ -f "$WORK/EMBED_COMPLETE" ]; then break; fi
            if [ -f "$WORK/EMBED_FAILED" ]; then
                echo "[write] embed lane failed; stopping"
                return 1
            fi
            sleep 10
            continue
        fi

        echo "[write $next] importing $(du -h "$WORK/$next.jsonl" | cut -f1)"
        if ! polign-import -stores "$STORE" -collection "$COLLECTION" \
                -metric l2 -dim 384 -expected-size "$expected" \
                -batch "$IMPORT_BATCH" -train-sample "$TRAIN_SAMPLE" \
                -compact=false "$WORK/$next.jsonl" >> "$LOGS/import.log" 2>&1; then
            echo "[write $next] import FAILED — see $LOGS/import.log"
            return 1
        fi
        rm -f "$WORK/$next.jsonl" "$WORK/$next.ready"
        touch "$WORK/$next.done"
        imported=$((imported + 1))

        # Optional checkpoint compaction; off by default, see the note above.
        if [ "$COMPACT_EVERY" -gt 0 ] && [ $((imported % COMPACT_EVERY)) -eq 0 ]; then
            echo "[write] checkpoint compaction after $imported shards"
            if ! polign-import -stores "$STORE" -collection "$COLLECTION" \
                    -compact -compact-min-segments 2 >> "$LOGS/import.log" 2>&1; then
                echo "[write] checkpoint compaction FAILED — see $LOGS/import.log"
                return 1
            fi
        fi
    done

    echo "[write] final compaction (min-segments 1: one physical copy per id)"
    if ! polign-import -stores "$STORE" -collection "$COLLECTION" \
            -compact -compact-min-segments 1 >> "$LOGS/import.log" 2>&1; then
        echo "[write] final compaction FAILED — see $LOGS/import.log"
        return 1
    fi
    echo "[write] index complete"
}

embed_lane & EMBED_PID=$!
writer_lane & WRITE_PID=$!

wait $EMBED_PID; embed_rc=$?
wait $WRITE_PID; write_rc=$?

if [ $embed_rc -ne 0 ] || [ $write_rc -ne 0 ]; then
    echo "build FAILED (embed rc=$embed_rc, write rc=$write_rc)"
    exit 1
fi
echo "build complete: $COLLECTION"
