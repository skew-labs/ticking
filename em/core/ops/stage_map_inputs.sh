#!/bin/bash
# stage_map_inputs.sh (VM, as root): copy the graph builder inputs (map edges/ + addresses/) from same-region Blob to
# /data/emv2-in/<chain>/map/ before the builder needs them (same-region reads are free). IO only; one chain at a time.
#   bash stage_map_inputs.sh optimism robinhood arbitrum ethereum
# Refuses a chain when /data would drop below FLOOR_GB free after the copy. Never prints the SAS.
set -euo pipefail
S=$(cat /root/.em-blob.sas); S=${S#\?}
B=https://arbitrum.blob.core.windows.net/em-mapping
FLOOR_GB=${FLOOR_GB:-200}
CAP=${CAP_MBPS:-2000}
export AZCOPY_BUFFER_GB=1 HOME=/root AZCOPY_LOG_LOCATION=/data/emv2-dev/azcopy-logs
mkdir -p "$AZCOPY_LOG_LOCATION"
declare -A SRC=([linea]=linea/map [unichain]=unichain/map [optimism]=optimism/map [robinhood]=map/robinhood
                [arbitrum]=map/arbitrum [ethereum]=ethereum/map [base]=base/map [polygon]=polygon/map)
declare -A NEED_GB=([linea]=5 [unichain]=2 [optimism]=33 [robinhood]=13 [arbitrum]=26 [ethereum]=107 [base]=142 [polygon]=196)
for ch in "$@"; do
  src=${SRC[$ch]:?unknown chain $ch}
  free=$(( $(df -B1 --output=avail /data | tail -1) / 1000000000 ))
  if (( free - ${NEED_GB[$ch]} < FLOOR_GB )); then echo "$(date -u +%T) SKIP $ch: /data free ${free}G - ${NEED_GB[$ch]}G < floor ${FLOOR_GB}G"; continue; fi
  for t in addresses edges; do
    d=/data/emv2-in/$ch/map; mkdir -p "$d"
    azcopy copy "$B/$src/$t?$S" "$d/" --recursive --cap-mbps "$CAP" --check-md5 FailIfDifferent --log-level ERROR --output-level essential >/dev/null
    echo "$(date -u +%T) $ch $t $(du -sb "$d/$t" | cut -f1) bytes $(ls "$d/$t" | wc -l) files"
  done
  touch "/data/emv2-in/$ch/map/STAGED"
done
chown -R azureuser:azureuser /data/emv2-in
echo "$(date -u +%T) DONE $*"
