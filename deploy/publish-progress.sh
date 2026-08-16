#!/bin/bash
# Publish how much of the corpus is actually loaded, so the live demo can say so.
#
# A cold-first collection is searchable from its first published generation, not
# from its last — which is the point, but it means the UI would otherwise claim
# the full corpus size while a fraction of it is queryable. This writes the true
# loaded count to the bucket; the serving host folds it into corpus.json (see
# refresh-manifest.sh) and the app picks it up without a restart.
#
# Runs on the BUILD host, alongside build-index-v2.sh.
#
#   BUCKET=polign-demo-wiki-en ./publish-progress.sh &
set -u

BUCKET=${BUCKET:-polign-demo-wiki-en}
LOGS=${LOGS:-$HOME/logs}
INTERVAL=${INTERVAL:-300}
export AWS_REGION=${AWS_REGION:-us-east-1}

while true; do
    # Sum the importer's per-run totals, not its per-flush progress lines. The
    # progress lines report a running total *within* one import, so a shard
    # larger than -batch logs several of them ("imported 400000", then
    # "imported 401378") and adding those up counts the same vectors twice —
    # which inflated this number by ~6% before it was caught.
    loaded=$(grep -oE "done: [0-9]+ vectors into" "$LOGS/import.log" 2>/dev/null |
             awk '{s+=$2} END {print s+0}')
    printf '{"loaded":%s}\n' "${loaded:-0}" > /tmp/progress.json
    aws s3 cp /tmp/progress.json "s3://$BUCKET/app/progress.json" --only-show-errors

    # Stop once the build says it is done, so the file settles on a final count.
    if grep -q "build complete" "$LOGS/build.log" 2>/dev/null; then
        echo "build complete at $loaded loaded; progress publisher exiting"
        exit 0
    fi
    sleep "$INTERVAL"
done
