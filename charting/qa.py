"""Regression check: implausible wicks / closes on stock tokens and anchors, per stored resolution.

A bar is implausible when its high is above K x (or its low below 1/K x) the rolling median close of the
surrounding +-N bars of the same token and resolution. K = 1.5 (a 50% excursion inside one bar is not a
real move for a stock token, ETH/BTC or a stablecoin).
usage: qa.py <db> [--top 10]          (read-only; the server exposes the same report at /api/health/quality)
"""
import sys

K = 1.5
N = 10
PERIODS = (60, 300, 900, 3600, 14400, 86400)


def report(con, top=10, kinds=("stock", "anchor")):
    kl = ",".join("'%s'" % k for k in kinds)
    out = {"k": K, "window": N, "kinds": list(kinds), "periods": {}, "worst": []}
    for p in PERIODS:
        con.execute(f"""CREATE OR REPLACE TEMP TABLE qa_b AS
          SELECT b.token, mk.symbol, mk.kind, b.time, b.open, b.high, b.low, b.close,
                 median(b.close) OVER (PARTITION BY b.token ORDER BY b.time ROWS BETWEEN {N} PRECEDING AND {N} FOLLOWING) AS rm
          FROM token_bars b JOIN markets mk ON mk.token = b.token WHERE b.period = {p} AND mk.kind IN ({kl})""")
        r = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE high > rm * {K} OR low < rm / {K}),
                                   count(*) FILTER (WHERE close > rm * {K} OR close < rm / {K}),
                                   count(*) FILTER (WHERE open > rm * {K} OR open < rm / {K}),
                                   count(DISTINCT token) FILTER (WHERE high > rm * {K} OR low < rm / {K})
                            FROM qa_b""").fetchone()
        out["periods"][str(p)] = {"bars": r[0], "bad_wicks": r[1], "bad_closes": r[2], "bad_opens": r[3], "tokens_with_bad_wicks": r[4]}
        for row in con.execute(f"""SELECT symbol, kind, time, high, low, close, rm, greatest(high / rm, rm / low) AS x FROM qa_b
                                   WHERE high > rm * {K} OR low < rm / {K} ORDER BY x DESC LIMIT {top}""").fetchall():
            out["worst"].append({"period": p, "symbol": row[0], "kind": row[1], "time": row[2], "high": row[3], "low": row[4],
                                 "close": row[5], "rolling_median": row[6], "excursion": row[7]})
    out["worst"] = sorted(out["worst"], key=lambda w: -w["excursion"])[:top]
    return out


if __name__ == "__main__":
    import json
    import duckdb
    c = duckdb.connect(sys.argv[1], read_only=True)
    c.execute("SET threads=4")
    print(json.dumps(report(c, int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 10), indent=1, default=str))
