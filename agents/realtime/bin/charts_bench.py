"""charts_bench.py [n_markets] [rounds]: candle latency on em-charts :8895 (read only). Top markets by volume per chain,
intervals 1m/5m/15m/1h/4h/1d, limit 300; reports p50/p95/p99/max and payload size, cold (first) and warm rounds."""
import json, statistics, sys, time, urllib.request
H = "http://127.0.0.1:8895"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
R = int(sys.argv[2]) if len(sys.argv) > 2 else 3
def get(p):
    t0 = time.perf_counter()
    with urllib.request.urlopen(H + p, timeout=30) as r:
        b = r.read()
    return (time.perf_counter() - t0) * 1000, len(b)
mk = []
for ch in ("arbitrum", "robinhood", "arc", "ethereum", "optimism", "unichain", "linea", "base", "polygon"):
    d = json.loads(urllib.request.urlopen(H + "/api/public/markets?chain=%s&limit=500" % ch, timeout=30).read())
    toks = sorted([t for t in d.get("tokens") or [] if t.get("network") == ch], key=lambda t: -(t.get("volume") or 0))[:N]
    mk += [t["id"] for t in toks]
lat = {"cold": [], "warm": []}; size = []
for r in range(R):
    for m in mk:
        for iv in ("1m", "5m", "15m", "1h", "4h", "1d"):
            ms, n = get("/api/public/engine/candles?aggregation=token&market=%s&interval=%s&limit=300" % (m, iv))
            lat["cold" if r == 0 else "warm"].append(ms); size.append(n)
def q(v, p):
    v = sorted(v); return round(v[min(len(v) - 1, int(p * len(v)))], 1)
for k, v in lat.items():
    if v:
        print(k, "n", len(v), "p50", q(v, .5), "p95", q(v, .95), "p99", q(v, .99), "max", round(max(v), 1))
print("payload bytes p50", int(statistics.median(size)), "max", max(size), "markets", len(mk))
