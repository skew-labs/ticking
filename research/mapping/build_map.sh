#!/usr/bin/env bash
# build_map.sh <chain> <out_dir> <map_dir>
#
# Builds the wallet-mapping layer (see build_map.sql header) for one chain from em-extract
# Parquet in <out_dir>, into <map_dir>:
#   addresses/part-*.parquet  edges/part-*.parquet  token_stats/token_stats.parquet
#   address_tokens/part-*.parquet  emitter_topics/emitter_topics.parquet  checks.json
#   dex_trades/  dex_pools/  address_dex/        (when DEX=1, see dex_trades.sql)
# Incremental: per-range partials (stage/ranges/rNNN) are rebuilt only when that range's set
# of finished chunks changed (manifest sha256 signature); the merge re-aggregates all partials.
# Safe to run while extraction continues (only chunks with an OK manifest are read).
#
# env: DUCKDB (/data/mapping/bin/duckdb)  THREADS (6)  MEM (30GB)  TMP (/data/mapping/tmp)
#      TMPMAX (1500GB)  RANGE (10000000)  BUCKETS (32, power of two <= 256)
#      MAX_BLOCK (unset = all; e.g. 20000000 builds only blocks < 20M)   MIN_BLOCK (0)
#      PARTIAL (0; 1 = also build ranges that are not complete yet, e.g. the chain head)
#      STEPS ("ranges merge finalize checks")   DEX (1 = also build the dex layer)
#      MAIN_BUDGET (200000000 tx+token-transfer rows) / DEX_BUDGET (150000000 log rows): a 10M range
#      is processed in consecutive chunk groups ("parts") under these budgets to bound memory;
#      parts of a range are written under stage/ranges/rNNN/pK (merge sums them like ranges).
set -euo pipefail

