#!/bin/bash
# final_sync.sh (VM, as root): before the VM is deallocated (its /data NVMe is ephemeral), copy what exists only on this VM to Blob
# and verify sizes: the frozen arc snapshot and its rebuilt map, WETH9 extra outputs, and the EMV2 build records (logs, facts,
# manifests, inputs lists; segments are already published content-addressed). Prints a per-prefix check line.
set -euo pipefail
FAIL=0
S=$(cat /root/.em-blob.sas); S=${S#\?}; B=https://arbitrum.blob.core.windows.net/em-mapping
export HOME=/root AZCOPY_BUFFER_GB=1 AZCOPY_LOG_LOCATION=/data/emv2-dev/azcopy-logs
up() { # up <local_dir> <blob_prefix> [extra azcopy args]
  local src=$1 dst=$2; shift 2
  azcopy copy "$src/*" "$B/$dst?$S" --recursive --put-md5 --overwrite=ifSourceNewer --cap-mbps 4000 --log-level ERROR --output-level essential "$@" >/dev/null
  local lb=$(find "$src" -type f ! -path '*/stage/*' -printf '%s\n' | awk '{s+=$1} END {print s+0}')
  local rb=$(python3 /data/emv2-dev/ops/blob_bytes.py "$dst/")
  echo "$(date -u +%T) $dst local=${lb} blob=${rb}"
  [ "$rb" -ge "$lb" ] || { echo "MISMATCH $dst: blob bytes below local"; FAIL=1; }
}
snap=/data/emv2-in/arc/snap-20261020T1152Z
up $snap arc/emv2/inputs/snap-20261020T1152Z
up /data/emv2-in/arc/map-snap-20261020T1152Z arc/emv2/inputs/map-snap-20261020T1152Z --exclude-path stage
up /data/emv2-in/arc/weth9 arc/emv2/inputs/weth9-extra-8af0335ecc25880f
up /data/emv2-in/linea/weth9 linea/emv2/inputs/weth9-extra-b1459408dcb9bbda
rec=/data/emv2-dev/ops/records-20261020; rm -rf $rec; mkdir -p $rec/ids
cp /data/emv2-dev/ops/ids/* $rec/ids/
for d in /data/emv2-out/*/* /data/emv2-dev/determinism/*/*/*; do
  [ -d "$d" ] || continue; t=$rec/builds/$(echo $d | sed 's#^/data/##; s#/#_#g'); mkdir -p $t
  find "$d" -maxdepth 1 -type f ! -name '*.seg' -exec cp {} $t/ \;
done
up $rec emv2/ops-records/20261020
[ $FAIL = 0 ] || { echo "$(date -u +%T) FINAL_SYNC_FAILED"; exit 1; }
echo "$(date -u +%T) FINAL_SYNC_DONE"
