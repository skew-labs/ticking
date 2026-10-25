#!/bin/bash
# count_stream.sh GRAPH_BIN IMAGE OUT.csv: whole-chain totals recomputed by DuckDB from `GRAPH_BIN dump --image IMAGE --all --table edges|nodes`,
# independent of the graph tool's own counters. Output: key,value CSV for compare_counts.py. Runs on the VM (streams, no big temp files).
set -euo pipefail
B=$1; I=$2; O=$3; T=/data/emv2-dev/verify/tmp; mkdir -p "$T"
E=$T/edges.$$.fifo; N=$T/nodes.$$.fifo; mkfifo "$E" "$N"; trap 'rm -f "$E" "$N"' EXIT
"$B" dump --image "$I" --all --table edges > "$E" &
nice -n 10 duckdb -csv -noheader -c "SET threads=8; SET memory_limit='8GB'; SET temp_directory='$T';
CREATE TEMP TABLE k AS SELECT kind, count(*) AS rows, sum(n::UBIGINT) AS n,
  sum(CASE WHEN kind = 'native_tx' THEN nullif(total_amount, '')::HUGEINT END) AS amt
  FROM read_csv('$E', header=true, all_varchar=true) GROUP BY kind;
SELECT 'edges_n_' || kind, n::VARCHAR FROM k
UNION ALL SELECT 'edges_rows', sum(rows)::VARCHAR FROM k
UNION ALL SELECT 'native_total_amount', max(amt)::VARCHAR FROM k;" > "$O.tmp"
wait
"$B" dump --image "$I" --all --table nodes > "$N" &
nice -n 10 duckdb -csv -noheader -c "SET threads=8; SET memory_limit='8GB'; SET temp_directory='$T';
CREATE TEMP TABLE s AS SELECT count(*) AS nodes, sum(tx_sent::UBIGINT) AS a, sum(tx_received::UBIGINT) AS b,
  sum(token_transfers_in::UBIGINT) AS c, sum(token_transfers_out::UBIGINT) AS d, count(*) FILTER (WHERE lower(is_contract) IN ('true', '1')) AS e
  FROM read_csv('$N', header=true, all_varchar=true);
SELECT 'nodes', nodes::VARCHAR FROM s UNION ALL SELECT 'sum_tx_sent', a::VARCHAR FROM s UNION ALL SELECT 'sum_tx_received', b::VARCHAR FROM s
UNION ALL SELECT 'sum_token_transfers_in', c::VARCHAR FROM s UNION ALL SELECT 'sum_token_transfers_out', d::VARCHAR FROM s
UNION ALL SELECT 'n_is_contract', e::VARCHAR FROM s;" >> "$O.tmp"
wait
{ echo "key,value"; cat "$O.tmp"; } > "$O"; rm -f "$O.tmp"
cat "$O"
