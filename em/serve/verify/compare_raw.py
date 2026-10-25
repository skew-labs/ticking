#!/usr/bin/env python3
"""compare_raw.py CHAIN RAW_BAL.csv DUMP_DIR [--as-of N]: EMV2 ledger dump against balances recomputed straight from extraction
token_transfers (raw_balances.sql + raw_big.py). Every (address, token, kind) row is compared: n_in, n_out, exact balance, first/last
block. Used where there is no map (Arc) and as an independent check of the map elsewhere. Writes results/<chain>/RAW.json."""
import json, os, subprocess, sys
chain, raw, d = sys.argv[1:4]
as_of = int(sys.argv[sys.argv.index("--as-of") + 1]) if "--as-of" in sys.argv else None
out = f"results/{chain}"; os.makedirs(out, exist_ok=True)
pick = lambda stem: next((f"{d}/{stem}{x}" for x in (".tsv.zst", ".csv", ".tsv") if os.path.exists(f"{d}/{stem}{x}")), f"{d}/{stem}.csv")
def duck(sql):
    p = subprocess.run(["duckdb", "-json", "-c", f"SET threads=4; SET memory_limit='12GB'; SET temp_directory='/data/emv2-dev/verify/tmp/p{os.getpid()}'; " + sql], capture_output=True, text=True)
    if p.returncode: raise RuntimeError(p.stderr[:1500])
    return json.loads(p.stdout or "[]")
cap = f" WHERE first_block::UBIGINT <= {as_of}" if as_of else ""
setup = (f"CREATE TEMP TABLE r AS SELECT lower(address) a, lower(token) t, kind k, n_in::UBIGINT nin, n_out::UBIGINT nout, balance::VARCHAR bal, "
         f"first_block::UBIGINT fb, last_block::UBIGINT lb FROM read_csv('{raw}', header=true, all_varchar=true); "
         f"CREATE TEMP TABLE v AS SELECT lower(address) a, lower(token) t, kind k, n_in::UBIGINT nin, n_out::UBIGINT nout, "
         f"ltrim(balance, '+')::VARCHAR bal, first_block::UBIGINT fb, last_block::UBIGINT lb FROM read_csv('{pick('bal')}', header=true, all_varchar=true); ")
res = []
def chk(name, sql, total):
    try:
        n = duck(setup + f"SELECT count(*) n FROM ({sql})")[0]["n"]
        s = duck(setup + f"SELECT * FROM ({sql}) LIMIT 20") if n else []
        res.append({"check": name, "ok": n == 0, "differing": n, "compared": duck(setup + total)[0]["n"], "sample": s})
    except RuntimeError as e:
        res.append({"check": name, "ok": False, "error": str(e)})
chk("raw rows missing in V2", "SELECT * FROM r ANTI JOIN v USING (a, t, k)", "SELECT count(*) n FROM r")
chk("V2 rows not in raw", "SELECT * FROM v ANTI JOIN r USING (a, t, k)", "SELECT count(*) n FROM v")
chk("n_in, n_out, balance (exact), first/last block equal",
    "SELECT r.*, v.nin v_nin, v.nout v_nout, v.bal v_bal, v.fb v_fb, v.lb v_lb FROM r JOIN v USING (a, t, k) "
    "WHERE r.nin <> v.nin OR r.nout <> v.nout OR r.bal <> v.bal OR r.fb <> v.fb OR r.lb <> v.lb", "SELECT count(*) n FROM r")
try:
    rc = duck(f"SELECT k, n FROM read_csv('{os.path.dirname(raw)}/raw_counts.csv', header=true, all_varchar=true)")
    cnt = dict(l.rstrip("\n").split("\t", 1) for l in open(pick("counts")) if "\t" in l)
    want = {"1": "events_erc20", "2": "events_erc721", "3": "events_erc1155", "unknown": "n_tt_unknown"}
    for row in rc:
        key = want.get(row["k"])
        if key:
            res.append({"check": f"counts {key}", "ok": str(cnt.get(key, "0")) == str(row["n"]), "got": cnt.get(key), "expected": row["n"]})
except Exception as e:
    res.append({"check": "counts", "ok": False, "error": str(e)[:300]})
ok = all(x.get("ok") for x in res)
json.dump({"chain": chain, "ok": ok, "raw": raw, "dump": d, "checks": res}, open(f"{out}/RAW.json", "w"), indent=1, default=str)
for x in res:
    print(("PASS " if x.get("ok") else "FAIL ") + x["check"], {k: x[k] for k in ("differing", "compared", "got", "expected", "error") if k in x})
print(chain, "RAW", "PASS" if ok else "FAIL")
