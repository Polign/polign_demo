#!/bin/bash
# Build a collection's segment index from prepared passage shards.
#
# This is the bulk ingest path, and it does not involve a running node at all:
# for each shard, `load -out` embeds the passages to JSONL, `polign-import`
# turns that JSONL into per-cell segments in object storage, and the JSONL is
# deleted before the next shard. A cold-serving node picks the result up on its
# next segment refresh.
#
# Why not write through a node: the online write path durably logs every record
# and buffers it in memory until the persistor covers it with a segment.
# Measured on this corpus, writes outrun segment building ~4:1, so a bulk load
# through that path either backpressures down to the persistor's rate or fills
# node memory with unindexed records. Importing writes the segments directly —
# O(batch) memory, no log, no buffer.
#
# Per shard rather than one pass over everything: 12.5M 512-dim vectors as
# JSONL is ~90 GB, and there is no reason to hold it. One shard is ~2 GB.
#
# Compaction is deferred to the final shard. A query reads every segment in the
# cells it probes, so the index must end compacted — but each compaction
# rescans the whole collection, so running one per shard would cost more the
# further the load progressed, for no benefit while nothing is querying it.
#
#   STORE=s3://my-bucket/polign ./build-index.sh
set -u

DATA=${DATA:-$HOME/data}
LOGS=${LOGS:-$HOME/logs}
STORE=${STORE:?set STORE, e.g. s3://your-bucket/polign}
COLLECTION=${COLLECTION:-wikipedia}
WORK=${WORK:-$HOME/emit}
export AWS_REGION=${AWS_REGION:-us-east-1}

mkdir -p "$WORK" "$LOGS"
total_shards=$(python3 -c "import json;print(len(json.load(open('$DATA/corpus.json'))['shards']))")
expected=$(python3 -c "import json;print(json.load(open('$DATA/corpus.json'))['passages'])")

echo "building $COLLECTION from $total_shards shards ($expected passages) into $STORE"

for n in $(seq 1 "$total_shards"); do
    done_n=$(python3 -c "
import json,os
p='$DATA/.load-checkpoint-$COLLECTION.json'
print(len(json.load(open(p))['done']) if os.path.exists(p) else 0)")
    if [ "$done_n" -ge "$total_shards" ]; then
        echo "all $total_shards shards imported"
        break
    fi

    jsonl="$WORK/shard.jsonl"
    rm -f "$jsonl"

    # Embed exactly one shard; the checkpoint makes each run take the next one.
    if ! load -dir "$DATA" -out "$jsonl" -collection "$COLLECTION" -shards 1 2>&1 | tail -2; then
        echo "emit failed on shard $((done_n + 1))"
        exit 1
    fi

    # Compact only on the last shard (see the note above).
    compact=false
    if [ "$((done_n + 1))" -ge "$total_shards" ]; then
        compact=true
        echo "final shard: importing with compaction"
    fi

    if ! polign-import -stores "$STORE" -collection "$COLLECTION" \
        -expected-size "$expected" -compact="$compact" "$jsonl" 2>&1 | tail -3; then
        echo "import failed on shard $((done_n + 1)) — the checkpoint counts it as emitted, so"
        echo "re-run after removing that entry from $DATA/.load-checkpoint-$COLLECTION.json"
        exit 1
    fi
    rm -f "$jsonl"
done

echo "index build complete"
