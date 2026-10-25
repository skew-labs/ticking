#!/usr/bin/env python3
"""accum_check.py ACCUM_JSON TOKEN [--limit N]: /accumulators answer (saved JSON) against an independent recompute from the frozen Arc
snapshot token_transfers over (window.fromBlock, window.toBlock]: per holder exact signed delta (ERC20; 0x0, 0x..dead and self transfers
left out). Compares the top-N accumulators and distributors (address + deltaRaw, order) and the scanned event count."""
import json, os, subprocess, sys
ans = json.load(open(sys.argv[1])); tok = sys.argv[2].lower()[2:]
N = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 10
fb, lb = ans["window"]["fromBlock"], ans["window"]["toBlock"]
TT = "/data/emv2-in/arc/snap-20261020T1152Z/token_transfers/*.parquet"
sql = f"""SET threads=4; SET memory_limit='8GB'; SET temp_directory='/data/emv2-dev/verify/tmp/acc{os.getpid()}';
CREATE TEMP TABLE t AS SELECT "from" f, "to" tt, lpad(hex(amount), 64, '0') h FROM read_parquet('{TT}')
  WHERE token = from_hex('{tok}') AND standard = 'ERC20' AND block_number > {fb} AND block_number <= {lb};
CREATE TEMP TABLE v AS SELECT f, tt, ('0x' || substr(h, 33, 16))::UBIGINT::HUGEINT * 18446744073709551616::HUGEINT + ('0x' || substr(h, 49, 16))::UBIGINT::HUGEINT amt,
  substr(h, 1, 32) <> '00000000000000000000000000000000' over128 FROM t;
CREATE TEMP TABLE d AS SELECT a, sum(x) delta FROM (SELECT tt a, amt x FROM v WHERE f <> tt UNION ALL SELECT f, -amt FROM v WHERE f <> tt)
  WHERE a NOT IN (from_hex('0000000000000000000000000000000000000000'), from_hex('000000000000000000000000000000000000dead')) GROUP BY a;
SELECT json_object('events', (SELECT count(*) FROM t), 'over128', (SELECT count(*) FILTER (WHERE over128) FROM v),
  'acc', (SELECT list(json_object('address', '0x' || lower(hex(a)), 'deltaRaw', delta::VARCHAR) ORDER BY delta DESC, a) FROM (SELECT * FROM d WHERE delta > 0 ORDER BY delta DESC, a LIMIT {N})),
  'dis', (SELECT list(json_object('address', '0x' || lower(hex(a)), 'deltaRaw', delta::VARCHAR) ORDER BY delta ASC, a) FROM (SELECT * FROM d WHERE delta < 0 ORDER BY delta ASC, a LIMIT {N}))) j;"""
p = subprocess.run(["duckdb", "-json", "-c", sql], capture_output=True, text=True)
if p.returncode:
    sys.exit(p.stderr[:600])
raw = json.loads(p.stdout)[0]["j"]
raw = json.loads(raw) if isinstance(raw, str) else raw
def side(k_ans, k_raw):
    a = [(x["address"].lower(), x["deltaRaw"]) for x in ans.get(k_ans, [])][:N]
    r = [((json.loads(x) if isinstance(x, str) else x)["address"], str((json.loads(x) if isinstance(x, str) else x)["deltaRaw"])) for x in (raw[k_raw] or [])]
    return a == r, a, r
ok_a, a1, r1 = side("accumulators", "acc"); ok_d, a2, r2 = side("distributors", "dis")
res = {"window": ans["window"], "events_v2": ans.get("scannedEvents"), "events_raw": raw["events"], "over128": raw["over128"],
       "accumulators_ok": ok_a, "distributors_ok": ok_d, "ok": ok_a and ok_d and ans.get("scannedEvents") == raw["events"],
       "first_diff": [(x, y) for x, y in zip(a1 + a2, r1 + r2) if x != y][:4]}
print(json.dumps(res))
