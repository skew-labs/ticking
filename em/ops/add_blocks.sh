#!/bin/bash
# add_blocks.sh (VM, as root): add blk.time to a published graph-only build (core request 2026-10-20 17:24 UTC).
#   add_blocks.sh <chain> <build_id> <blocks_dir> <max_block>
# Rebuilds manifest.graph.json from the published manifest, runs `emv2-build-ledger blocks`, merges without re-hashing the
# graph files (they are already verified on Blob), then publishes with --augment (only blk.time is new; the previous
# manifest is kept as manifest.prev-<UTC>.json).
set -euo pipefail
ch=$1; id=$2; blk=$3; maxb=$4
NEW=/data/emv2-dev/src-new/build; OPS=/data/emv2-dev/ops
d=/data/emv2-addblk/$ch/$id; rm -rf $d; mkdir -p $d
S=$(cat /root/.em-blob.sas); S=${S#\?}
curl -sf "https://arbitrum.blob.core.windows.net/em-mapping/$ch/emv2/$id/manifest.json?$S" -o $d/manifest.json
python3 - $d <<'PY'
import json, sys
d = sys.argv[1]; m = json.load(open(d + "/manifest.json"))
assert list(m["modules"]) == ["graph"], f"expected a graph-only build, got modules {list(m['modules'])}"
part = {"schema": m["schema"], "format_version": m["format_version"], "chain": m["chain"], "module": "graph", "build_id": m["build_id"],
        "inputs": m["inputs"], "files": m["files"], "facts": m["modules"]["graph"]}
json.dump(part, open(d + "/manifest.graph.json", "w"), indent=1)
PY
rm -f $d/manifest.json
$NEW/emv2-build-ledger blocks --chain $ch --blocks $blk --out $d --build-id $id --max-block $maxb 2>&1 | tail -2
$NEW/emv2-manifest merge $d --no-verify
python3 $OPS/emv2_publish.py $d --augment
