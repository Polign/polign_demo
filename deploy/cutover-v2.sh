#!/bin/bash
# Cut demowiki2.polign.com from the "building" placeholder to the live demo.
#
# Ordering matters here. The app is started and proven against the node BEFORE
# Caddy is pointed at it, so a broken deploy leaves the placeholder up rather
# than replacing it with a 502. Prewarming happens before the switch too: the
# first query touching a region of the corpus pays an S3 round trip per probed
# cell, and the front page's own examples should not be the queries that
# discover that.
#
#   sudo ./cutover-v2.sh
set -eu

CADDYFILE=${CADDYFILE:-/etc/caddy.Caddyfile}
APP=http://127.0.0.1:23100

echo "== 1. dependencies"
systemctl is-active --quiet polign-embedserve || { echo "embedserve not running"; exit 1; }
curl -sf -m 10 http://127.0.0.1:23200/healthz >/dev/null || { echo "sidecar not answering"; exit 1; }

# Restart the node rather than trusting whatever manifest it is holding. A
# searcher built during the corpus load pins that generation until its next
# refresh, and the build's final compaction rewrites every cell and GCs the
# blobs the old manifest points at — so a long-lived searcher can answer with
# "object not found" until it refreshes. Restarting makes the cutover read the
# published generation from a clean slate instead of racing that window.
systemctl restart polign-node-v2
for i in $(seq 1 30); do
    curl -sf -m 5 http://127.0.0.1:23000/healthz >/dev/null 2>&1 && break
    sleep 2
done
systemctl is-active --quiet polign-node-v2 || { echo "node did not come back"; exit 1; }

echo "== 2. start the app"
systemctl enable --now polign-demo-v2
for i in $(seq 1 30); do
    curl -sf -m 5 "$APP/healthz" >/dev/null 2>&1 && break
    sleep 2
done
curl -sf -m 5 "$APP/healthz" >/dev/null || {
    echo "app did not come up; leaving the placeholder in place"
    journalctl -u polign-demo-v2 -n 20 --no-pager
    exit 1
}

echo "== 3. prove a real query returns hits"
probe=$(curl -sf -m 120 "$APP/demo/search?q=who+invented+the+telephone&mode=semantic" || true)
count=$(printf '%s' "$probe" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("results",[])))' 2>/dev/null || echo 0)
if [ "${count:-0}" -lt 1 ]; then
    echo "query returned no hits; leaving the placeholder in place"
    printf '%s\n' "$probe" | head -5
    exit 1
fi
echo "   $count hits"

echo "== 4. prewarm the front-page examples"
python3 - <<'EOF' || echo "   (prewarm incomplete — the first visitor pays the cold read)"
import json, urllib.parse, urllib.request
meta = json.load(urllib.request.urlopen("http://127.0.0.1:23100/demo/meta", timeout=30))
for q in meta.get("examples", []):
    u = "http://127.0.0.1:23100/demo/search?q=" + urllib.parse.quote(q) + "&mode=semantic"
    try:
        r = json.load(urllib.request.urlopen(u, timeout=180))
        print(f"   {r['took_ms']:7.0f} ms  {q}")
    except Exception as exc:
        print(f"   failed: {q}: {exc}")
EOF

echo "== 5. point Caddy at the app"
cp "$CADDYFILE" "$CADDYFILE.placeholder.bak"
install -m 0644 "$(dirname "$0")/Caddyfile-v2" "$CADDYFILE"
if ! /usr/local/bin/caddy validate --config "$CADDYFILE" >/dev/null 2>&1; then
    echo "Caddyfile invalid; restoring the placeholder"
    cp "$CADDYFILE.placeholder.bak" "$CADDYFILE"
    exit 1
fi
systemctl reload caddy || systemctl restart caddy

echo "== done: https://demowiki2.polign.com is live"
