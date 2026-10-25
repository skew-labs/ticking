"""wallet300.py [workers]: 300 wallets across chains read through the production bridge (:8896, loopback = the Ask path) at the
given concurrency; counts the status codes and latencies. Read only."""
import concurrent.futures as cf, json, sqlite3, statistics, sys, time, urllib.error, urllib.request
import duckdb
W = int(sys.argv[1]) if len(sys.argv) > 1 else 2
pick = {"ethereum": 60, "base": 60, "optimism": 30, "linea": 30, "unichain": 20}
c = duckdb.connect(); rows = []
for ch, n in pick.items():
    r = c.execute("SELECT address FROM read_parquet(?) LIMIT 4000", ["/srv/skew/em-data/compact/%s/top_addresses.parquet" % ch]).fetchall()
    rows += [(ch, "0x" + bytes(a).hex() if not isinstance(a, str) else a) for (a,) in r[7::max(1, len(r) // n)][:n]]
g = sqlite3.connect("file:/srv/skew/em-galaxy/galaxy.db?mode=ro", uri=True)
for ch, n in (("arbitrum", 50), ("robinhood", 50)):
    rows += [(ch, a) for (a,) in g.execute("SELECT address FROM nodes WHERE chain=? AND in_map=1 AND node_id % 997 = 3 LIMIT ?", (ch, n))]
rows = rows[:300]
ERR = {}
def one(x):
    ch, a = x; t = time.time()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8896/api/public/em/tools/wallet?chain=%s&address=%s" % (ch, a), timeout=60) as r:
            r.read(); return r.status, (time.time() - t) * 1000
    except urllib.error.HTTPError as e:
        return e.code, (time.time() - t) * 1000
    except Exception as e:
        ERR[type(e).__name__ + ": " + str(e)[:70]] = ERR.get(type(e).__name__ + ": " + str(e)[:70], 0) + 1
        return 0, (time.time() - t) * 1000
t0 = time.time()
with cf.ThreadPoolExecutor(W) as ex:
    res = list(ex.map(one, rows))
codes = {}
for s, _ in res: codes[s] = codes.get(s, 0) + 1
lat = sorted(l for _, l in res)
print("exceptions", ERR)
print("wallets", len(rows), "workers", W, "wall %.0fs" % (time.time() - t0), "codes", codes,
      "p50 %.0fms p95 %.0fms max %.0fms" % (lat[len(lat) // 2], lat[int(len(lat) * .95)], lat[-1]))
