#!/bin/bash
# tools/determinism.sh DIR_A DIR_B : two builds of the same inputs (same build_id) must be byte-identical.
# Compares manifest.json "files" (name -> sha256, bytes) and re-hashes both trees. Exit 0 = identical.
set -euo pipefail
a=${1:?build dir A}; b=${2:?build dir B}
python3 - "$a" "$b" <<'PY'
import hashlib, json, os, sys
a, b = sys.argv[1], sys.argv[2]
ma, mb = (json.load(open(os.path.join(d, "manifest.json"))) for d in (a, b))
assert ma["build_id"] == mb["build_id"], "different build_id: inputs differ, not a determinism test"
fa, fb = ma["files"], mb["files"]
diff = sorted(set(fa) ^ set(fb)) + sorted(n for n in set(fa) & set(fb) if fa[n]["sha256"] != fb[n]["sha256"])
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""): h.update(blk)
    return h.hexdigest()
bad = [n for n in fa if n in fb and sha(os.path.join(a, n)) != fa[n]["sha256"]]
print(json.dumps({"build_id": ma["build_id"], "files": len(fa), "differ": diff, "rehash_mismatch": bad, "identical": not diff and not bad}))
sys.exit(0 if not diff and not bad else 1)
PY
