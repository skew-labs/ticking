#!/bin/bash
# rebuild_arc.sh (VM, as root through systemd-run): core decision 11:42 (b): arc map, graph and ledger from one frozen snapshot.
#   rebuild_arc.sh <snapshot_dir>
# 1) build_map.sh over the snapshot (blocks 0..26,859,999, the old map's range) -> /data/emv2-in/arc/map-<snap>
# 2) all 19 checks must pass  3) build_id from the snapshot's file hashes  4) run_chain.sh with the new map + snapshot tt/blocks.
set -euo pipefail
snap=$1; [ -f "$snap/FROZEN" ] || { echo "snapshot not frozen: $snap" >&2; exit 1; }
name=$(basename "$snap"); map=/data/emv2-in/arc/map-$name; tmp=/data/emv2-tmp/arc-map-$name
mkdir -p "$map" "$tmp"
echo "$(date -u +%T) build_map arc snap=$snap map=$map"
DUCKDB=/usr/local/bin/duckdb THREADS=${THREADS:-8} MEM=${MEM:-20GB} TMP=$tmp TMPMAX=200GB MAX_BLOCK=26860000 PARTIAL=1 \
  bash /data/emv2-dev/ops/mapping/build_map.sh arc "$snap" "$map"
python3 - "$map/checks.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
bad = [x["check_name"] for x in c if not x.get("ok")]
print(f"checks: {len(c)} total, {len(c) - len(bad)} ok", "| FAILED: " + "; ".join(bad) if bad else "| ALL OK")
sys.exit(1 if bad or len(c) < 19 else 0)
PY
id=$(/data/emv2-dev/opsbuild/build/emv2-manifest build-id arc "$snap/INPUTS.json")
echo "arc $id" > /data/emv2-dev/ops/ids/arc.build_id
echo "$(date -u +%T) arc build_id $id (snapshot hashes)"
MAP_DIR=$map TT_DIR=$snap/token_transfers BLK_DIR=$snap/blocks bash /data/emv2-dev/ops/run_chain.sh arc "$id" 26859999 4
