#!/bin/bash
# wait for the linea fetch to finish, then fetch the robinhood graph part only (core request 15:42 UTC)
while pgrep -f "[p]ython3 emv2_fetch.py linea" >/dev/null; do sleep 10; done
cd /srv/skew/emv2-dev/ops && nice -n 10 ionice -c2 -n7 python3 emv2_fetch.py robinhood e265263e29d91ce4 --include-kind-prefixes dict.,blk.,g. --max-gb 14 > /srv/skew/emv2/logs/fetch-rh-graph.out 2>&1
