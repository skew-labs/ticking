#!/bin/bash
# emv2_prod_rollback.sh [backup dir] (run on skew as root): put back the production bridge file saved by emv2_prod_apply.sh
# (default: backups/LAST-EMV2), remove emv2-bridge.json if present, restart once. Nothing else is touched.
set -euo pipefail
D=/srv/skew/em-ticking-bridge
B=${1:-$(cat $D/backups/LAST-EMV2)}
[ -f "$B/em_bridge.py" ] || { echo "no backup at $B"; exit 1; }
rm -f $D/emv2-bridge.json
install -m 0644 "$B/em_bridge.py" $D/em_bridge.py
systemctl restart em-ticking-bridge
for i in $(seq 1 30); do [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' 127.0.0.1:8896/api/public/em/health)" = 200 ] && break; sleep 1; done
echo "bridge restored from $B: $(systemctl is-active em-ticking-bridge); emv2 block lines: $(grep -c 'EMV2 shadow (emv2/bridge' $D/em_bridge.py || true)"
