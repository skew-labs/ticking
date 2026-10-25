#!/usr/bin/env python3
"""compare_graph.py RESPONSES.jsonl: /api/public/em/graph answers (fetched on skew by graph_fetch.py) against the frozen maps and labels.

  hop-1 edges, window=all: every edge (source -> target, kind, asset) must equal the map edge (from, to, asset, kind): n, amountRaw (ERC721
      has none), firstBlock, lastBlock. kind map: native -> native_tx, erc20 -> erc20, nft -> erc721|erc1155.
  window=7d: an edge's firstBlock/lastBlock, when given, lie in (asOf.fromBlock, asOf.block].
  nodes: category and entity == labels.parquet (address-keyed overlay source); entityGrade from source/confidence:
      registry|manual -> proven, agent -> judged, propagation -> inferred, classifier with confidence >= 0.8 -> inferred, else judged;
      isContract == map is_contract OR the label category is a contract category.
  hop 2: hop-2 edges carry no amount; hubs at hop 1 are stop=true and are not expanded (no hop-2 edge leaves a hub).
  USD: USDC/USDT/DAI-style stablecoins usd == amount; other assets usd null with a usdReason.
  latency: warm p50/p99 per hops (two warm repeats per request).
Writes results/GRAPH.json."""
import json, os, statistics, subprocess, sys
from collections import Counter, defaultdict
rows = [json.loads(l) for l in open(sys.argv[1])]
MAP = {"arc": "/data/emv2-in/arc/map-snap-20261020T1152Z", "unichain": "/data/emv2-in/unichain/map"}
LBL = {"arc": "graphv/labels_arc.parquet", "unichain": "graphv/labels_unichain.parquet"}
STABLE = {"USDC", "USDT", "DAI", "USDC.E", "USDBC", "USDT0"}
KIND = {"native": ["native_tx"], "erc20": ["erc20"], "nft": ["erc721", "erc1155"], "erc721": ["erc721"], "erc1155": ["erc1155"]}
def duck(sql):
    p = subprocess.run(["duckdb", "-json", "-c", f"SET threads=4; SET memory_limit='8GB'; SET temp_directory='/data/emv2-dev/verify/tmp/g{os.getpid()}'; " + sql],
                       capture_output=True, text=True)
    if p.returncode: raise SystemExit(p.stderr[:800])
    return json.loads(p.stdout or "[]")
