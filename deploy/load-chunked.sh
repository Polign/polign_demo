#!/bin/bash
# Chunked load driver for the build host.
#
# A polign_db node applies every write to its in-memory index as well as to the
# durable write log. For a corpus this size that costs both ways:
#
#   memory — 12.5M passages in one long-lived process needs ~25 GB of RAM.
#   time   — HNSW insertion slows as the graph grows. Measured: ~1,700
#            passages/s into an empty index, ~400/s once it holds ~1M.
#
# None of that work survives here: the serving node reads segments from object
# storage and never loads this index. The write log is the source of truth, so
# the load runs one shard at a time, restarting the node between shards with
# -restore-stores "". Each restart begins with an empty in-memory index — so
# insertion stays at its fast rate — while the persistor keeps consuming the
# log and writing the segments that actually get served. The loader's
# checkpoint makes each run resume exactly where the last one stopped.
#
# Raising SHARDS_PER_CHUNK trades that speed back for fewer restarts.
#
#   STORE=s3://my-bucket/polign ./load-chunked.sh
set -u

SHARDS_PER_CHUNK=${SHARDS_PER_CHUNK:-1}
DATA=${DATA:-$HOME/data}
LOGS=${LOGS:-$HOME/logs}
STORE=${STORE:?set STORE, e.g. s3://your-bucket/polign}
export AWS_REGION=us-east-1

start_node() {
    pkill -x polign-server 2>/dev/null
    sleep 3
    GOMEMLIMIT=11GiB setsid nohup polign-server \
        -store "$STORE" \
        -restore-stores "" \
        -http 127.0.0.1:23000 -grpc 127.0.0.1:23001 \
        -maintain 0 -log-batch-window 25ms \
        >> "$LOGS/node-chunks.log" 2>&1 < /dev/null &
    for _ in $(seq 1 60); do
        if curl -sf -m 3 localhost:23000/healthz >/dev/null 2>&1; then
            echo "  node up"
            return 0
        fi
        sleep 2
    done
    echo "  node did not come up in 120s"
    return 1
}

total_shards=$(python3 -c "import json;print(len(json.load(open('$DATA/corpus.json'))['shards']))")

for chunk in $(seq 1 20); do
    done_shards=$(python3 -c "
import json,os
p='$DATA/.load-checkpoint-wikipedia.json'
print(len(json.load(open(p))['done']) if os.path.exists(p) else 0)")
    echo "=== chunk $chunk: $done_shards/$total_shards shards loaded ==="
    if [ "$done_shards" -ge "$total_shards" ]; then
        echo "ALL SHARDS LOADED"
        break
    fi

    start_node || exit 1
    load -dir "$DATA" -addr http://127.0.0.1:23000 -collection wikipedia \
         -shards "$SHARDS_PER_CHUNK" -workers 24 -batch 1000 2>&1 | tail -8
    rss=$(ps -o rss= -C polign-server 2>/dev/null | awk '{printf "%.0f", $1/1024}')
    echo "  node RSS at end of chunk: ${rss:-?} MiB"
done

# Final pass: leave a node running with no in-memory index so the persistor can
# drain whatever write log tail is left before the maintenance pass.
echo "=== draining write log ==="
start_node
