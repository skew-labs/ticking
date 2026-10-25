#!/usr/bin/env bash
# Crash-recovery test: commits acknowledged before a kill -9 must all be present after restart.
#   tests/crash_test.sh <data_dir> <port> [seconds_before_kill=10]
set -euo pipefail
DATA=$1; PORT=$2; KILL_AFTER=${3:-10}
B=$(cd "$(dirname "$0")/.." && pwd)/build
ST=/data/graphloop/state/crash; LOG=/data/graphloop/logs; REC=$LOG/crash_acked.jsonl
rm -rf "$ST" "$REC"
start() { taskset -c 0-7 "$B/glserve" --data "$DATA" --state "$ST" --port "$PORT" --workers 4 --cpus 0,2,4,6 --resolve 0.99 --llm-quantile 0 --confirm-quantile 0 ${EXTRA:-} >>"$LOG/crash_srv.out" 2>>"$LOG/crash_srv.err" & echo $!; }
wait_up() { for i in $(seq 1 120); do curl -s "localhost:$PORT/health" >/dev/null 2>&1 && return 0; sleep 0.5; done; return 1; }
PID=$(start); wait_up
taskset -c 8-15 "$B/glbench" --port "$PORT" --mode propose --clients 16 --batch 15 --seconds $((KILL_AFTER + 30)) --record "$REC" --cpus 8-15 > "$LOG/crash_bench.json" 2>"$LOG/crash_bench.err" &
BPID=$!
sleep "$KILL_AFTER"
kill -9 "$PID"; echo "killed glserve pid=$PID after ${KILL_AFTER}s"
wait "$BPID" || true
ACKED=$(wc -l < "$REC")
T0=$(date +%s%N)
PID=$(start); wait_up
T1=$(date +%s%N)
echo "restart_ms=$(( (T1 - T0) / 1000000 )) acked_lines=$ACKED"
grep -E '"event":"(ready|wal_torn_tail_truncated)"' "$LOG/crash_srv.err" | tail -3
"$B/glbench" --port "$PORT" --mode verify --record "$REC"
curl -s "localhost:$PORT/status" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(json.dumps({"commits_after_restart": s["commits"]["total"], "agent_labels": s["agent_labels"], "wal": s["wal"]}))'
kill "$PID"; wait "$PID" 2>/dev/null || true
