#!/usr/bin/env python3
"""holders_check.py HOLDERS_JSON TOKEN: every snapshot of a holders-at answer against balances recomputed from the frozen Arc snapshot
token_transfers up to that snapshot's block: holder count (balance > 0; 0x0 and 0x..dead left out) and the top list (address order;
amount * 10^decimals within relative 1e-9 of the exact raw balance)."""
import json, os, subprocess, sys
ans = json.load(open(sys.argv[1])); tok = sys.argv[2].lower()[2:]
dec = ans.get("decimals") if isinstance(ans.get("decimals"), int) else 18
TT = "/data/emv2-in/arc/snap-20261020T1152Z/token_transfers/*.parquet"
out = []
for s in ans.get("snaps", []):
    if not s.get("available") or s.get("block") is None:
        out.append({"key": s.get("key"), "skipped": True}); continue
    n = len(s.get("top", []))
    sql = f"""SET threads=4; SET memory_limit='8GB'; SET temp_directory='/data/emv2-dev/verify/tmp/h{os.getpid()}';
CREATE TEMP TABLE v AS SELECT "from" f, "to" tt, ('0x' || substr(h, 33, 16))::UBIGINT::HUGEINT * 18446744073709551616::HUGEINT + ('0x' || substr(h, 49, 16))::UBIGINT::HUGEINT amt,
  substr(h, 1, 32) <> '00000000000000000000000000000000' over128
  FROM (SELECT "from", "to", lpad(hex(amount), 64, '0') h FROM read_parquet('{TT}') WHERE token = from_hex('{tok}') AND standard = 'ERC20' AND block_number <= {s['block']});
CREATE TEMP TABLE b AS SELECT a, sum(x) bal FROM (SELECT tt a, amt x FROM v UNION ALL SELECT f, -amt FROM v) GROUP BY a;
SELECT json_object('over128', (SELECT count(*) FILTER (WHERE over128) FROM v),
  'holders', (SELECT count(*) FROM b WHERE bal > 0 AND a NOT IN (from_hex('0000000000000000000000000000000000000000'), from_hex('000000000000000000000000000000000000dead'))),
  'top', (SELECT list(json_object('address', '0x' || lower(hex(a)), 'bal', bal::VARCHAR) ORDER BY bal DESC, a) FROM (SELECT * FROM b WHERE bal > 0
          AND a NOT IN (from_hex('0000000000000000000000000000000000000000'), from_hex('000000000000000000000000000000000000dead')) ORDER BY bal DESC, a LIMIT {max(n, 1)}))) j"""
    p = subprocess.run(["duckdb", "-json", "-c", sql], capture_output=True, text=True)
    if p.returncode:
        out.append({"key": s.get("key"), "error": p.stderr[:300]}); continue
    r = json.loads(p.stdout)[0]["j"]; r = json.loads(r) if isinstance(r, str) else r
    rt = [(json.loads(x) if isinstance(x, str) else x) for x in (r["top"] or [])]
    vt = s.get("top", [])
    addr_ok = [x["address"].lower() for x in vt] == [x["address"] for x in rt][:len(vt)]
    if not isinstance(ans.get("decimals"), int) and vt and rt and float(vt[0]["amount"]) > 0:   # answer without decimals: infer from the top holder
        import math
        dec = round(math.log10(int(rt[0]["bal"]) / float(vt[0]["amount"])))
    amt_ok = all(abs(float(v["amount"]) * 10 ** dec - int(x["bal"])) <= abs(int(x["bal"])) * 1e-9 + 1 for v, x in zip(vt, rt))
    out.append({"key": s.get("key"), "block": s["block"], "holders_v2": s.get("holders"), "holders_raw": r["holders"], "over128": r["over128"],
                "holders_ok": s.get("holders") == r["holders"], "decimals_used": dec, "top_n": len(vt), "top_addresses_ok": addr_ok, "top_amounts_ok": amt_ok,
                "first_top_diff": next(([v.get("address"), x["address"]] for v, x in zip(vt, rt) if v.get("address", "").lower() != x["address"]), None)})
ok = all(x.get("skipped") or (x.get("holders_ok") and x.get("top_addresses_ok") and x.get("top_amounts_ok")) for x in out)
print(json.dumps({"ok": ok, "snaps": out}))
