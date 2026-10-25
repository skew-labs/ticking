#!/bin/bash
# diskguard.sh (VM, as root, transient unit emv2-diskguard): stop EMV2 build/stage units before /data fills, so Polygon econ and
# other owners' jobs never hit ENOSPC because of EMV2. Checks every 10 s; stops emv2-lane-* and emv2-stage-* below FLOOR_GB free.
FLOOR_GB=${FLOOR_GB:-45}
while true; do
  free=$(( $(df -B1 --output=avail /data | tail -1) / 1000000000 ))
  if (( free < FLOOR_GB )); then
    units=$(systemctl list-units --no-legend --state=active 'emv2-lane-*' 'emv2-stage-*' | awk '{print $1}')
    if [ -n "$units" ]; then
      echo "$(date -u +%T) /data free ${free}G < ${FLOOR_GB}G: stopping $units"
      systemctl stop $units
    fi
  fi
  sleep 10
done
