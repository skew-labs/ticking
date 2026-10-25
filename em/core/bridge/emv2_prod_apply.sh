#!/bin/bash
# emv2_prod_apply.sh (run on skew as root): put the EMV2 shadow block into the production bridge (em-ticking-bridge, :8896)
# and nothing else. The running em_bridge.py is copied, patched with apply_bridge_emv2.py, checked to differ from it by exactly
# the inserted block, installed, and the bridge restarted once. The drop-in, account-bridge.json, registry and logos are not
# touched. No emv2-bridge.json is written, so every route stays on its current path ("old").
#   emv2_prod_apply.sh            check_prod baseline, apply, EM route comparison, check_prod compare (rolls back on a change)
#   emv2_prod_apply.sh --dry-run  build and verify the patched file only
#   emv2_prod_apply.sh --keep-config  update the block on a bridge that already has emv2-bridge.json (routes unchanged)
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
MARK, END = "# EMV2 shadow (emv2/bridge/apply_bridge_emv2.py", "Handler.api = _emv2_api"
def strip(t):   # the file without its EMV2 block (rule line .. Handler.api = _emv2_api), if it has one
    if MARK not in t: return t
    a = t.rindex("\n# ----", 0, t.index(MARK)) + 1; b = t.index(END, a) + len(END)
    return (t[:a] + t[b:]).replace("\n\n\n", "\n\n")
assert blk in new, "patched file lacks the new EMV2 block"
assert strip(new).replace("\n\n\n", "\n\n") == strip(old).replace("\n\n\n", "\n\n"), "patched file differs from the running one by more than the EMV2 block"
compile(new, "em_bridge.py", "exec")
print("patched file = running file with the EMV2 block %s (%d lines); compiles" % ("replaced" if MARK in old else "added", blk.count("\n")))
EOF
# A block update on a bridge whose emv2-bridge.json already switches routes needs --keep-config: the same routes stay switched
# across the restart, so the before/after EM route snapshot still compares like with like.
KEEP=0; for a in "$@"; do [ "$a" = --keep-config ] && KEEP=1; done
if [ -e $D/emv2-bridge.json ] && [ $KEEP = 0 ]; then echo "refusing: $D/emv2-bridge.json exists (pass --keep-config to update the block with the current routes)"; exit 1; fi
for a in "$@"; do [ "$a" = --dry-run ] && { echo "dry run: nothing installed"; exit 0; }; done
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
