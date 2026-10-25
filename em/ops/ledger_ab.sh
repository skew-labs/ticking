#!/bin/bash
# ledger_ab.sh (VM, root): byte-identity check of the sliced/spilling ledger against the baseline binary.
#   ledger_ab.sh <chain> <build_id> <max_block> <parts> <per_pass> <slice_events> <ae_gb> [extra_dir] [allow_missing]
# One graph (dict.addr) under A, hard-linked into B; ledger A = baseline binary, ledger B = new binary with forced slicing
# (--slice-events) and forced by_addr spill (--ae-budget-gb); then every file in manifest.ledger.json must have the same
# sha256 in A and B, and the two part manifests must be identical.
set -euo pipefail
ch=$1; id=$2; maxb=$3; parts=$4; pp=$5; se=$6; ae=$7; extra=${8:-}; am=${9:-}
OLD=/data/emv2-dev/src-base/build/emv2-build-ledger; NEW=/data/emv2-dev/src-new/build/emv2-build-ledger; G=/data/emv2-dev/graph/cm2/build
root=/data/emv2-test/$ch; A=$root/A/$id; B=$root/B/$id
rm -rf $root; mkdir -p $A $B
log() { echo "$(date -u +%T) [ab $ch] $*" | tee -a $root/ab.log; }
"$G/emv2-build-graph" --chain $ch --chain-id $(python3 -c "print({'linea':59144,'unichain':130,'arc':5042,'robinhood':4663,'arbitrum':42161,'optimism':10}['$ch'])") \
  --map /data/emv2-in/$ch/map --out $A --tmp /data/emv2-tmp/ab-$ch --threads 8 --mem-budget 16 --build-id $id > $A/graph.facts.json
for f in $A/*; do ln "$f" $B/; done
args=(full --chain $ch --in /data/emv2-in/$ch/tt --blocks /data/emv2-in/$ch/blocks --build-id $id --max-block $maxb --parts $parts --per-pass $pp --threads 8)
[ -n "$extra" ] && args+=(--extra $extra)
[ -n "$am" ] && args+=(--allow-missing)
t=$(date +%s); /usr/bin/time -f "old maxrss_kb=%M" $OLD "${args[@]}" --out $A > $A/ledger.facts.json 2> $A/ledger.log; log "old done $(( $(date +%s) - t ))s $(tail -1 $A/ledger.log)"
t=$(date +%s); /usr/bin/time -f "new maxrss_kb=%M" $NEW "${args[@]}" --out $B --slice-events $se --ae-budget-gb $ae > $B/ledger.facts.json 2> $B/ledger.log; log "new done $(( $(date +%s) - t ))s $(tail -1 $B/ledger.log)"
grep -e "hot tokens" -e "spill runs" $B/ledger.log | head -3 | tee -a $root/ab.log
python3 - $A $B <<'PY' | tee -a $root/ab.log
import hashlib, json, sys, os
A, B = sys.argv[1], sys.argv[2]
ma, mb = json.load(open(A + "/manifest.ledger.json")), json.load(open(B + "/manifest.ledger.json"))
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 24), b""): h.update(b)
    return h.hexdigest()
fa, fb = ma["files"], mb["files"]
names = sorted(set(fa) | set(fb)); diff = [n for n in names if n not in fa or n not in fb or sha(A + "/" + n) != sha(B + "/" + n)]
print(f"files A={len(fa)} B={len(fb)} differ={len(diff)} {diff[:8]}")
print("manifest.ledger.json identical:", json.dumps(ma, sort_keys=True) == json.dumps(mb, sort_keys=True))
if json.dumps(ma, sort_keys=True) != json.dumps(mb, sort_keys=True):
    for k in sorted(set(ma.get("facts", {})) | set(mb.get("facts", {}))):
        if ma.get("facts", {}).get(k) != mb.get("facts", {}).get(k): print("  fact", k, ma.get("facts", {}).get(k), mb.get("facts", {}).get(k))
print("RESULT", "PASS" if not diff and json.dumps(ma, sort_keys=True) == json.dumps(mb, sort_keys=True) else "FAIL")
PY
