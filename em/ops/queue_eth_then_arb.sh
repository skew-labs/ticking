#!/bin/bash
# core 16:39 UTC: finish the ethereum graph hot, then arbitrum graph hot only (cold kinds stay on Blob for the cold tier).
cd /srv/skew/emv2-dev/ops
python3 emv2_fetch.py ethereum c1ae4fffe3f6ef5d --exclude-kinds g.edge.exact,g.hub.cold,g.node.cold --max-gb 45 --abort-below-gb 70 > /srv/skew/emv2/logs/fetch-eth-hot2.out 2>&1 || exit $?
python3 emv2_fetch.py arbitrum 5bb7feeb57232401 --exclude-kinds g.edge.exact,g.hub.cold,g.node.cold --max-gb 12 --abort-below-gb 70 > /srv/skew/emv2/logs/fetch-arb-hot.out 2>&1
