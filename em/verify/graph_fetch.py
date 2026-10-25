#!/usr/bin/env python3
"""graph_fetch.py SAMPLE.json OUT.jsonl: fetch /api/public/em/graph from the local emv2-serve for the sampled addresses (hops 1 all / 7d,
hops 2 all), one request at a time, cold then two warm repeats; saves the bodies and latencies."""
import json, sys, time, urllib.request
S = json.load(open(sys.argv[1])); out = open(sys.argv[2], "w")
B = "http://127.0.0.1:8740/api/public/em/graph"
def get(u):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(u, timeout=120) as r: body, code = r.read(), r.status
    except urllib.error.HTTPError as e: body, code = e.read(), e.code
    return code, (time.perf_counter() - t) * 1000, body
for ch, grp in S.items():
    for kind, addrs in grp.items():
        for a in addrs:
            for hops, win in ((1, "all"), (1, "7d"), (2, "all")):
                if hops == 2 and kind == "random" and addrs.index(a) >= 10: continue
                u = f"{B}?address={a}&chain={ch}&hops={hops}&window={win}&limit=25"
                code, ms0, body = get(u); _, ms1, _ = get(u); _, ms2, _ = get(u)
                out.write(json.dumps({"chain": ch, "kind": kind, "address": a, "hops": hops, "window": win, "code": code, "cold_ms": ms0,
                                      "warm_ms": [ms1, ms2], "body": json.loads(body) if body[:1] == b"{" else body.decode()[:200]}) + "\n")
print("done")
