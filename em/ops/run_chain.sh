#!/bin/bash
# run_chain.sh (VM, as root through systemd-run): one full EMV2 chain build, graph -> ledger -> merge -> verify [-> publish].
#   run_chain.sh <chain> <build_id> <max_block> <parts> [out_root=/data/emv2-out] [publish=yes]
# Uses the staged inputs /data/emv2-in/<chain>/{map,tt,blocks} (arc's map: /data/emv2-dev/graph/in/arc).
# Determinism builds pass another out_root and publish=no; EMV2_SIMD passes through to both builders.
set -euo pipefail
ch=$1; id=$2; maxb=$3; parts=$4; root=${5:-/data/emv2-out}; pub=${6:-yes}
OPS=/data/emv2-dev/ops
MAN=/data/emv2-dev/opsbuild/build/emv2-manifest
LEDGER=${LEDGER_BIN:-/data/emv2-dev/ledger/build/emv2-build-ledger}
GRAPH_LAYOUT=${GRAPH_LAYOUT:-emv2.graph.v2}
map=${MAP_DIR:-/data/emv2-in/$ch/map}; tt=${TT_DIR:-/data/emv2-in/$ch/tt}; blk=${BLK_DIR:-/data/emv2-in/$ch/blocks}
out=$root/$ch/$id
mkdir -p "$out"
log() { echo "$(date -u +%T) [$ch $id] $*" | tee -a "$out/chain.log"; }
log "start simd=${EMV2_SIMD:-auto} max_block=$maxb parts=$parts out=$out publish=$pub map=$map tt=$tt"
t0=$(date +%s)
layout=$(python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print(m.get('facts',{}).get('layout',''))" "$out/manifest.graph.json" 2>/dev/null || true)
if [ -f "$out/GRAPH_OK" ] && [ "$layout" = "$GRAPH_LAYOUT" ]; then
  log "graph already built ($layout), skipping"
else
  # Not a complete current-layout graph: start the build dir from scratch (keep logs with a suffix).
  stamp=$(date -u +%H%M%S)
  (cd "$out" && for f in graph.log graph.facts.json graph.counts.json ledger.log ledger.facts.json; do [ -e $f ] && mv $f $f.prev-$stamp; done
   rm -f ./*.seg ./*.seg.tmp-* manifest.graph.json manifest.ledger.json manifest.json GRAPH_OK BUILD_OK PUBLISHED.json; rm -rf dump; true)
  log "graph layout '${layout:-none}' != $GRAPH_LAYOUT: build dir cleared for a full rebuild"
  bash "$OPS/run_graph.sh" "$ch" "$id" "$map" 16 "$root"
  log "graph done in $(( $(date +%s) - t0 ))s"
fi
t1=$(date +%s)
"$LEDGER" full --chain "$ch" --in "$tt" --blocks "$blk" --out "$out" --build-id "$id" \
  --max-block "$maxb" --parts "$parts" --per-pass "${PER_PASS:-4}" --threads 12 --dump ${EXTRA_DIR:+--extra "$EXTRA_DIR"} ${ALLOW_MISSING:+--allow-missing} > "$out/ledger.facts.json" 2> >(tee -a "$out/ledger.log" >&2)
log "ledger done in $(( $(date +%s) - t1 ))s: $(head -c 300 "$out/ledger.facts.json")"
"$MAN" merge "$out" | tee -a "$out/chain.log"
"$MAN" verify "$out" | tee -a "$out/chain.log"
chown -R azureuser:azureuser "$out"
touch "$out/BUILD_OK"
log "BUILD_OK $(du -sb "$out" | cut -f1) bytes in $(( $(date +%s) - t0 ))s"
if [ "$pub" = yes ]; then
  python3 "$OPS/emv2_publish.py" "$out" 2>&1 | tee -a "$out/chain.log"
  log "PUBLISHED"
fi
