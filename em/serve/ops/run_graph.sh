#!/bin/bash
# run_graph.sh (VM, as azureuser; called through systemd-run by ops): graph stage of one EMV2 chain build.
#   run_graph.sh <chain> <build_id> <map_dir> [mem_budget_gb] [out_root]
# check-sorted -> emv2-build-graph -> emv2-graph-tool check -> counts. Writes <out>/graph.log and <out>/GRAPH_OK on success.
# EMV2_SIMD in the environment is passed through (determinism builds).
set -euo pipefail
ch=$1; id=$2; map=$3; mem=${4:-16}; root=${5:-/data/emv2-out}
BIN=${GRAPH_BIN:-/data/emv2-dev/graph/cm/build}
declare -A CID=([arbitrum]=42161 [robinhood]=4663 [arc]=5042 [ethereum]=1 [base]=8453 [optimism]=10 [linea]=59144 [polygon]=137 [unichain]=130)
out=$root/$ch/$id; tmp=/data/emv2-tmp/$ch-graph-$id-$$
mkdir -p "$out" "$tmp"
exec > >(tee -a "$out/graph.log") 2>&1
echo "$(date -u +%T) graph $ch $id map=$map mem=${mem}G simd=${EMV2_SIMD:-auto}"
sort_flag=()
set +e; "$BIN/emv2-build-graph" --check-sorted --map "$map" --threads 8; rc=$?; set -e
if [ $rc -eq 3 ]; then
  echo "$(date -u +%T) unsorted parts: building with --sort-parts"; sort_flag=(--sort-parts)
elif [ $rc -ne 0 ]; then
  echo "check-sorted failed rc=$rc"; exit $rc
fi
"$BIN/emv2-build-graph" --chain "$ch" --chain-id "${CID[$ch]}" --map "$map" --out "$out" --tmp "$tmp" --threads 14 \
  --mem-budget "$mem" --build-id "$id" "${sort_flag[@]}" > "$out/graph.facts.json"
echo "$(date -u +%T) built: $(head -c 400 "$out/graph.facts.json")"
"$BIN/emv2-graph-tool" check "$out" --threads 14
"$BIN/emv2-graph-tool" dump "$out" counts > "$out/graph.counts.json"
echo "$(date -u +%T) check ok; counts $(head -c 600 "$out/graph.counts.json")"
rm -rf "$tmp"
touch "$out/GRAPH_OK"
echo "$(date -u +%T) GRAPH_OK $ch $id $(du -sb "$out" | cut -f1) bytes"
