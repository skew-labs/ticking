#!/bin/bash
# graph_check.sh (VM, as root through systemd-run): finish the check + counts steps of a graph build whose check was killed
# (emv2-graph-tool check needs much more memory than the build on big chains). Touches GRAPH_OK on success.
#   graph_check.sh <build_dir> [threads=8]
set -euo pipefail
out=$1; th=${2:-8}; BIN=${GRAPH_BIN:-/data/emv2-dev/graph/cm/build}
[ -f "$out/manifest.graph.json" ] || { echo "no manifest.graph.json in $out" >&2; exit 1; }
"$BIN/emv2-graph-tool" check "$out" --threads "$th" | tee -a "$out/graph.log"
"$BIN/emv2-graph-tool" dump "$out" counts > "$out/graph.counts.json"
touch "$out/GRAPH_OK"
echo "$(date -u +%T) GRAPH_OK (check rerun) $(du -sb "$out" | cut -f1) bytes" | tee -a "$out/graph.log"
