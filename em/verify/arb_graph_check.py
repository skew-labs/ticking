#!/usr/bin/env python3
"""arb_graph_check.py: arbitrum graph 5bb7feeb57232401 on the staging serve :8741 against the frozen map sample (golden/arbitrum, map end
512,450,747 = 2026-10-07), the overlay label source and block timestamps (public RPC, sampled). Out-edges of sampled addresses are keyed to
map rows (from, to, asset, kind). Writes results/arbitrum/GRAPH_STAGED.json."""
import json, random, time, urllib.request, duckdb, csv
from collections import Counter, defaultdict
B = "http://127.0.0.1:8741/api/public/em/graph"; G = "golden/arbitrum"
rows = list(csv.DictReader(open(f"{G}/sample.csv")))
rnd = [r["address"] for r in rows if r["why"] == "random"][200:235]
hubs = [r["address"] for r in rows if "hub_out" in r["why"]][:6]
db = duckdb.connect(); db.execute("SET threads=2; SET memory_limit='3GB'")
def get(u):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(u, timeout=120) as r: b = r.read(); code = r.status
    except urllib.error.HTTPError as e: b = e.read(); code = e.code
    return code, (time.perf_counter() - t) * 1000, json.loads(b) if b[:1] == b"{" else {}
resp = []
for a in rnd + hubs:
    u = f"{B}?address={a}&chain=arbitrum&hops=1&window=all&limit=25"
    code, c0, j = get(u); _, w1, _ = get(u); _, w2, _ = get(u)
    resp.append({"a": a, "hub": a in hubs, "code": code, "cold": c0, "warm": [w1, w2], "body": j})
qa = [x["a"] for x in resp]
lst = ",".join(f"'{a}'" for a in qa)
me = db.execute(f"""SELECT '0x' || lower(hex("from")) f, '0x' || lower(hex("to")) t, CASE WHEN kind = 'native_tx' THEN NULL ELSE '0x' || lower(hex(asset)) END a, kind k,
  n::VARCHAR n, coalesce(total_amount::VARCHAR, total_amount_big) amt, first_block fb, last_block lb FROM read_parquet('{G}/edges.parquet')
  WHERE '0x' || lower(hex("from")) IN ({lst})""").fetchall()
mm = defaultdict(list)
for f, t, a, k, n, amt, fb, lb in me: mm[(f, t)].append({"a": a, "k": k, "n": n, "amt": amt, "fb": fb, "lb": lb})
KIND = {"native": ["native_tx"], "erc20": ["erc20"], "nft": ["erc721", "erc1155"], "erc721": ["erc721"], "erc1155": ["erc1155"]}
c = Counter(); bad = defaultdict(list); blocks = {}; nodes = set()
for x in resp:
    b = x["body"]; c["requests"] += 1; c[f"code_{x['code']}"] += 1
    c["spamEdgesHidden_total"] += int(b.get("spamEdgesHidden") or 0)
    if b.get("spamEdgesHidden"): c["responses_with_spam_hidden"] += 1
    for n in b.get("nodes", []): nodes.add(n["address"])
    for e in b.get("edges", []):
        src, tgt = e["source"].split(":", 1)[1], e["target"].split(":", 1)[1]
        sym = (e.get("asset") or {}).get("symbol") if isinstance(e.get("asset"), dict) else None
        c[f"exact_{e.get('exact')}"] += 1
        if src != x["a"] or e.get("hop") != 1: continue
        c["out_edges"] += 1
        if not e.get("exact"): c["out_inexact"] += 1; continue
        aa = (e.get("asset") or {}).get("address") if isinstance(e.get("asset"), dict) else None
        cand = [m for m in mm.get((src, tgt), []) if m["k"] in KIND.get(e["kind"], [e["kind"]]) and (m["a"] or None) == (aa.lower() if aa else None)]
        ok = any(m["n"] == str(e["n"]) and (m["amt"] or None) == (e.get("amountRaw") or None) and m["fb"] == e.get("firstBlock") and m["lb"] == e.get("lastBlock") for m in cand)
        if ok: c["out_exact_match"] += 1
        else:
            c["out_exact_diff"] += 1
            if len(bad["edge"]) < 3: bad["edge"].append({"v2": {k: e.get(k) for k in ("source", "target", "kind", "n", "amountRaw", "firstBlock", "lastBlock")}, "asset": aa, "map": cand[:2]})
        for bk, tk in (("firstBlock", "firstTime"), ("lastBlock", "lastTime")):
            if e.get(bk) is not None: blocks.setdefault(e[bk], []).append(e.get(tk))
        if sym is not None and (len(sym) > 24 or any(s in sym.lower() for s in ("http", ".com", "visit", "claim", "airdrop", "t.me", "www"))):
            c["shown_symbol_looks_spam"] += 1
            if len(bad["spam_shown"]) < 3: bad["spam_shown"].append(sym)
