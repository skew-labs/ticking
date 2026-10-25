#!/usr/bin/env python3
"""latency.py PLAN.json OUT.json: p50/p99 latency of EMV2 (and the current services) for the design section 3 queries.

PLAN.json: {"targets": {"v2": "http://127.0.0.1:PORT", "old": "http://127.0.0.1:8906/api/public/em"},
            "queries": [{"name": "holders-at ARB", "path": {"v2": "/holders-at?...", "old": "/holders-at?..."}, "addresses": [...]?}]}
A query with "addresses" runs once per address with {addr} substituted.
cold = first request for each distinct query (no prior request for it in this run), warm = the same requests repeated twice more.
One request at a time (skew is the production host); load average is recorded before and after each target.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request


def get(url):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            r.read(); code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = -1
    return (time.perf_counter() - t) * 1000, code


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 2)


def load():
    try:
        return open("/proc/loadavg").read().split()[:3]
    except OSError:
        return None


plan = json.load(open(sys.argv[1]))
res = {"startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rows": []}
for tname, base in plan["targets"].items():
    l0 = load()
    for q in plan["queries"]:
        path = q["path"].get(tname)
        if not path:
            continue
        urls = [base + path.replace("{addr}", a) for a in q.get("addresses", [None])] if "{addr}" in path else [base + path]
        cold, warm, codes = [], [], {}
        for u in urls:
            ms, c = get(u); cold.append(ms); codes[c] = codes.get(c, 0) + 1
        for _ in range(2):
            for u in urls:
                ms, c = get(u); warm.append(ms); codes[c] = codes.get(c, 0) + 1
        row = {"target": tname, "query": q["name"], "n": len(urls), "cold_p50": pct(cold, 50), "cold_p99": pct(cold, 99), "cold_max": round(max(cold), 2),
               "warm_p50": pct(warm, 50), "warm_p99": pct(warm, 99), "codes": codes}
        res["rows"].append(row)
        print(json.dumps(row))
    res.setdefault("load", {})[tname] = {"before": l0, "after": load()}
json.dump(res, open(sys.argv[2], "w"), indent=1)
