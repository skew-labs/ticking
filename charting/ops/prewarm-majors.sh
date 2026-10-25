#!/bin/bash
# Prewarm liquid majors for workspace open (ms-class warm hits)
set -euo pipefail
H=http://127.0.0.1
for u in \
  "$H:8895/api/public/engine/candles?network=robinhood&mint=0x0bd7d308f8e1639fab988df18a8011f41eacad73&interval=1h&limit=200&aggregation=token" \
  "$H:8895/api/public/engine/candles?network=robinhood&mint=0x0bd7d308f8e1639fab988df18a8011f41eacad73&interval=1s&limit=120&aggregation=token" \
  "$H:19428/api/public/engine/candles?market=BTC&aggregation=exchange&interval=1h&limit=200" \
  "$H:19428/api/public/engine/candles?market=BTC&aggregation=exchange&interval=1m&limit=300" \
  "$H:19428/api/public/engine/candles?mint=So11111111111111111111111111111111111111112&aggregation=token&interval=1h&limit=200" \
  "$H:19428/api/public/engine/candles?mint=So11111111111111111111111111111111111111112&aggregation=token&interval=1m&limit=300"
do curl -sS -m 20 -o /dev/null "$u" || true
done