# block time check against the chain (public RPC, up to 40 blocks)
random.seed(7); bl = random.sample(sorted(blocks), min(40, len(blocks)))
def rpc(bn):
    q = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber", "params": [hex(bn), False]}).encode()
    r = urllib.request.Request("https://arb1.arbitrum.io/rpc", q, {"Content-Type": "application/json", "User-Agent": "curl/8"})
    return int(json.loads(urllib.request.urlopen(r, timeout=20).read())["result"]["timestamp"], 16)
for bn in bl:
    try: ts = rpc(bn)
    except Exception as ex: c["time_rpc_error"] += 1; continue
    c["time_checked"] += 1
    if any(v != ts for v in blocks[bn]):
        c["time_diff"] += 1
        if len(bad["time"]) < 3: bad["time"].append({"block": bn, "v2": blocks[bn][:2], "chain": ts})
# labels and isContract
nl = ",".join(f"'{a}'" for a in nodes)
lab = {r[0]: r[1:] for r in db.execute(f"SELECT '0x' || lower(hex(address)), category, entity_name, source, confidence FROM read_parquet('/srv/skew/em-graphloop/export/arbitrum/labels.parquet') WHERE '0x' || lower(hex(address)) IN ({nl})").fetchall()}
for x in resp:
    for n in x["body"].get("nodes", []):
        L = lab.get(n["address"]); c["nodes"] += 1
        if not L: continue
        c["nodes_labelled"] += 1
        cat, ent, src, conf = L
        if n.get("category") != cat: c["category_diff"] += 1; bad["category"].append((n["address"], n.get("category"), cat)) if len(bad["category"]) < 3 else None
        if (n.get("entity") or None) != (ent or None): c["entity_diff"] += 1; bad["entity"].append((n["address"], n.get("entity"), ent)) if len(bad["entity"]) < 3 else None
        if n.get("entity"):
            src = (src or "").lower(); g = "proven" if src in ("registry", "manual") else "judged" if src == "agent" else "inferred" if src == "propagation" or (src == "classifier" and (conf or 0) >= 0.8) else "judged"
            if n.get("entityGrade") != g: c["grade_diff"] += 1; bad["grade"].append((n["address"], n.get("entityGrade"), g, src, conf)) if len(bad["grade"]) < 3 else None
isc = {r[0]: r[1] for r in db.execute(f"SELECT '0x' || lower(hex(address)), is_contract FROM read_parquet('{G}/addresses.parquet')").fetchall()}
for x in resp:
    for n in x["body"].get("nodes", []):
        if n["address"] in isc:
            c["iscontract_checked"] += 1
            L = lab.get(n["address"]); want = bool(isc[n["address"]]) or ((L or [None])[0] or "").startswith(("contract", "token", "dex", "lending", "bridge", "multisig", "nft"))
            if bool(n.get("isContract")) != want: c["iscontract_diff"] += 1; bad["isContract"].append((n["address"], n.get("isContract"), isc[n["address"]], (L or [None])[0])) if len(bad["isContract"]) < 3 else None
pct = lambda xs, p: round(sorted(xs)[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 2)
lat = {"cold_p50": pct([x["cold"] for x in resp], 50), "cold_p99": pct([x["cold"] for x in resp], 99), "warm_p50": pct([w for x in resp for w in x["warm"]], 50), "warm_p99": pct([w for x in resp for w in x["warm"]], 99)}
out = {"sample_addresses": {"random": rnd, "hubs": hubs},
       "build": "5bb7feeb57232401", "serve": "staging :8741", "as_of": {"block": 512450747, "time": 1791345437, "note": "arbitrum map end 2026-10-07"}, "counts": dict(c), "latency_ms": lat, "diff_examples": dict(bad),
       "spam_example": next(({"spamEdgesHidden": x["body"].get("spamEdgesHidden"), "keys": [k for k in x["body"] if "spam" in k.lower()]} for x in resp if x["body"].get("spamEdgesHidden")), None)}
import os; os.makedirs("results/arbitrum", exist_ok=True)
json.dump(out, open("results/arbitrum/GRAPH_STAGED.json", "w"), indent=1, default=str)
print(json.dumps(out, default=str)[:3000])