[ $# -eq 3 ] || { echo "usage: $0 <chain> <out_dir> <map_dir>" >&2; exit 64; }
CHAIN=$1; OUT=$(cd "$2" && pwd); MAP=$3
HERE=$(cd "$(dirname "$0")" && pwd)
SQL=$HERE/build_map.sql
DEXSQL=$HERE/dex_trades.sql
DUCKDB=${DUCKDB:-/data/mapping/bin/duckdb}
THREADS=${THREADS:-6}; MEM=${MEM:-30GB}; TMPROOT=${TMP:-/data/mapping/tmp}; TMPMAX=${TMPMAX:-1500GB}
RANGE=${RANGE:-10000000}; BUCKETS=${BUCKETS:-32}; MAX_BLOCK=${MAX_BLOCK:--}; MIN_BLOCK=${MIN_BLOCK:-0}; PARTIAL=${PARTIAL:-0}
STEPS=${STEPS:-ranges merge finalize checks}; DEX=${DEX:-1}
MAIN_BUDGET=${MAIN_BUDGET:-200000000}; DEX_BUDGET=${DEX_BUDGET:-150000000}

mkdir -p "$MAP"/{stage/ranges,stage/tmp,stage/plan,stage/merge,logs} "$TMPROOT"
MAP=$(cd "$MAP" && pwd)
NEXT=$MAP/stage/next
LOG=$MAP/logs/build_$(date -u +%Y%m%dT%H%M%SZ).log
TIMINGS=$MAP/logs/timings.tsv
exec 9>"$MAP/.lock"
flock -n 9 || { echo "another build_map.sh is running on $MAP" >&2; exit 75; }

log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG" >&2; }

# extract one "-- @section <name>" block of a SQL file
section() { awk -v s="$2" '/^-- @section /{cur=$3; next} cur==s' "$1"; }

# substitute placeholders
subst() {
  sed -e "s|@THREADS@|$THREADS|g" -e "s|@MEM@|$MEM|g" -e "s|@TMP@|$DTMP|g" -e "s|@TMPMAX@|$TMPMAX|g" \
      -e "s|@BUCKETS@|$BUCKETS|g" -e "s|@MAP@|$MAP|g" -e "s|@NEXT@|$NEXT|g" -e "s|@CHAIN@|$CHAIN|g" \
      -e "s|@RDIR@|${RDIR:-}|g" -e "s|@B@|${B:-}|g"
}

# list file -> SQL list literal
sqllist() { sed "s/.*/'&'/" "$@" | paste -sd, -; }

# run DuckDB on: settings + macros (+dex macros) + preamble file + section(s)
run_sql() {  # run_sql <step-name> <preamble-file> <sqlfile:section>...
  local name=$1 pre=$2; shift 2
  local f=$MAP/stage/tmp/$name.sql t0 t1
  DTMP=$TMPROOT/duck_${CHAIN}_$$
  mkdir -p "$DTMP"
  {
    section "$SQL" settings; section "$SQL" macros
    [ -f "$DEXSQL" ] && section "$DEXSQL" dex_macros
    cat "$pre"
    for s in "$@"; do section "${s%%:*}" "${s#*:}"; done
  } | subst > "$f"
  t0=$(date +%s%N)
  if ! "$DUCKDB" -bail -batch < "$f" >> "$LOG" 2>&1; then
    log "FAILED step $name (sql: $f, log: $LOG)"; rm -rf "$DTMP"; exit 1
  fi
  t1=$(date +%s%N)
  rm -rf "$DTMP"
  local secs; secs=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", (b-a)/1e9}')
  printf '%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$CHAIN" "$name" "$secs" >> "$TIMINGS"
  log "done $name in $secs s"
}

# view helper: view name -> read_parquet over the given globs/paths, or an empty relation
view_or_empty() {  # view_or_empty <name> <empty-select> <path-or-glob>...
  local name=$1 empty=$2; shift 2
  local items=() p
  for p in "$@"; do compgen -G "$p" > /dev/null && items+=("'$p'"); done
  if [ ${#items[@]} -gt 0 ]; then
    echo "CREATE OR REPLACE VIEW $name AS SELECT * FROM read_parquet([$(IFS=,; echo "${items[*]}")], hive_partitioning = false, union_by_name = true);"
  else
    echo "CREATE OR REPLACE VIEW $name AS $empty;"
  fi
}

PLAN=$MAP/stage/plan
python3 -I "$HERE/plan_chunks.py" "$OUT" "$PLAN" "$RANGE" "$MAX_BLOCK" "$PARTIAL" "$MIN_BLOCK" 2>>"$LOG"
mapfile -t RIDS < <(cut -f1 "$PLAN/ranges.tsv")
log "chain=$CHAIN out=$OUT map=$MAP ranges=${#RIDS[@]} (${RIDS[*]:-none}) buckets=$BUCKETS threads=$THREADS mem=$MEM"
[ ${#RIDS[@]} -gt 0 ] || { log "no complete ranges yet"; exit 0; }

src_views() {  # src_views <key>... (key = rNNN or rNNN.pK) -> preamble defining src_* over those inputs
  local tb v k
  for tb in blocks:src_blocks transactions:src_tx logs:src_logs token_transfers:src_tt contract_creations:src_cc; do
    v=${tb#*:}; tb=${tb%%:*}
    local lists=()
    for k in "$@"; do lists+=("$PLAN/$k.$tb.list"); done
    echo "CREATE OR REPLACE VIEW $v AS SELECT * FROM read_parquet([$(sqllist "${lists[@]}")]);"
  done
}

# split range <rid> into consecutive chunk groups whose weight stays under <budget>;
# weight columns of rNNN.counts.tsv: 1=tx 2=token_transfers 3=logs. Writes rNNN.<tag>K.<table>.list
# and prints the part keys.
make_parts() {  # make_parts <rid> <tag> <budget> <awk weight expr>
  local rid=$1 tag=$2 budget=$3 wexpr=$4 tb
  awk -F'\t' -v b="$budget" "{w=$wexpr; if (n>0 && acc+w>b) {print s\" \"NR-1; s=NR; acc=0; n=0} if (n==0) s=NR; acc+=w; n++} END {if (n>0) print s\" \"NR}" \
      "$PLAN/r$rid.counts.tsv" | {
    local k=0 a z
    while read -r a z; do
      for tb in blocks transactions logs token_transfers contract_creations; do
        sed -n "${a},${z}p" "$PLAN/r$rid.$tb.list" > "$PLAN/r$rid.$tag$k.$tb.list"
      done
      echo "r$rid.$tag$k"; k=$((k + 1))
    done; }
}

# ---------------------------------------------------------------- 1. per-range partials
if [[ " $STEPS " == *" ranges "* ]]; then
  # signature = input chunk signature + code version (stage layout tag + range section text)
  code_main=$( { echo layout-parts-v2; section "$SQL" range; } | sha1sum | cut -c1-12)
  code_dex=$( { echo layout-parts-v2; [ -f "$DEXSQL" ] && section "$DEXSQL" dex_range; } | sha1sum | cut -c1-12)
  while IFS=$'\t' read -r rid nch exp complete sig first last; do
    final=$MAP/stage/ranges/r$rid
    if [ -f "$final/_SIGNATURE" ] && [ "$(cat "$final/_SIGNATURE")" = "$sig:$code_main" ]; then
      log "range r$rid ($first-$last, $nch chunks): up to date"
    else
      mapfile -t parts < <(make_parts "$rid" p "$MAIN_BUDGET" '$1+$2')
      log "range r$rid ($first-$last, $nch/$exp chunks, complete=$complete): building in ${#parts[@]} part(s)"
      tmpr=$MAP/stage/tmp/r$rid
      rm -rf "$tmpr"; mkdir -p "$tmpr"
      for k in "${parts[@]}"; do
        RDIR=$tmpr/${k#*.}; mkdir -p "$RDIR"
        pre=$MAP/stage/tmp/pre_$k.sql
        src_views "$k" > "$pre"
        run_sql "range_$k" "$pre" "$SQL:range"
      done
      echo "$sig:$code_main" > "$tmpr/_SIGNATURE"
      rm -rf "$final.old"; [ -d "$final" ] && mv "$final" "$final.old"
      mv "$tmpr" "$final"; [ -d "$final.old/dex" ] && mv "$final.old/dex" "$final/dex"; rm -rf "$final.old"
    fi
    if [ "$DEX" = 1 ]; then
      if [ -f "$final/dex/_SIGNATURE" ] && [ "$(cat "$final/dex/_SIGNATURE")" = "$sig:$code_dex" ]; then
        log "dex r$rid: up to date"
      else
        mapfile -t parts < <(make_parts "$rid" d "$DEX_BUDGET" '$3')
        log "dex r$rid: building in ${#parts[@]} part(s)"
        tmpd=$final/dex.tmp
        rm -rf "$tmpd"; mkdir -p "$tmpd"
        for k in "${parts[@]}"; do
          RDIR=$tmpd/${k#*.}; mkdir -p "$RDIR"
          pre=$MAP/stage/tmp/pre_$k.sql
          src_views "$k" > "$pre"
          run_sql "dex_range_$k" "$pre" "$DEXSQL:dex_range"
        done
        echo "$sig:$code_dex" > "$tmpd/_SIGNATURE"
        rm -rf "$final/dex"; mv "$tmpd" "$final/dex"
      fi
    fi
    RDIR=
  done < "$PLAN/ranges.tsv"
fi

RDIRS=(); for r in "${RIDS[@]}"; do [ -d "$MAP/stage/ranges/r$r" ] && RDIRS+=("$MAP/stage/ranges/r$r"); done
globs() { local d; for d in "${RDIRS[@]}"; do echo "$d/$1"; done; }

# ---------------------------------------------------------------- 2. merge (per bucket)
if [[ " $STEPS " == *" merge "* ]]; then
  rm -rf "$NEXT" "$MAP/stage/merge"; mkdir -p "$NEXT"/{edges,addresses,address_tokens,token_stats,emitter_topics} "$MAP/stage/merge/holders" "$MAP/stage/merge/rev"
  for ((B = 0; B < BUCKETS; B++)); do
    pre=$MAP/stage/tmp/pre_m$B.sql
    { mapfile -t g < <(globs "p*/edges/bucket=$B/*.parquet")
      view_or_empty stg_edges "SELECT NULL::BLOB AS \"from\", NULL::BLOB AS \"to\", NULL::BLOB AS asset, NULL::UTINYINT AS kind, 0::UBIGINT AS n, 0::DECIMAL(38,0) AS s0, 0::DECIMAL(38,0) AS s1, 0::DECIMAL(38,0) AS s2, 0::DECIMAL(38,0) AS s3, 0::UBIGINT AS fb, 0::UBIGINT AS lb WHERE false" "${g[@]}"
      mapfile -t g < <(globs "p*/flows/bucket=$B/*.parquet")
      view_or_empty stg_flows "SELECT NULL::BLOB AS token, NULL::BLOB AS holder, NULL::UTINYINT AS kind, 0::UBIGINT AS n_in, 0::UBIGINT AS n_out, 0::DECIMAL(38,0) AS i0, 0::DECIMAL(38,0) AS i1, 0::DECIMAL(38,0) AS i2, 0::DECIMAL(38,0) AS i3, 0::DECIMAL(38,0) AS o0, 0::DECIMAL(38,0) AS o1, 0::DECIMAL(38,0) AS o2, 0::DECIMAL(38,0) AS o3, 0::UBIGINT AS fb, 0::UBIGINT AS lb WHERE false" "${g[@]}"
    } > "$pre"
    run_sql "merge_edges_flows_b$B" "$pre" "$SQL:merge_edges" "$SQL:merge_flows"
  done
  if [ "$DEX" = 1 ]; then
    pre=$MAP/stage/tmp/pre_dex.sql
    { mapfile -t g < <(globs "dex/d*/dex_trades.parquet"); view_or_empty stg_dex "SELECT NULL WHERE false" "${g[@]}"
      mapfile -t g < <(globs "dex/d*/dex_pools.parquet"); view_or_empty stg_dex_pools "SELECT NULL WHERE false" "${g[@]}"
      mapfile -t g < <(globs "dex/d*/dex_pairs_seen.parquet"); view_or_empty stg_dex_pairs "SELECT NULL WHERE false" "${g[@]}"
    } > "$pre"
    mkdir -p "$NEXT"/{dex_trades,dex_pools,address_dex}
    run_sql "merge_dex" "$pre" "$DEXSQL:dex_merge"
  fi
  for ((B = 0; B < BUCKETS; B++)); do
    pre=$MAP/stage/tmp/pre_a$B.sql
    { mapfile -t g < <(globs "p*/addr/bucket=$B/*.parquet")
      view_or_empty stg_addr "SELECT NULL WHERE false" "${g[@]}"
      view_or_empty out_edges "SELECT NULL::BLOB AS \"from\", NULL::BLOB AS \"to\" WHERE false" "$NEXT/edges/part-$B.parquet"
      view_or_empty rev_pairs "SELECT NULL::BLOB AS a, NULL::BLOB AS x WHERE false" "$MAP/stage/merge/rev/src=*/bucket=$B/*.parquet"
      if [ "$DEX" = 1 ] && [ -f "$NEXT/address_dex_summary.parquet" ]; then
        echo "CREATE OR REPLACE VIEW dex_addr AS SELECT * FROM read_parquet('$NEXT/address_dex_summary.parquet') WHERE bkt(address) = $B;"
      else
        echo "CREATE OR REPLACE VIEW dex_addr AS SELECT NULL::BLOB AS address, 0::UBIGINT AS dex_trades, 0::UBIGINT AS dex_pools, 0::UBIGINT AS dex_tokens WHERE false;"
      fi
    } > "$pre"
    if ! grep -q "read_parquet" <(head -1 "$pre"); then log "bucket $B: no address partials, skipped"; continue; fi
    run_sql "merge_addresses_b$B" "$pre" "$SQL:merge_addresses"
  done
fi

# ---------------------------------------------------------------- 3. finalize + publish
if [[ " $STEPS " == *" finalize "* ]]; then
  pre=$MAP/stage/tmp/pre_fin.sql
  { mapfile -t g < <(globs "p*/tokens.parquet"); view_or_empty stg_tokens "SELECT NULL WHERE false" "${g[@]}"
    view_or_empty stg_holders "SELECT NULL WHERE false" "$MAP/stage/merge/holders/*.parquet"
    mapfile -t g < <(globs "p*/emitter_topics.parquet"); view_or_empty stg_topics "SELECT NULL WHERE false" "${g[@]}"
  } > "$pre"
  run_sql "finalize" "$pre" "$SQL:finalize"
  rm -f "$NEXT/address_dex_summary.parquet"
  for t in edges addresses address_tokens token_stats emitter_topics dex_trades dex_pools address_dex; do
    [ -d "$NEXT/$t" ] || continue
    rm -rf "$MAP/$t.old"; [ -d "$MAP/$t" ] && mv "$MAP/$t" "$MAP/$t.old"
    mv "$NEXT/$t" "$MAP/$t"; rm -rf "$MAP/$t.old"
  done
  if [ "$DEX" = 1 ] && [ -d "$MAP/dex_trades" ]; then
    echo "-- no inputs" > "$MAP/stage/tmp/pre_dexrep.sql"
    run_sql "dex_report" "$MAP/stage/tmp/pre_dexrep.sql" "$DEXSQL:dex_report"
  fi
  printf '%s\n' "${RIDS[@]}" > "$MAP/ranges_included.txt"
  cp "$PLAN/ranges.tsv" "$MAP/ranges_included.tsv"
  log "published outputs in $MAP"
fi

# ---------------------------------------------------------------- 4. checks
if [[ " $STEPS " == *" checks "* ]]; then
  pre=$MAP/stage/tmp/pre_chk.sql
  { mapfile -t g < <(globs "p*/counts.parquet"); view_or_empty stg_counts "SELECT NULL WHERE false" "${g[@]}"
    echo "CREATE OR REPLACE VIEW map_edges AS SELECT * FROM read_parquet('$MAP/edges/*.parquet');"
    echo "CREATE OR REPLACE VIEW map_addresses AS SELECT * FROM read_parquet('$MAP/addresses/*.parquet');"
    echo "CREATE OR REPLACE VIEW map_tokens AS SELECT * FROM read_parquet('$MAP/token_stats/*.parquet');"
    echo "CREATE OR REPLACE VIEW map_address_tokens AS SELECT * FROM read_parquet('$MAP/address_tokens/*.parquet');"
    src_views "${RIDS[@]/#/r}"
  } > "$pre"
  run_sql "checks" "$pre" "$SQL:checks"
  if grep -Eq '"ok": ?false' "$MAP/checks.json"; then log "CHECKS FAILED: see $MAP/checks.json"; exit 3; fi
  log "all checks passed ($MAP/checks.json)"
fi
