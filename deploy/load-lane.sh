#!/bin/bash
# One parallel loading lane: its own node, its own dataset view.
#
# The in-memory HNSW insert that dominates load time is single-writer per node,
# so throughput scales by running several nodes against the same object store.
# They all append to the shared write log (which is built for a fleet); only
# lane 0 runs the persistor, the rest are pure writers.
#
#   ./drive-parallel.sh <lane> <http-port> <grpc-port> <persist true|false>
set -u

LANE=$1
HTTP_PORT=$2
GRPC_PORT=$3
PERSIST=$4

DATA=${DATA:-$HOME/data}-p$LANE
LOGS=${LOGS:-$HOME/logs}
STORE=${STORE:?set STORE, e.g. s3://your-bucket/polign}
export AWS_REGION=us-east-1

start_node() {
    pkill -f "polign-server .*:$HTTP_PORT" 2>/dev/null
    sleep 2
    GOMEMLIMIT=3GiB setsid nohup polign-server \
        -store "$STORE" \
        -restore-stores "" \
        -persist="$PERSIST" \
        -http "127.0.0.1:$HTTP_PORT" -grpc "127.0.0.1:$GRPC_PORT" \
        -maintain 0 -log-batch-window 25ms \
        >> "$LOGS/node-p$LANE.log" 2>&1 < /dev/null &
    for _ in $(seq 1 90); do
        if curl -sf -m 3 "localhost:$HTTP_PORT/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    echo "lane $LANE: node did not come up"
    return 1
}

total=$(python3 -c "import json;print(len(json.load(open('$DATA/corpus.json'))['shards']))")

for chunk in $(seq 1 60); do
    done_n=$(python3 -c "
import json,os
p='$DATA/.load-checkpoint-wikipedia.json'
print(len(json.load(open(p))['done']) if os.path.exists(p) else 0)")
    if [ "$done_n" -ge "$total" ]; then
        echo "lane $LANE: ALL $total SHARDS LOADED"
        break
    fi
    echo "lane $LANE: chunk $chunk ($done_n/$total shards)"
    start_node || exit 1
    load -dir "$DATA" -addr "http://127.0.0.1:$HTTP_PORT" -collection wikipedia \
         -shards 1 -workers 16 -batch 1000 2>&1 | tail -3
done

# Leave lane 0's node up so its persistor can drain the shared write log.
if [ "$LANE" != "0" ]; then
    pkill -f "polign-server .*:$HTTP_PORT" 2>/dev/null
fi
echo "lane $LANE: done"
