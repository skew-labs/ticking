#!/bin/bash
# rebuild.sh [chain ...]: rebuild the deep store (DuckDB) + wallet index from the stage, low priority.
# The live tail is paused for the duration (it shares tail_state and the stage); the hot SQLite store keeps
# serving the newest minutes and catches up after the restart. Logs: logs/rebuild.log, logs/build_db_<chain>.log
cd /srv/skew/em-charts || exit 1
CHAINS=${*:-robinhood arbitrum}
PY="nice -n 19 ionice -c3 /srv/skew/em-galaxy/.venv/bin/python"
touch data/LIVE_PAUSED
trap 'rm -f data/LIVE_PAUSED' EXIT
for ch in $CHAINS; do
  echo "$(date -u +%T) $ch stage"
  $PY build_bars.py $ch recent >> logs/rebuild.log 2>&1 || { echo "$ch recent trades FAILED"; exit 1; }
  echo "$(date -u +%T) $ch deep"
  $PY build_db.py $ch > logs/build_db_$ch.log 2>&1 || { echo "$ch build_db FAILED"; tail -n 20 logs/build_db_$ch.log; exit 1; }
  $PY qa.py data/$ch/candles.duckdb --top 10 > logs/qa_$ch.json 2>&1
  echo "$(date -u +%T) $ch wallet index"
  $PY build_wallet_index.py $ch > logs/wallet_$ch.log 2>&1 || { echo "$ch wallet FAILED"; tail -n 20 logs/wallet_$ch.log; exit 1; }
done
rm -f data/LIVE_PAUSED
systemctl restart em-map-charts
echo "$(date -u +%T) done"
