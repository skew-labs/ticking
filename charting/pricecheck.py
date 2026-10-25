"""Compare em-charts daily closes with Chainlink daily prices from the econ layer (prices_daily)."""
import json
import statistics
import sys
import urllib.request

import duckdb

API = "http://127.0.0.1:8895"
chain = sys.argv[1]
c = duckdb.connect()
rows = c.execute(f"""SELECT lower(token), source, epoch(day)::BIGINT * 1000 AS t, price_usd
                     FROM read_parquet('/srv/skew/em-data/econ/{chain}/prices_daily.parquet') WHERE source LIKE 'chainlink:%'""").fetchall()
ref = {}
for tok, src, t, px in rows:
    ref.setdefault((tok, src), {})[t // 86400000 * 86400000] = px
markets = {m["address"]: m for m in json.load(urllib.request.urlopen(f"{API}/api/public/markets?network={chain}"))["tokens"]}
out = []
for (tok, src), days in sorted(ref.items()):
    if tok == "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee":
        tok = "0x0000000000000000000000000000000000000000"
    m = markets.get(tok)
    if not m:
        continue
    d = json.load(urllib.request.urlopen(f"{API}/api/public/engine/candles?aggregation=token&mint={m['id']}&interval=1d&limit=600&gap_policy=recorded"))
    ours = {b["time"]: b["close"] for b in d["candles"]}
    devs = [ours[t] / px - 1 for t, px in days.items() if t in ours and px]
    if not devs:
        continue
    a = sorted(abs(x) for x in devs)
    out.append((m["symbol"], src.split(":", 1)[1], len(devs), statistics.median(devs) * 100, a[len(a) // 2] * 100, a[int(len(a) * .9)] * 100, a[-1] * 100))
print(f"{'symbol':10} {'feed':16} {'days':>4} {'median%':>8} {'|med|%':>7} {'p90%':>7} {'max%':>7}")
for r in out:
    print(f"{r[0]:10} {r[1]:16} {r[2]:4d} {r[3]:8.3f} {r[4]:7.3f} {r[5]:7.3f} {r[6]:7.3f}")
