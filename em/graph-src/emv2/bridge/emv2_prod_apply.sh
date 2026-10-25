#!/bin/bash
# emv2_prod_apply.sh (run on skew as root): put the EMV2 shadow block into the production bridge (em-ticking-bridge, :8896)
# and nothing else. The running em_bridge.py is copied, patched with apply_bridge_emv2.py, checked to differ from it by exactly
# the inserted block, installed, and the bridge restarted once. The drop-in, account-bridge.json, registry and logos are not
# touched. No emv2-bridge.json is written, so every route stays on its current path ("old").
#   emv2_prod_apply.sh            check_prod baseline, apply, EM route comparison, check_prod compare (rolls back on a change)
#   emv2_prod_apply.sh --dry-run  build and verify the patched file only
# Rollback: emv2_prod_rollback.sh (restores em_bridge.py from backups/emv2-<ts>/ and restarts).
set -euo pipefail
D=/srv/skew/em-ticking-bridge
HERE=$(cd "$(dirname "$0")" && pwd)
PROD=/srv/skew/em-account-prod/app/deploy/prod
TS=$(date -u +%Y%m%d-%H%M%S)
B=$D/backups/emv2-$TS
W=$(mktemp -d /tmp/emv2-prod.XXXXXX); trap 'rm -rf "$W"' EXIT
cp -p $D/em_bridge.py "$W/em_bridge.py.orig"; cp -p $D/em_bridge.py "$W/em_bridge.py"
python3 "$HERE/apply_bridge_emv2.py" "$W/em_bridge.py"
python3 - "$W" "$HERE/emv2_block.py" <<'EOF'
import sys
w, blk = sys.argv[1], open(sys.argv[2]).read().rstrip()
new, old = open(w + "/em_bridge.py").read(), open(w + "/em_bridge.py.orig").read()
assert new != old and new.replace("\n\n" + blk + "\n", "", 1) == old, "patched file differs from the running one by more than the EMV2 block"
compile(new, "em_bridge.py", "exec")
print("patched file = running file + EMV2 block (%d lines); compiles" % blk.count("\n"))
EOF
[ -e $D/emv2-bridge.json ] && { echo "refusing: $D/emv2-bridge.json exists (routes would leave 'old' on restart)"; exit 1; }
[ "${1:-}" = --dry-run ] && { echo "dry run: nothing installed"; exit 0; }
bash $PROD/check_prod.sh baseline /srv/skew/em-account-prod/checks/before-emv2-$TS.json
python3 "$HERE/emv2_route_snapshot.py" 8896 "$W/routes-before.json"
install -d -m 700 "$B"; cp -p $D/em_bridge.py "$B/em_bridge.py"; echo "$B" > $D/backups/LAST-EMV2
install -m 0644 "$W/em_bridge.py" $D/em_bridge.py
systemctl restart em-ticking-bridge
for i in $(seq 1 30); do [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' 127.0.0.1:8896/api/public/em/health)" = 200 ] && break; sleep 1; done
echo "bridge: $(systemctl is-active em-ticking-bridge) after ${i}s; emv2 block: $(grep -c 'EMV2 shadow (emv2/bridge' $D/em_bridge.py)"
python3 "$HERE/emv2_route_snapshot.py" 8896 "$W/routes-after.json" --compare "$W/routes-before.json" || { echo "EM routes changed: rolling back"; bash "$HERE/emv2_prod_rollback.sh" "$B"; exit 1; }
bash $PROD/check_prod.sh compare /srv/skew/em-account-prod/checks/before-emv2-$TS.json || { echo "console/chart/market checks changed: rolling back"; bash "$HERE/emv2_prod_rollback.sh" "$B"; exit 1; }
echo "applied; backup $B; rollback: bash $HERE/emv2_prod_rollback.sh"
