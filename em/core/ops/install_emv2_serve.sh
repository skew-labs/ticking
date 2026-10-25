#!/bin/bash
# install_emv2_serve.sh (skew, as root): install the emv2-serve binary built by the serve owner and register emv2-serve.service
# (loopback :8740). Owner-approved for EMV2 phase 1 on 2026-10-20. Re-run after a new binary; the old one is kept as .prev.
#   bash install_emv2_serve.sh [/srv/skew/emv2-dev/serve/build/emv2-serve]
set -euo pipefail
SRC=${1:-/srv/skew/emv2-dev/serve/build/emv2-serve}
ROOT=/srv/skew/emv2
[ -x "$SRC" ] || { echo "no executable at $SRC" >&2; exit 1; }
install -d -o ubuntu -g ubuntu "$ROOT" "$ROOT/bin" "$ROOT/logs"
[ -e "$ROOT/bin/emv2-serve" ] && cp -p "$ROOT/bin/emv2-serve" "$ROOT/bin/emv2-serve.prev"
install -o root -g root -m 0755 "$SRC" "$ROOT/bin/emv2-serve.new"
mv -f "$ROOT/bin/emv2-serve.new" "$ROOT/bin/emv2-serve"
cat > /etc/systemd/system/emv2-serve.service <<'EOF'
[Unit]
Description=EMV2 C++ read server (EMV2 phase 1, loopback :8740; bridge shadow target)
After=network.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
ExecStart=/srv/skew/emv2/bin/emv2-serve --root /srv/skew/emv2 --host 127.0.0.1 --port 8740 --workers 4 --cpus 4,5,6,7
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
MemoryHigh=3G
MemoryMax=4G
OOMScoreAdjust=600
Nice=10
IOSchedulingClass=idle
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
ReadOnlyPaths=/srv/skew/emv2
ReadWritePaths=/srv/skew/emv2/logs
StandardOutput=append:/srv/skew/emv2/logs/serve.log
StandardError=append:/srv/skew/emv2/logs/serve.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable emv2-serve.service >/dev/null 2>&1
systemctl restart emv2-serve.service
for i in $(seq 1 20); do [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' 127.0.0.1:8740/health)" = 200 ] && break; sleep 1; done
echo "emv2-serve: $(systemctl is-active emv2-serve) $(systemctl is-enabled emv2-serve)"
curl -s -m 5 127.0.0.1:8740/health; echo
