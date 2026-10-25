#!/usr/bin/env python3
"""compare_polygon_held.py BAL_FOCUS.tsv.zst [--src golden/polygon] [--sample N]: EMV2 balances on the Polygon held (risk) pairs.

The independent HUGEINT exact run stopped after 1,880 of 2,244 token_transfers files, so:
  complete pairs: tokens that never appear in the 364 unread files -> exact = partial (exact_side_partial), compared for every pair;
  sampled pairs:  N tokens that do appear there (half the most-paired, half a fixed hash order) -> exact = partial + the unread files'
                  contribution, recomputed here straight from Blob (polygon_remaining.py sums) and compared for every pair of those tokens;
  the rest are counted as unverified.
Pairs with a >= 2^100 transfer (excluded_pairs) are reported apart: the exact run skipped those transfers, EMV2 does not.
Inputs in --src: exact_held_pairs.parquet, exact_side_partial.parquet, files_manifest.parquet, excluded_pairs.parquet (optional),
remaining_tokens.parquet (polygon_remaining.py tokens). Writes results/polygon/HELD.json.
"""
import json
import os
import subprocess
import sys

bal = sys.argv[1]
src = sys.argv[sys.argv.index("--src") + 1] if "--src" in sys.argv else "golden/polygon"
N = int(sys.argv[sys.argv.index("--sample") + 1]) if "--sample" in sys.argv else 300
out = "results/polygon"
os.makedirs(out, exist_ok=True)
DB = f"{out}/held.duckdb"


def duck(sql, db=DB):
    p = subprocess.run(["duckdb", "-json", db, "-c", f"SET threads=4; SET memory_limit='12GB'; SET temp_directory='/data/emv2-dev/verify/tmp/p{os.getpid()}'; " + sql],
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[:1500])
    return json.loads(p.stdout or "[]")


ex = f"{src}/excluded_pairs.parquet"
duck(f"""
CREATE OR REPLACE TABLE held AS SELECT DISTINCT lower(wallet) w, lower(token) t FROM read_parquet('{src}/exact_held_pairs.parquet');
CREATE OR REPLACE TABLE part AS SELECT lower(wallet) w, lower(token) t, partial_raw::HUGEINT p FROM read_parquet('{src}/exact_partial_str.parquet');
CREATE OR REPLACE TABLE rem_tok AS SELECT DISTINCT '0x' || lower(hex(token)) t FROM read_parquet('{src}/remaining_tokens.parquet');
CREATE OR REPLACE TABLE excl AS SELECT DISTINCT lower(wallet) w, lower(token) t FROM {"read_parquet('" + ex + "')" if os.path.exists(ex) else "(SELECT NULL::VARCHAR wallet, NULL::VARCHAR token WHERE false)"};
CREATE OR REPLACE TABLE v AS SELECT lower(address) w, lower(token) t, sum(TRY_CAST(balance AS HUGEINT)) b, count(*) FILTER (WHERE TRY_CAST(balance AS HUGEINT) IS NULL) nbig FROM read_csv('{bal}', header=true, all_varchar=true)
  WHERE kind = 'erc20' GROUP BY 1, 2;
""")
# complete tokens: every pair compared
comp = duck("""
WITH c AS (SELECT h.w, h.t, coalesce(part.p, 0) exact, v.b v2, (h.w, h.t) IN (SELECT (w, t) FROM excl) is_ex
           FROM held h LEFT JOIN part USING (w, t) LEFT JOIN v USING (w, t) WHERE h.t NOT IN (SELECT t FROM rem_tok))
SELECT count(*) pairs, count(*) FILTER (WHERE v2 IS NULL) v2_missing,
       count(*) FILTER (WHERE NOT is_ex AND coalesce(v2, 0) <> exact) differ, count(*) FILTER (WHERE is_ex AND coalesce(v2, 0) <> exact) differ_excluded,
       count(DISTINCT t) tokens FROM c""")[0]
