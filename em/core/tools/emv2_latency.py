#!/usr/bin/env python3
"""emv2_latency.py (skew): per chain and route latency of EMV2 answers, first ask (cold: answer cache empty for that query,
pages possibly not resident) and repeat (warm), engine direct (:8740) and through the bridge (:8896).

    python3 emv2_latency.py [--engine http://127.0.0.1:8740] [--bridge http://127.0.0.1:8896] [--n 30] [--out FILE.json]

Queries come from the served data itself: featured entities (graph 1/2 hops), the RPC truth sample tokens (holders, accumulators,
tokens). Each query string is made unique per run with a limit value, so the first ask misses the answer cache. GET only.
"""
import argparse
import glob
import json
import random
import time
import urllib.error
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--engine", default="http://127.0.0.1:8740")
ap.add_argument("--bridge", default="http://127.0.0.1:8896")
ap.add_argument("--n", type=int, default=30)
ap.add_argument("--out", default=None)
a = ap.parse_args()


def get(url):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            r.read(); code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    return (time.perf_counter() - t) * 1000, code


def pct(v, p):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * p))], 1) if v else None


health = json.load(urllib.request.urlopen(a.engine + "/health", timeout=10))
chains = sorted(health["chains"])
rng = random.Random(7)
rows = []
for c in chains:
    addrs = []
    try:
        addrs = [e["address"] for e in json.load(open(f"/srv/skew/emv2/featured/{c}.json"))["entities"] if isinstance(e.get("address"), str)]
    except (OSError, ValueError, KeyError):
        pass
    toks = []
    for f in sorted(glob.glob(f"/srv/skew/emv2/checks/truth/{c}-*.json"))[-1:]:
        toks = [t["token"] for t in json.load(open(f))["tokens"] if t.get("verdict") == "PASS"]
    lim = rng.randint(11, 19)   # unique per run: the first ask is an answer-cache miss
    routes = {
        "graph 1hop": [f"/graph?chain={c}&address={x}&hops=1&limit={lim}" for x in addrs[: a.n]],
        "graph 2hop": [f"/graph?chain={c}&address={x}&hops=2&limit={lim}" for x in addrs[: a.n]],
        "holders-at": [f"/holders-at?market={c}:{t}&block={health['chains'][c]['head']}&limit={lim + 10}" for t in toks[: a.n]],
        "holders snaps": [f"/holders-at?market={c}:{t}&snaps=1&limit={lim + 10}" for t in toks[: a.n]],
        "accumulators 7d": [f"/accumulators?market={c}:{t}&window=7d&limit={lim}" for t in toks[: a.n]],
    }
    for route, qs in routes.items():
        if not qs:
            continue
        cold, warm, bridge, codes = [], [], [], {}
        for q in qs:
            ms, code = get(a.engine + q); cold.append(ms); codes[code] = codes.get(code, 0) + 1
            ms, _ = get(a.engine + q); warm.append(ms)
            ms, _ = get(a.bridge + "/api/public/em" + q); bridge.append(ms)
        rows.append({"chain": c, "route": route, "n": len(qs), "codes": codes,
                     "cold_p50": pct(cold, .5), "cold_p99": pct(cold, .99), "warm_p50": pct(warm, .5), "warm_p99": pct(warm, .99),
                     "bridge_warm_p50": pct(bridge, .5), "bridge_warm_p99": pct(bridge, .99)})
        r = rows[-1]
        print(f"{c:10} {route:16} n={r['n']:3} cold p50 {r['cold_p50']:8} p99 {r['cold_p99']:8} | warm p50 {r['warm_p50']:6} p99 {r['warm_p99']:6} | "
              f"bridge p50 {r['bridge_warm_p50']:6} p99 {r['bridge_warm_p99']:6} | {codes}", flush=True)
if a.out:
    json.dump({"t": int(time.time()), "rows": rows}, open(a.out, "w"), indent=1)
