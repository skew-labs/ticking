#!/usr/bin/env bash
# feature_pack.sh <chain> <out_dir> <map_dir> [N=1000]
# Emits LLM feature packs for the top N addresses of a map built by build_map.sh:
#   <map_dir>/feature_packs/packs_top<N>.parquet and packs_top<N>.jsonl
# env: DUCKDB THREADS MEM TMP (same defaults as build_map.sh)
set -euo pipefail
[ $# -ge 3 ] || { echo "usage: $0 <chain> <out_dir> <map_dir> [N]" >&2; exit 64; }
CHAIN=$1; MAP=$(cd "$3" && pwd); N=${4:-1000}
HERE=$(cd "$(dirname "$0")" && pwd)
DUCKDB=${DUCKDB:-/data/mapping/bin/duckdb}
THREADS=${THREADS:-6}; MEM=${MEM:-30GB}; TMPROOT=${TMP:-/data/mapping/tmp}; TMPMAX=${TMPMAX:-1500GB}
section() { awk -v s="$2" '/^-- @section /{cur=$3; next} cur==s' "$1"; }
DTMP=$TMPROOT/duck_fp_$$; mkdir -p "$DTMP" "$MAP/feature_packs"
lists=(); while read -r r; do lists+=("$MAP/stage/plan/r$r.transactions.list"); done < "$MAP/ranges_included.txt"
f=$MAP/stage/tmp/feature_pack.sql
{ section "$HERE/build_map.sql" settings; section "$HERE/build_map.sql" macros
  echo "CREATE OR REPLACE VIEW src_tx AS SELECT * FROM read_parquet([$(sed "s/.*/'&'/" "${lists[@]}" | paste -sd, -)]);"
  if compgen -G "$MAP/dex_pools/*.parquet" > /dev/null; then
    echo "CREATE OR REPLACE VIEW dex_pools_v AS SELECT pool, venue FROM read_parquet('$MAP/dex_pools/*.parquet');"
    echo "CREATE OR REPLACE VIEW dex_routers_v AS SELECT DISTINCT router FROM read_parquet('$MAP/dex_trades/*/*.parquet', hive_partitioning = false) WHERE router IS NOT NULL;"
  else
    echo "CREATE OR REPLACE VIEW dex_pools_v AS SELECT NULL::BLOB AS pool, NULL::BLOB AS venue WHERE false;"
    echo "CREATE OR REPLACE VIEW dex_routers_v AS SELECT NULL::BLOB AS router WHERE false;"
  fi
  section "$HERE/feature_pack.sql" feature_pack
} | sed -e "s|@THREADS@|$THREADS|g" -e "s|@MEM@|$MEM|g" -e "s|@TMP@|$DTMP|g" -e "s|@TMPMAX@|$TMPMAX|g" \
        -e "s|@BUCKETS@|32|g" -e "s|@MAP@|$MAP|g" -e "s|@CHAIN@|$CHAIN|g" -e "s|@N@|$N|g" > "$f"
t0=$(date +%s%N)
"$DUCKDB" -bail -batch < "$f"
"$DUCKDB" -noheader -list -c "SELECT pack FROM read_parquet('$MAP/feature_packs/packs_top$N.parquet') ORDER BY score DESC" \
  > "$MAP/feature_packs/packs_top$N.jsonl"
rm -rf "$DTMP"
echo "feature packs: $MAP/feature_packs/packs_top$N.jsonl ($(wc -l < "$MAP/feature_packs/packs_top$N.jsonl") lines) in $(awk -v a="$t0" -v b="$(date +%s%N)" 'BEGIN{printf "%.1f", (b-a)/1e9}') s"
