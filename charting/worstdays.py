"""Worst daily deviations vs Chainlink for chosen symbols (diagnostic)."""
import json, sys, time, urllib.request
import duckdb
API = "http://127.0.0.1:8895"
chain, syms = sys.argv[1], sys.argv[2:]
c = duckdb.connect()
mk = {m["symbol"]: m for m in json.load(urllib.request.urlopen(f"{API}/api/public/markets?network={chain}"))["tokens"] if m["symbol"] in syms}
for s, m in mk.items():
    tok = m["address"] if m["address"] != "0x" + "0" * 40 else "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    ref = dict(c.execute(f"SELECT epoch(day)::BIGINT * 1000 // 86400000 * 86400000, price_usd FROM read_parquet('/srv/skew/em-data/econ/{chain}/prices_daily.parquet') "
                         f"WHERE lower(token) = '{tok}' AND source LIKE 'chainlink:%'").fetchall())
    d = json.load(urllib.request.urlopen(f"{API}/api/public/engine/candles?aggregation=token&mint={m['id']}&interval=1d&limit=600&gap_policy=recorded"))
    rows = sorted(((b["close"] / ref[b["time"]] - 1) * 100, b["time"], b["close"], ref[b["time"]], b["volume"], b["count"]) for b in d["candles"] if b["time"] in ref)
    for dev, t, ours, r, vol, n in sorted(rows, key=lambda x: -abs(x[0]))[:3]:
        print(s, time.strftime("%F %a", time.gmtime(t / 1000)), f"ours {ours:.4f} chainlink {r:.4f} dev {dev:+.2f}% vol ${vol:,.0f} swaps {n}")
