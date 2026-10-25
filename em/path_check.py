#!/usr/bin/env python3
"""path_check.py: V2 path (staging :8741 /trace?chain&from&to&max_depth=6) vs glserve /tools/path (arc :8714, arbitrum :8711) on fixed pairs.
For each pair: found, hops, nodes, cold/warm latency; for disagreements, the degrees of glserve's path nodes (glserve neighbors) to classify."""
import json, csv, time, urllib.request
def get(u):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(u, timeout=180) as r: b = r.read()
    except urllib.error.HTTPError as e: b = e.read()
    return (time.perf_counter() - t) * 1000, json.loads(b) if b[:1] == b"{" else {"raw": b[:200].decode()}
ARC_HUB = "0x0000000000000068f116a894984e2db1123eb395"
arc = ['0x0026e64772314bcfaa62d7c11e3c94ed43c22315', '0x0029ce8962ebce6f68b03a144bcb89e8ecedbb75', '0x011509e0ca3c098d03af641df8fda549dd2fa585',
       '0x0165090e48f97aad08960dc148ad47a879d49305', '0x0192b3084c4ac8a374a811c3b877df3919b162d5', '0x019b6d68041fdbffbd779491dbeffebd3068d375',
       '0x01eb2835800dc986507080004069d0190dd4d265', '0x0220f0b422550f1c769180161f50d4012615a685', '0x0273f8251e26263e7a4e3d69df0e4671c592d785',
       '0x02fff1bb31ab7c9bff3fc6d4579b67aba71fc015']
rows = list(csv.DictReader(open("verify/golden/arbitrum/sample.csv")))
arb_hub = [r["address"] for r in rows if "hub_in" in r["why"]][0]
arb = [r["address"] for r in rows if r["why"] == "random"][300:305]
pairs = [("arc", a, ARC_HUB, 8714) for a in arc] + [("arbitrum", a, arb_hub, 8711) for a in arb]
def gl_nodes(g):
    r = g.get("result", g); p = r.get("path") or []
    return [x.get("ref") or x.get("address") if isinstance(x, dict) else x for x in p], r
def deg(port, ref):
    _, j = get(f"http://127.0.0.1:{port}/tools/neighbors?ref={ref}&dir=both&limit=1"); r = j.get("result", j)
    return (r.get("in_degree"), r.get("out_degree"))
out = []
for ch, a, hub, port in pairs:
    q = f"http://127.0.0.1:8741/api/public/em/trace?chain={ch}&from={a}&to={hub}&max_depth=6"
    c0, v = get(q); w1, _ = get(q); w2, _ = get(q)
    _, g = get(f"http://127.0.0.1:{port}/tools/path?from={a}&to={hub}&max_depth=6")
    gn, gr = gl_nodes(g)
    vn = [x.get("address") for x in v.get("path", [])]
    row = {"chain": ch, "from": a, "to": hub, "v2_found": v.get("found"), "v2_hops": v.get("hops"), "v2_budget_exhausted": v.get("budget_exhausted"),
           "v2_visited": v.get("visited"), "v2_nodes": vn, "gl_found": gr.get("found"), "gl_hops": (len(gn) - 1) if gn else gr.get("hops"), "gl_nodes": gn,
           "cold_ms": round(c0, 1), "warm_ms": [round(w1, 2), round(w2, 2)], "gl_error": g.get("error")}
    row["agree"] = bool(row["v2_found"]) == bool(row["gl_found"]) and (not row["v2_found"] or row["v2_hops"] == row["gl_hops"])
    if not row["agree"] and gn:
        row["gl_path_degrees"] = {n: deg(port, n) for n in gn}
        mids = gn[1:-1]
        cls = []
        if row["gl_hops"] and row["gl_hops"] > 6: cls.append("depth")
        if any(sum(x for x in d if x) > 20000 for n, d in row["gl_path_degrees"].items() if n in mids): cls.append("hub_intermediate_skipped(>20k)")
        if row["v2_budget_exhausted"]: cls.append("budget")
        if not cls: cls.append("unclassified(direction or hub endpoint list limit)")
        row["class"] = cls
    out.append(row)
json.dump(out, open("verify/results/path_check.json", "w"), indent=1)
for r in out:
    print(r["chain"], r["from"][:10], "V2", r["v2_found"], r["v2_hops"], "| glserve", r["gl_found"], r["gl_hops"], "| agree", r["agree"], "| cold", r["cold_ms"], "warm", r["warm_ms"], r.get("class", ""), r.get("gl_error") or "")
