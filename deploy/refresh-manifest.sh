#!/bin/bash
# Fold the build's published progress into the UI's corpus.json.
#
# Runs on the SERVING host on a timer. The app re-reads corpus.json whenever its
# mtime changes, so this needs no restart and never interrupts a search in
# flight. Writes are atomic (temp + mv) for the same reason.
#
# While the build runs, `passages` is the loaded count rather than the corpus
# total: a visitor searching a 20%-loaded index should be told the index is 20%
# loaded, not shown 12.5M and left to read a miss as an absence.
set -u

BUCKET=${BUCKET:-polign-demo-wiki-en}
DATA=${DATA:-/opt/polign/data}
export AWS_REGION=${AWS_REGION:-us-east-1}

tmp=$(mktemp)
trap 'rm -f "$tmp" "$tmp.json"' EXIT

if ! aws s3 cp "s3://$BUCKET/app/progress.json" "$tmp" --only-show-errors 2>/dev/null; then
    exit 0    # no progress published (build finished or not started): leave as-is
fi

python3 - "$tmp" "$DATA/corpus.json" "$tmp.json" <<'EOF'
import json, sys, os
progress, manifest_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
loaded = json.load(open(progress)).get("loaded", 0)
m = json.load(open(manifest_path))
# total_passages is the corpus's real size, preserved across refreshes so the
# final update can restore it when the build finishes.
total = m.get("total_passages", m.get("passages", 0))
m["total_passages"] = total
m["passages"] = loaded if 0 < loaded < total else total
json.dump(m, open(out, "w"), indent=2)
print(f"{m['passages']:,} of {total:,} passages loaded")
EOF

[ -s "$tmp.json" ] || exit 1
mv "$tmp.json" "$DATA/corpus.json"
