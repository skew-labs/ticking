import json, csv, urllib.request, duckdb
from collections import Counter
G = "golden/arbitrum"; B = "http://127.0.0.1:8741/api/public/em/graph"
rows = list(csv.DictReader(open(f"{G}/sample.csv")))
qa = [r["address"] for r in rows if r["why"] == "random"][200:235] + [r["address"] for r in rows if "hub_out" in r["why"]][:6]
db = duckdb.connect(); db.execute("SET threads=2")
lst = ",".join(f"'{a}'" for a in qa)
agg = {}
for f, t, k, n in db.execute(f"""SELECT '0x'||lower(hex("from")), '0x'||lower(hex("to")), kind, sum(n)::BIGINT FROM read_parquet('{G}/edges.parquet')
    WHERE '0x'||lower(hex("from")) IN ({lst}) GROUP BY ALL""").fetchall(): agg[(f, t, k)] = n
K = {"native": ["native_tx"], "erc20": ["erc20"], "nft": ["erc721", "erc1155"]}
from collections import defaultdict
c = Counter(); ex = []
for a in qa:
    j = json.loads(urllib.request.urlopen(f"{B}?address={a}&chain=arbitrum&hops=1&window=all&limit=25", timeout=60).read())
    v = defaultdict(int); ex_flag = defaultdict(set)
    for e in j["edges"]:
        s, t = e["source"].split(":")[1], e["target"].split(":")[1]
        if s != a or e.get("hop") != 1: continue
        v[(s, t, e["kind"])] += e["n"]; ex_flag[(s, t, e["kind"])].add(bool(e.get("exact")))
    for (s, t, kd), n in v.items():
        want = sum(agg.get((s, t, k), 0) for k in K.get(kd, [kd]))
        tag = "exact" if ex_flag[(s, t, kd)] == {True} else "mixed_or_inexact"
        c[f"{tag}_groups"] += 1
        if want == n: c[f"{tag}_n_match"] += 1
        else:
            c[f"{tag}_n_diff"] += 1
            if len(ex) < 4: ex.append({"src": s, "tgt": t, "kind": kd, "v2_n_sum": n, "map_n": want, "tag": tag})
print(dict(c), ex)
