#!/usr/bin/env python3
"""compare_neighbors.py CHAIN GOLDEN_JSONL V2_BASE OUTDIR: EMV2 neighbors vs the frozen glserve neighbors (chains without a map, e.g. Arc).

glserve rows: (ref, edge kind, n, dir) aggregated per peer and kind; EMV2 rows: (peer, asset, n) per peer and asset. Both are summed per
peer and direction and compared on glserve's top 64 peers: n must match exactly (same transfers counted); the top-10 peer sets
should overlap fully. A peer whose counts differ only because one side's data ends later is reported as a count difference with
both heads, so the reader can tell a time skew. One request at a time.
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict

chain, gpath, base, out = sys.argv[1:5]
os.makedirs(out, exist_ok=True)
rows, tot = [], defaultdict(int)
for line in open(gpath):
    g = json.loads(line)
    if g["dir"] not in ("in", "out") or "error" in g["resp"]:
        continue
    want = defaultdict(int)
    for x in g["resp"].get("neighbors", []):
        if x.get("dir", g["dir"]) == g["dir"]:
            want[x["ref"].lower()] += int(x["n"])
    try:
        v = json.loads(urllib.request.urlopen(f"{base}/api/public/em/emv2/neighbors?chain={chain}&address={g['address']}&dir={g['dir']}&limit=200", timeout=60).read())
    except Exception as e:
        rows.append({"address": g["address"], "dir": g["dir"], "error": str(e)[:200]}); tot["error"] += 1
        continue
    have = defaultdict(int)
    for r in v.get("rows", []):
        have[r["peer"].lower()] += int(r["n"])
    top_g = [p for p, _ in sorted(want.items(), key=lambda kv: -kv[1])[:10]]
    top_v = [p for p, _ in sorted(have.items(), key=lambda kv: -kv[1])[:10]]
    diffs = [{"peer": p, "glserve": n, "v2": have.get(p)} for p, n in want.items() if have.get(p) != n]
    full_list = len(v.get("rows", [])) < 200 and not v.get("truncated")
    tot["pairs"] += 1; tot["peers"] += len(want); tot["peer_n_equal"] += len(want) - len(diffs)
    tot["top10_full_overlap"] += set(top_g) == set(top_v) or (not full_list and set(top_g) <= set(have))
    rows.append({"address": g["address"], "dir": g["dir"], "hub": g["hub"], "glserve_peers": len(want), "v2_edges": v.get("edges"),
                 "tier": v.get("tier", "hot"), "n_diffs": diffs[:10], "top10_g": top_g, "top10_v": top_v})
ok = tot["error"] == 0 and tot["peer_n_equal"] == tot["peers"]
res = {"chain": chain, "ok": ok, "totals": dict(tot), "rows": rows}
json.dump(res, open(f"{out}/NEIGHBORS.json", "w"), indent=1)
print(chain, "neighbors", "PASS" if ok else "FAIL", dict(tot))