comp_sample = duck("""SELECT h.w, h.t, coalesce(part.p, 0)::VARCHAR exact, v.b::VARCHAR v2 FROM held h LEFT JOIN part USING (w, t) LEFT JOIN v USING (w, t)
  WHERE h.t NOT IN (SELECT t FROM rem_tok) AND coalesce(v.b, 0) <> coalesce(part.p, 0) AND (h.w, h.t) NOT IN (SELECT (w, t) FROM excl) LIMIT 20""")
# sample tokens among those that do appear in the unread files
sample_file = f"{out}/sample_tokens.txt"
if os.path.exists(sample_file):   # keep the set the remaining-file sums were computed for
    toks = [{"t": l.strip()} for l in open(sample_file) if l.strip()]
else:
    toks = duck(f"""WITH r AS (SELECT t, count(*) n FROM held WHERE t IN (SELECT t FROM rem_tok) GROUP BY t)
SELECT t FROM (SELECT t FROM r ORDER BY n DESC, t LIMIT {N // 2}) UNION SELECT t FROM (SELECT t FROM r ORDER BY hash(t || 'emv2'), t LIMIT {N - N // 2})""")
    open(sample_file, "w").write("\n".join(x["t"] for x in toks) + "\n")
rem_files = f"{out}/remaining_files.txt"
open(rem_files, "w").write("\n".join("polygon/extraction/token_transfers/" + os.path.basename(x["file"]) for x in
                                     duck(f"SELECT file FROM read_parquet('{src}/files_manifest.parquet') WHERE NOT done ORDER BY file")) + "\n")
sums = f"{out}/remaining_sample_sums.parquet"
if not os.path.exists(sums):
    p = subprocess.run(["python3", "polygon_remaining.py", "sums", rem_files, sums, sample_file, "--threads", "2", "--par", "4"], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(p.stderr[-1500:])
rows = duck(f"""COPY (SELECT h.w, h.t, coalesce(part.p, 0)::VARCHAR p, coalesce(r.d0, '0') d0, coalesce(r.d1, '0') d1,
  coalesce(r.d2, '0') d2, coalesce(r.d3, '0') d3, coalesce(r.big_rows, 0) big, v.b::VARCHAR v2, (h.w, h.t) IN (SELECT (w, t) FROM excl) is_ex
  FROM held h LEFT JOIN part USING (w, t) LEFT JOIN v USING (w, t)
  LEFT JOIN (SELECT '0x' || lower(hex(holder)) w, '0x' || lower(hex(token)) t, * EXCLUDE (holder, token) FROM read_parquet('{sums}')) r USING (w, t)
  WHERE h.t IN (SELECT trim(column0) FROM read_csv('{sample_file}', header=false))) TO '{out}/sample_pairs.csv' (HEADER)""")
import csv
ns = nd = nde = nbig = 0
bad = []
for x in csv.DictReader(open(f"{out}/sample_pairs.csv")):
    ns += 1
    exact = int(x["p"]) + int(x["d0"]) + (int(x["d1"]) << 64) + (int(x["d2"]) << 128) + (int(x["d3"]) << 192)
    v2 = int(x["v2"]) if x["v2"] not in ("", None) else 0
    if exact != v2:
        if x["is_ex"] == "true" or int(x["big"]) > 0:
            nde += 1
        else:
            nd += 1
            if len(bad) < 20:
                bad.append({"w": x["w"], "t": x["t"], "exact": str(exact), "v2": str(v2)})
total_held = duck("SELECT count(*) n FROM held")[0]["n"]
v2_over_hugeint = duck("SELECT count(*) n FROM v WHERE nbig > 0")[0]["n"]
res = {"complete": {**comp, "sample_diffs": comp_sample, "ok": comp["differ"] == 0},
       "sampled": {"tokens": len(toks), "pairs": ns, "differ": nd, "differ_excluded_or_big": nde, "sample_diffs": bad, "ok": nd == 0},
       "unverified_pairs": total_held - comp["pairs"] - ns, "held_pairs": total_held, "v2_values_beyond_hugeint": v2_over_hugeint}
res["ok"] = res["complete"]["ok"] and res["sampled"]["ok"]
json.dump(res, open(f"{out}/HELD.json", "w"), indent=1, default=str)
print(json.dumps({k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk != "sample_diffs"}) for k, v in res.items()}))
