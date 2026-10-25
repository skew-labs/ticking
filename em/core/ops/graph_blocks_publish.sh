#!/bin/bash
# graph_blocks_publish.sh (VM, as root): for a local graph-only build, wait for GRAPH_OK, add blk.time (ledger blocks mode,
# same build_id), then merge -> verify -> publish (finish_build.sh).
#   graph_blocks_publish.sh <chain> <build_id> <max_block>
set -euo pipefail
ch=$1; id=$2; maxb=$3; d=/data/emv2-out/$ch/$id
while [ ! -f $d/GRAPH_OK ]; do sleep 30; done
/data/emv2-dev/src-new/build/emv2-build-ledger blocks --chain $ch --blocks /data/emv2-in/$ch/blocks --out $d --build-id $id --max-block $maxb 2>&1 | tail -2
bash /data/emv2-dev/ops/finish_build.sh $d
