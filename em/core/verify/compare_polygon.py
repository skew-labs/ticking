#!/usr/bin/env python3
"""compare_polygon.py BAL_FOCUS.tsv.zst [--src golden/polygon]: EMV2 Polygon focus balances against the exact HUGEINT recompute.

golden/polygon/ (from Blob polygon/econ/verify/, written by the Polygon econ run):
  focus_exact.parquet   wallet, token, balance_raw HUGEINT (positive balances only), n_transfers [, has_excluded]
  risk_pairs.parquet    wallet, token, why, approx_raw DOUBLE (map net_approx), exact_raw HUGEINT, n
  sign_flips.parquet    wallet, token, approx DOUBLE, exact HUGEINT   (map double sign != exact sign)
  excluded_pairs.parquet (optional) pairs with a transfer >= 2^100 that the exact run skipped
EMV2: bal_focus.tsv.zst  address, token, kind, n_in, n_out, balance, first_block, last_block  (decimal strings)

PASS requires: every focus_exact pair equal in V2; no V2 positive balance on a focus pair that exact says is not positive;
risk pairs equal exact_raw; sign flips: V2 sign == exact sign (0 left). Pairs with excluded >= 2^100 transfers are reported apart.
"""
import json
import os
import subprocess
import sys

bal = sys.argv[1]
src = sys.argv[sys.argv.index("--src") + 1] if "--src" in sys.argv else "golden/polygon"
out = "results/polygon"
os.makedirs(out, exist_ok=True)


def duck(sql):
    p = subprocess.run(["duckdb", "-json", "-c", f"SET threads=4; SET memory_limit='24GB'; SET temp_directory='/data/emv2-dev/verify/tmp/p{os.getpid()}'; " + sql],
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[:2000])
    o = p.stdout.strip()
    return json.loads(o) if o else []


excl = os.path.exists(f"{src}/excluded_pairs.parquet")
# focus_exact.parquet stores the HUGEINT as DOUBLE (parquet has no 128-bit integer); focus_exact_str.parquet is the same table exported
# from the econ database with balance_raw as a decimal string, identical to the published file in double precision (checked)
fx = f"{src}/focus_exact_str.parquet" if os.path.exists(f"{src}/focus_exact_str.parquet") else f"{src}/focus_exact.parquet"
has_col = "has_excluded" in json.dumps(duck(f"DESCRIBE SELECT * FROM read_parquet('{src}/focus_exact.parquet')"))
ex_sql = ("SELECT lower(wallet) AS w, lower(token) AS t FROM read_parquet('{0}/excluded_pairs.parquet')".format(src) if excl else
          "SELECT lower(wallet) AS w, lower(token) AS t FROM read_parquet('{0}/focus_exact.parquet') WHERE has_excluded".format(src) if has_col else
          "SELECT NULL::VARCHAR AS w, NULL::VARCHAR AS t WHERE false")
setup = (f"CREATE TEMP TABLE v AS SELECT lower(address) AS w, lower(token) AS t, sum(TRY_CAST(balance AS HUGEINT)) AS b, count(*) FILTER (WHERE TRY_CAST(balance AS HUGEINT) IS NULL) AS nbig "
         f"FROM read_csv('{bal}', delim='\\t', header=true, all_varchar=true) WHERE kind = 'erc20' GROUP BY 1, 2; "
         f"CREATE TEMP TABLE x AS SELECT lower(wallet) AS w, lower(token) AS t, balance_raw::HUGEINT AS b FROM read_parquet('{fx}'); "
         f"CREATE TEMP TABLE ex AS {ex_sql}; ")
res = []


def chk(name, sql, total=None):
    try:
        n = duck(setup + f"SELECT count(*) AS n FROM ({sql}) q ANTI JOIN ex USING (w, t)")[0]["n"]
        e = duck(setup + f"SELECT count(*) AS n FROM ({sql}) q SEMI JOIN ex USING (w, t)")[0]["n"]
        s = duck(setup + f"SELECT * FROM ({sql}) LIMIT 20") if n + e else []
        tot = duck(setup + total)[0]["n"] if total else None
        res.append({"check": name, "ok": n == 0, "differing": n, "excluded_ge_2_100": e, "compared": tot, "sample": s})
    except RuntimeError as err:
        res.append({"check": name, "ok": False, "error": str(err)})


chk("focus_exact pairs equal in V2 (balance)", "SELECT x.w, x.t, x.b AS exact, v.b AS v2 FROM x LEFT JOIN v USING (w, t) WHERE v.b IS DISTINCT FROM x.b",
    "SELECT count(*) AS n FROM x")
chk("V2 positive where exact is not positive (focus tokens)", "SELECT v.w, v.t, v.b AS v2 FROM v ANTI JOIN x USING (w, t) WHERE v.b > 0 "
    "AND v.t IN (SELECT DISTINCT t FROM x)", "SELECT count(*) AS n FROM v WHERE t IN (SELECT DISTINCT t FROM x)")
has_exact = duck(f"SELECT count(exact_raw) AS n FROM read_parquet('{src}/risk_pairs.parquet')")[0]["n"] > 0 if os.path.exists(f"{src}/risk_pairs.parquet") else False
if has_exact: chk("risk pairs: V2 == exact_raw",
    f"SELECT r.w, r.t, r.why, r.exact, v.b AS v2 FROM (SELECT lower(wallet) AS w, lower(token) AS t, why, exact_raw::HUGEINT AS exact "
    f"FROM read_parquet('{src}/risk_pairs.parquet')) r LEFT JOIN v USING (w, t) WHERE coalesce(v.b, 0) <> coalesce(r.exact, 0)",
    f"SELECT count(*) AS n FROM read_parquet('{src}/risk_pairs.parquet')")
chk("sign flips: V2 sign == exact sign (map double was wrong)",
    f"SELECT f.w, f.t, f.approx, f.exact, v.b AS v2 FROM (SELECT lower(wallet) AS w, lower(token) AS t, approx, exact::HUGEINT AS exact "
    f"FROM read_parquet('{src}/sign_flips.parquet')) f LEFT JOIN v USING (w, t) WHERE sign(coalesce(v.b, 0)) <> sign(coalesce(f.exact, 0))",
    f"SELECT count(*) AS n FROM read_parquet('{src}/sign_flips.parquet')")
ok = all(r.get("ok") for r in res)
asof = json.load(open(f"{src}/AS_OF.json")) if os.path.exists(f"{src}/AS_OF.json") else None
json.dump({"ok": ok, "asOf": asof, "exactSource": fx, "exclusionSource": "excluded_pairs.parquet" if excl else "has_excluded" if has_col else "none", "checks": res},
          open(f"{out}/FOCUS.json", "w"), indent=1, default=str)
for r in res:
    print(("PASS " if r.get("ok") else "FAIL ") + r["check"], {k: r[k] for k in ("differing", "excluded_ge_2_100", "compared", "error") if k in r})
print("polygon focus", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
