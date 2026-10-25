#!/bin/bash
# install_emv2_coldfetch.sh (skew, root): the cold-tier fetcher (controller 2026-10-21: "Blob Range + LRU", base·polygon·arbitrum
# exact kinds). emv2-serve (sandboxed, no network) asks for 64 KiB blocks through <root>/.coldfetch.fifo; this daemon reads them
# from Blob with HTTP Range and keeps at most CAP_GB of them on disk (LRU, holes punched on eviction).
#   CAP_GB=8 bash install_emv2_coldfetch.sh
set -euo pipefail
CAP_GB=${CAP_GB:-8}
install -d -m 755 /srv/skew/emv2/tools /srv/skew/emv2/logs
install -m 755 /srv/skew/emv2-dev/core/emv2/tools/emv2_coldfetch.py /srv/skew/emv2/tools/
cat > /etc/systemd/system/emv2-coldfetch.service <<UNIT
[Unit]
Description=EMV2 cold tier fetcher (Blob Range reads into sparse local caches, LRU ${CAP_GB} GB)
Before=emv2-serve.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I /srv/skew/emv2/tools/emv2_coldfetch.py daemon --cap-gb ${CAP_GB} --workers 24
Restart=always
RestartSec=2
Nice=5
IOSchedulingClass=best-effort
IOSchedulingPriority=4
MemoryHigh=700M
MemoryMax=1G
OOMScoreAdjust=300
StandardOutput=append:/srv/skew/emv2/logs/coldfetch.log
StandardError=append:/srv/skew/emv2/logs/coldfetch.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now emv2-coldfetch.service
sleep 2; systemctl is-active emv2-coldfetch.service; tail -n 3 /srv/skew/emv2/logs/coldfetch.log
