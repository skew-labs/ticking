#!/bin/bash
# stage_tt_inputs.sh (VM, as root): copy the ledger builder inputs (token_transfers Parquet, plus blocks for blk.time) from
# same-region Blob to /data/emv2-in/<chain>/{tt,blocks}/ for full-mode ledger builds. Temp files (*.tmp*) are skipped.
# base and polygon are not staged here: the ledger streams them (its own fetcher).
#   bash stage_tt_inputs.sh arc linea unichain optimism robinhood arbitrum ethereum
set -euo pipefail
S=$(cat /root/.em-blob.sas); S=${S#\?}
B=https://arbitrum.blob.core.windows.net/em-mapping
FLOOR_GB=${FLOOR_GB:-150}
CAP=${CAP_MBPS:-2000}
export AZCOPY_BUFFER_GB=1 HOME=/root AZCOPY_LOG_LOCATION=/data/emv2-dev/azcopy-logs
mkdir -p "$AZCOPY_LOG_LOCATION"
declare -A TT=([arc]=realtime/arc/token_transfers [linea]=linea/extraction/token_transfers [unichain]=unichain/extraction/token_transfers
               [optimism]=optimism/extraction/token_transfers [robinhood]=robinhood/token_transfers [arbitrum]=arbitrum/token_transfers
               [ethereum]=ethereum/extraction/token_transfers)
declare -A BLK=([arc]=realtime/arc/blocks [linea]=linea/extraction/blocks [unichain]=unichain/extraction/blocks
                [optimism]=optimism/extraction/blocks [robinhood]=robinhood/blocks [arbitrum]=arbitrum/blocks [ethereum]=ethereum/extraction/blocks)
declare -A NEED_GB=([arc]=3 [linea]=8 [unichain]=9 [optimism]=34 [robinhood]=34 [arbitrum]=94 [ethereum]=117)
for ch in "$@"; do
  [ -n "${TT[$ch]:-}" ] || { echo "$(date -u +%T) SKIP $ch: not a full-mode chain (base/polygon stream)"; continue; }
  free=$(( $(df -B1 --output=avail /data | tail -1) / 1000000000 ))
  if (( free - ${NEED_GB[$ch]} < FLOOR_GB )); then echo "$(date -u +%T) SKIP $ch: /data free ${free}G - ${NEED_GB[$ch]}G < floor ${FLOOR_GB}G"; continue; fi
  for kind in tt blocks ${WITH_LOGS:+logs}; do
    case $kind in tt) src=${TT[$ch]};; blocks) src=${BLK[$ch]};; logs) src=${TT[$ch]%/token_transfers}/logs;; esac
    d=/data/emv2-in/$ch/$kind; mkdir -p "$d"
    if ! azcopy copy "$B/$src/*?$S" "$d/" --recursive --exclude-pattern '*.tmp*' --cap-mbps "$CAP" --check-md5 FailIfDifferent \
         --log-level ERROR --output-level essential >/dev/null; then
      echo "$(date -u +%T) FAIL $ch $kind (see $AZCOPY_LOG_LOCATION)"; continue
    fi
    echo "$(date -u +%T) $ch $kind $(du -sb "$d" | cut -f1) bytes $(find "$d" -type f | wc -l) files"
  done
  touch "/data/emv2-in/$ch/TT_STAGED"
done
chown -R azureuser:azureuser /data/emv2-in
echo "$(date -u +%T) DONE $*"
