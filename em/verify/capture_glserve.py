#!/usr/bin/env python3
"""capture_glserve.py PORT OUTDIR [N]: freeze glserve neighbors (dir=both/in/out, limit=64) for its top hubs and for N of their
non-hub neighbours, as the reference for EMV2 neighbors on a chain without a map (Arc). Saves OUTDIR/neighbors.jsonl."""
import json, sys, urllib.request
port, out = sys.argv[1], sys.argv[2]
n = int(sys.argv[3]) if len(sys.argv) > 3 else 150
get = lambda p: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}{p}", timeout=30).read())
hubs = [h["address"] for h in json.load(open(f"{out}/glserve_hubs.json"))]
seen, order = set(), []
with open(f"{out}/neighbors.jsonl", "w") as f:
    queue = list(hubs)
    while queue and len(order) < len(hubs) + n:
        a = queue.pop(0)
        if a in seen:
            continue
        seen.add(a); order.append(a)
        for d in ("both", "in", "out"):
            try:
                r = get(f"/tools/neighbors?ref={a}&dir={d}&limit=64")
            except Exception as e:
                r = {"error": str(e)}
            f.write(json.dumps({"address": a, "dir": d, "hub": a in hubs, "resp": r}) + "\n")
            if d == "both" and isinstance(r, dict):
                for x in (r.get("result") or r).get("neighbors", []) if isinstance(r.get("result") or r, dict) else []:
                    addr = (x.get("ref") or x.get("address")) if isinstance(x, dict) else None
                    if addr and addr not in seen and addr not in queue:
                        queue.append(addr)
print(len(order), "addresses frozen")
