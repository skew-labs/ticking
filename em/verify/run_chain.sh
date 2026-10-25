#!/bin/bash
# run_chain.sh CHAIN BUILD_DIR [DUMP_DIR] [GRAPH_BIN|-] [EMV2_DUMP_BIN|-]: everything verify checks for one finished chain build (VM, root):
#   compare.py (sample rows, pinned --as-of map end), counts (graph --table counts or count_stream over --all) + compare_counts.py.
# Wrapped in a systemd scope (ops rule: MemoryMax 8G, OOMScoreAdjust 800, nice 10). Logs to results/<chain>/run.log.
set -uo pipefail
CH=$1; B=$2; D=${3:-$2/dump}; GB=${4:--}; ED=${5:--}
cd /data/emv2-dev/verify; mkdir -p results/$CH
END=$(tail -1 golden/$CH/ranges_included.tsv 2>/dev/null | awk -F'\t' '{print $NF}')
RUN="systemd-run --scope -q -p MemoryMax=8G -p OOMScoreAdjust=800 nice -n 10"
{
echo "== $CH build=$B dump=$D as_of=$END $(date -u +%FT%TZ)"
ARGS=(); [ "$GB" != - ] && ARGS+=(--graph-bin "$GB" --image "$B"); [ "$ED" != - ] && ARGS+=(--emv2-dump "$ED" --image "$B")
[ -d "$D" ] && ARGS+=(--dump "$D")
$RUN python3 compare.py $CH "${ARGS[@]}" ${END:+--as-of $END} --out results/$CH
if [ "$GB" != - ]; then
  if $RUN "$GB" dump --image "$B" --table counts > results/$CH/graph_counts.csv 2>/dev/null && [ -s results/$CH/graph_counts.csv ]; then :; else
    $RUN bash count_stream.sh "$GB" "$B" results/$CH/graph_counts.csv; fi
elif [ "$ED" != - ]; then
  $RUN "$ED" "$B" counts --out results/$CH/graph_counts.csv
fi
LC=$(ls $D/counts.* 2>/dev/null | head -1)
[ -s results/$CH/graph_counts.csv ] && python3 compare_counts.py $CH results/$CH/graph_counts.csv $LC
} 2>&1 | tee -a results/$CH/run.log
