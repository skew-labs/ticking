#!/bin/bash
# install_emv2_daily.sh (skew, root): daily overlay + featured refresh (owner-approved "매일 갱신", controller 2026-10-20).
set -euo pipefail
install -d -m 755 /srv/skew/emv2/tools
install -m 755 /srv/skew/emv2-dev/core/emv2/tools/emv2_overlay.py /srv/skew/emv2-dev/core/emv2/tools/emv2_featured.py /srv/skew/emv2-dev/core/emv2/tools/emv2_daily.sh /srv/skew/emv2/tools/
cat > /etc/systemd/system/emv2-daily.service <<UNIT
[Unit]
Description=EMV2 daily overlay (labels, token meta) and featured home snapshot refresh
After=emv2-serve.service

[Service]
Type=oneshot
ExecStart=/srv/skew/emv2/tools/emv2_daily.sh
Nice=19
IOSchedulingClass=best-effort
IOSchedulingPriority=7
CPUQuota=100%
MemoryHigh=2500M
MemoryMax=3G
OOMScoreAdjust=800
TimeoutStartSec=3600
StandardOutput=append:/srv/skew/emv2/logs/daily.log
StandardError=append:/srv/skew/emv2/logs/daily.log
UNIT
cat > /etc/systemd/system/emv2-daily.timer <<UNIT
[Unit]
Description=EMV2 daily overlay + featured refresh (03:40 UTC)

[Timer]
OnCalendar=*-*-* 03:40:00 UTC
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now emv2-daily.timer
systemctl list-timers emv2-daily.timer --no-pager | head -3
