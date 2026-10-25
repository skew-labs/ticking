import json, csv, urllib.request, duckdb
from collections import Counter, defaultdict
G = "golden/arbitrum"; B = "http://127.0.0.1:8741/api/public/em/graph"
rows = list(csv.DictReader(open(f"{G}/sample.csv")))
qa = [r["address"] for r in rows if r["why"] == "random"][200:235] + [r["address"] for r in rows if "hub_out" in r["why"]][:6]
db = duckdb.connect(); lst = ",".join(f"'{a}'" for a in qa)
mm = defaultdict(list)
for f, t, a, k, n, amt, fb, lb in db.execute(f"""SELECT '0x'||lower(hex("from")), '0x'||lower(hex("to")), CASE WHEN kind='native_tx' THEN NULL ELSE '0x'||lower(hex(asset)) END, kind,
    n::BIGINT, coalesce(total_amount::VARCHAR, total_amount_big), first_block, last_block FROM read_parquet('{G}/edges.parquet') WHERE '0x'||lower(hex("from")) IN ({lst})""").fetchall():
    mm[(f, t)].append(dict(a=a, k=k, n=n, amt=amt, fb=fb, lb=lb))
K = {"native": ["native_tx"], "erc20": ["erc20"], "nft": ["erc721", "erc1155"]}
c = Counter(); ex = []; asset_shape = Counter()
for q in qa:
    j = json.loads(urllib.request.urlopen(f"{B}?address={q}&chain=arbitrum&hops=1&window=all&limit=25", timeout=120).read())
    shown = defaultdict(list)
    for e in j["edges"]:
        s, t = e["source"].split(":")[1], e["target"].split(":")[1]
        if s != q or e.get("hop") != 1 or not e.get("exact"): continue
        asset_shape[json.dumps(sorted((e.get("asset") or {}).keys())) + (" addr_null" if not (e.get("asset") or {}).get("address") else " addr_set")] += 1
        cand = [m for m in mm[(s, t)] if m["k"] in K.get(e["kind"], [e["kind"]])]
        hit = [m for m in cand if m["n"] == e["n"] and (m["amt"] or None) == (e.get("amountRaw") or None) and m["fb"] == e.get("firstBlock") and m["lb"] == e.get("lastBlock")]
        c["exact_out"] += 1
        if hit:
            c["match_some_map_row"] += 1; shown[(s, t, e["kind"])].append(hit[0])
        else:
            c["no_map_row"] += 1
            if len(ex) < 3: ex.append({"v2": {k: e.get(k) for k in ("target", "kind", "asset", "n", "amountRaw", "firstBlock", "lastBlock")}, "map_rows": cand[:3]})
    for (s, t, kd), hits in shown.items():
        cand = [m for m in mm[(s, t)] if m["k"] in K.get(kd, [kd])]
        if len(hits) < len(cand): c["pairs_with_assets_not_shown"] += 1; c["assets_not_shown"] += len(cand) - len(hits)
print(dict(c), dict(asset_shape), json.dumps(ex)[:1500])
