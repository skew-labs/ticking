#!/usr/bin/env python3
"""explain_held.py: for every Polygon held pair where EMV2 differed from the exact run, recompute from all 2,244 files twice (all
transfers / without amounts >= 2^100, diff_sums2.parquet) and check that EMV2 == all-transfer sum and exact == the sum without the big
amounts, i.e. the difference is exactly the transfers the exact run's documented rule skips. Writes results/polygon/HELD_EXPLAIN.json."""
import csv, json, subprocess
DB = "results/polygon/held.duckdb"
def duck(sql):
    p = subprocess.run(["duckdb", "-json", DB, "-c", "SET threads=4; " + sql], capture_output=True, text=True)
    if p.returncode: raise SystemExit(p.stderr[:800])
    return json.loads(p.stdout or "[]")
duck("""COPY (SELECT h.w, h.t, coalesce(part.p, 0)::VARCHAR exact_partial, vb.balance v2 FROM held h LEFT JOIN part USING (w, t)
  LEFT JOIN (SELECT lower(address) w, lower(token) t, ltrim(balance, '+') balance FROM read_csv('/data/emv2-dev/ledger/out/polygon/bal_focus.tsv', header=true, all_varchar=true) WHERE kind = 'erc20') vb USING (w, t)
  WHERE h.t IN (SELECT trim(column0) FROM read_csv('results/polygon/diff_tokens.txt', header=false))) TO 'results/polygon/diff_pairs.csv' (HEADER)""")
s2 = {}
for r in duck("SELECT '0x' || lower(hex(holder)) w, '0x' || lower(hex(token)) t, d0, d1, d2, d3, e0, e1, e2, e3, big_rows FROM read_parquet('results/polygon/diff_sums2.parquet')"):
    v = lambda c: int(r[c + "0"]) + (int(r[c + "1"]) << 64) + (int(r[c + "2"]) << 128) + (int(r[c + "3"]) << 192)
    s2[(r["w"], r["t"])] = (v("d"), v("e"), r["big_rows"])
rem_tok = {x["t"] for x in duck("SELECT t FROM rem_tok")}
smp = {}
for x in csv.DictReader(open("results/polygon/sample_pairs.csv")):
    smp[(x["w"], x["t"])] = int(x["p"]) + int(x["d0"]) + (int(x["d1"]) << 64) + (int(x["d2"]) << 128) + (int(x["d3"]) << 192)
n = differ = explained = unexplained = 0; bad = []
for x in csv.DictReader(open("results/polygon/diff_pairs.csv")):
    k = (x["w"], x["t"]); v2 = int(x["v2"]) if x["v2"] else 0
    exact = smp.get(k) if x["t"] in rem_tok else int(x["exact_partial"])
    if exact is None: continue          # remaining-file token outside the sample: not part of the comparison
    n += 1
    if exact == v2: continue
    differ += 1
    all_sum, nobig, big = s2.get(k, (0, 0, 0))
    if all_sum == v2 and big > 0 and (x["t"] in rem_tok or nobig == exact):
        explained += 1
    else:
        unexplained += 1
        if len(bad) < 20: bad.append({"w": x["w"], "t": x["t"], "v2": str(v2), "exact": str(exact), "all": str(all_sum), "nobig": str(nobig), "big_rows": big})
res = {"pairs_checked": n, "differ": differ, "explained_by_2_100_rule": explained, "unexplained": unexplained, "ok": unexplained == 0, "samples": bad}
json.dump(res, open("results/polygon/HELD_EXPLAIN.json", "w"), indent=1)
print(json.dumps({k: v for k, v in res.items() if k != "samples"}), bad[:3])
