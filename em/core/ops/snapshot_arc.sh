#!/bin/bash
# snapshot_arc.sh (VM, as root): freeze Blob realtime/arc (em-extract layout: manifests + 5 tables) into one VM directory so the
# arc map, graph and ledger are all built from the same bytes (the collector rewrites chunk files in place). Then hash every file.
#   snapshot_arc.sh  -> prints the snapshot dir; writes <snap>/INPUTS.json ([{name: local:<path>, sha256, bytes}]) and <snap>/FROZEN
set -euo pipefail
S=$(cat /root/.em-blob.sas); S=${S#\?}
B=https://arbitrum.blob.core.windows.net/em-mapping
snap=/data/emv2-in/arc/snap-$(date -u +%Y%m%dT%H%MZ)
mkdir -p "$snap"
export AZCOPY_BUFFER_GB=1 HOME=/root AZCOPY_LOG_LOCATION=/data/emv2-dev/azcopy-logs
for t in manifests blocks transactions logs token_transfers contract_creations; do
  azcopy copy "$B/realtime/arc/$t?$S" "$snap/" --recursive --exclude-pattern '*.tmp*' --cap-mbps 2000 --check-md5 FailIfDifferent \
    --log-level ERROR --output-level essential >/dev/null
  echo "$(date -u +%T) $t $(find "$snap/$t" -type f | wc -l) files $(du -sb "$snap/$t" | cut -f1) bytes"
done
python3 /data/emv2-dev/ops/emv2_inputs.py "$snap/INPUTS.json" "$snap" | tail -1
chmod -R a-w "$snap"/manifests "$snap"/blocks "$snap"/transactions "$snap"/logs "$snap"/token_transfers "$snap"/contract_creations
touch "$snap/FROZEN"
echo "SNAPSHOT $snap"