addr = lambda node_id: node_id.split(":", 1)[1]
res = {"requests": len(rows), "non200": sum(r["code"] != 200 for r in rows)}
for ch in MAP:
    rs = [r for r in rows if r["chain"] == ch and r["code"] == 200]
    want, nodes = set(), set()
    for r in rs:
        for e in r["body"].get("edges", []):
            if e.get("hop") == 1 and r["window"] == "all":
                want.add((addr(e["source"]), addr(e["target"])))
        for n in r["body"].get("nodes", []):
            nodes.add(n["address"])
    pairs = ",".join(f"('{a}','{b}')" for a, b in want) or "(NULL,NULL)"
    me = duck(f"""SELECT '0x' || lower(hex("from")) f, '0x' || lower(hex("to")) t, CASE WHEN kind = 'native_tx' THEN NULL ELSE '0x' || lower(hex(asset)) END a, kind k,
      n::VARCHAR n, coalesce(total_amount::VARCHAR, total_amount_big) amt, first_block fb, last_block lb FROM read_parquet('{MAP[ch]}/edges/*.parquet')
      WHERE ('0x' || lower(hex("from")), '0x' || lower(hex("to"))) IN (SELECT (f, t) FROM (VALUES {pairs}) v(f, t))""")
    mm = defaultdict(list)
    for m in me: mm[(m["f"], m["t"])].append(m)
    nl = ",".join(f"'{a}'" for a in nodes)
    lab = {x["a"]: x for x in duck(f"SELECT '0x' || lower(hex(address)) a, category, entity_name, source, confidence FROM read_parquet('{LBL[ch]}') WHERE '0x' || lower(hex(address)) IN ({nl})")}
    isc = {x["a"]: x["c"] for x in duck(f"SELECT '0x' || lower(hex(address)) a, is_contract c FROM read_parquet('{MAP[ch]}/addresses/*.parquet') WHERE '0x' || lower(hex(address)) IN ({nl})")}
    c = Counter(); bad = defaultdict(list); lat = defaultdict(list)
    for r in rs:
        b = r["body"]; lat[r["hops"]] += r["warm_ms"]
        frm = ((b.get("asOf") or {}).get(ch) or {}).get("fromBlock"); to = ((b.get("asOf") or {}).get(ch) or {}).get("block")
        hubs1 = {n["id"] for n in b.get("nodes", []) if n.get("hop") == 1 and n.get("isHub")}
        for e in b.get("edges", []):
            sym = (e.get("asset") or {}).get("symbol") if isinstance(e.get("asset"), dict) else None
            if e.get("hop") == 1 and r["window"] == "all":
                c["hop1_edges"] += 1
                aa = (e.get("asset") or {}).get("address") if isinstance(e.get("asset"), dict) else None
                cand = [m for m in mm.get((addr(e["source"]), addr(e["target"])), []) if m["k"] in KIND.get(e["kind"], [e["kind"]]) and (m["a"] or None) == (aa.lower() if aa else None)]
                ok = any(m["n"] == str(e["n"]) and (m["amt"] or None) == (e.get("amountRaw") or None) and m["fb"] == e.get("firstBlock") and m["lb"] == e.get("lastBlock") for m in cand)
                if not ok and e["kind"] == "nft" and aa is None:
                    c["hop1_nft_unkeyed"] += 1      # nft edge without an asset address: cannot be keyed to one map row
                elif not ok:
                    c["hop1_edge_diffs"] += 1
                    if len(bad["edge"]) < 8: bad["edge"].append({"v2": {k: e.get(k) for k in ("source", "target", "kind", "n", "amountRaw", "firstBlock", "lastBlock")}, "asset": aa, "map": cand[:2]})
            if r["window"] == "7d" and e.get("hop") == 1:
                c["window_edges"] += 1
                for k in ("firstBlock", "lastBlock"):
                    v = e.get(k)
                    if v is not None and not (frm < v <= to):
                        c["window_out_of_range"] += 1
                        if len(bad["window"]) < 5: bad["window"].append({"edge": e, "fromBlock": frm, "toBlock": to})
            if e.get("hop") == 2:
                c["hop2_edges"] += 1
                if e.get("amountRaw") is not None or e.get("amount") is not None: c["hop2_with_amount"] += 1
                if e["source"] in hubs1 or e["target"] in hubs1:
                    if not (e["source"].endswith(r["address"]) or e["target"].endswith(r["address"])): c["hop2_from_hub"] += 1
            if e.get("hop") == 1 and e.get("amount") is not None:
                if sym and sym.upper() in STABLE:
                    c["usd_stable"] += 1
                    if e.get("usd") is None or abs(e["usd"] - e["amount"]) > 1e-9 * max(1, abs(e["amount"])): c["usd_stable_wrong"] += 1
                elif e.get("usd") is not None or not e.get("usdReason"):
                    c["usd_nonstable_wrong"] += 1
        for n in b.get("nodes", []):
            c["nodes"] += 1
            if n.get("hop") == 1 and n.get("isHub") and r["hops"] == 2 and not n.get("stop"): c["hub_not_stopped"] += 1
            L = lab.get(n["address"])
            if L:
                c["nodes_labelled"] += 1
                src, conf = (L["source"] or "").lower(), L["confidence"] or 0
                grade = "proven" if src in ("registry", "manual") else "judged" if src == "agent" else "inferred" if src == "propagation" or (src == "classifier" and conf >= 0.8) else "judged"
                if n.get("category") != L["category"]: c["category_diffs"] += 1; bad["category"].append((n["address"], n.get("category"), L["category"])) if len(bad["category"]) < 5 else None
                if (n.get("entity") or None) != (L["entity_name"] or None): c["entity_diffs"] += 1; bad["entity"].append((n["address"], n.get("entity"), L["entity_name"])) if len(bad["entity"]) < 5 else None
                if n.get("entityGrade") != grade: c["grade_diffs"] += 1; bad["grade"].append((n["address"], n.get("entityGrade"), grade, src, conf)) if len(bad["grade"]) < 5 else None
            want_c = bool(isc.get(n["address"])) or ((L or {}).get("category") or "").startswith(("contract", "token", "dex", "lending", "bridge", "multisig", "nft"))
            if n["address"] in isc and bool(n.get("isContract")) != want_c:
                c["iscontract_diffs"] += 1
                if len(bad["isContract"]) < 5: bad["isContract"].append((n["address"], n.get("isContract"), isc.get(n["address"]), (L or {}).get("category")))
    pct = lambda xs, p: round(sorted(xs)[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 2) if xs else None
    res[ch] = {"counts": dict(c), "samples": dict(bad), "warm_ms": {f"hops{h}": {"p50": pct(v, 50), "p99": pct(v, 99), "n": len(v)} for h, v in lat.items()}}
    fatal = sum(c[k] for k in ("hop1_edge_diffs", "window_out_of_range", "hop2_with_amount", "hop2_from_hub", "hub_not_stopped", "usd_stable_wrong", "usd_nonstable_wrong",
                               "category_diffs", "entity_diffs", "grade_diffs", "iscontract_diffs"))
    res[ch]["ok"] = fatal == 0
res["ok"] = all(res[ch]["ok"] for ch in MAP) and res["non200"] == 0
json.dump(res, open("results/GRAPH.json", "w"), indent=1, default=str)
for ch in MAP:
    print(ch, "PASS" if res[ch]["ok"] else "FAIL", res[ch]["counts"], res[ch]["warm_ms"])
    for k, v in res[ch]["samples"].items(): print("  ", k, json.dumps(v, default=str)[:600])
