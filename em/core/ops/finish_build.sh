#!/bin/bash
# finish_build.sh (VM, as root): merge -> verify -> publish one build dir (used for graph-only builds of the big chains in phase 1:
# ethereum, base, polygon; their ledger counts/focus are verify evidence only and never go into the build dir).
#   finish_build.sh <build_dir>
set -euo pipefail
d=$1; MAN=/data/emv2-dev/opsbuild/build/emv2-manifest
[ -f "$d/GRAPH_OK" ] || { echo "no GRAPH_OK in $d" >&2; exit 1; }
[ ! -e "$d/manifest.ledger.json" ] || echo "note: $d has a ledger part; it will be merged too"
"$MAN" merge "$d" | tee -a "$d/chain.log"
"$MAN" verify "$d" | tee -a "$d/chain.log"
chown -R azureuser:azureuser "$d"; touch "$d/BUILD_OK"
python3 /data/emv2-dev/ops/emv2_publish.py "$d" 2>&1 | tee -a "$d/chain.log"
echo "$(date -u +%T) PUBLISHED $d" | tee -a "$d/chain.log"
