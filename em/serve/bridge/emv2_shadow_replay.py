#!/usr/bin/env python3
"""Gentle traffic for the staging shadow: for one chain, take its busiest markets from em-charts, ask the staging bridge for
holders, then for a few holders ask the wallet routes. Every answer comes from the current services as usual; the bridge's
EMV2 block compares in the background when that route and chain are set to shadow. GET only, at most --rps requests a second.
usage: emv2_shadow_replay.py <chain> [--bridge 127.0.0.1:8893] [--charts 127.0.0.1:8895] [--markets 5] [--wallets 4] [--rps 2]"""
import http.client
import json
import re
import sys
import time
import urllib.parse


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def get(hp, target, timeout=40):
    h, p = hp.rsplit(":", 1)
    c = http.client.HTTPConnection(h, int(p), timeout=timeout)
    try:
        c.request("GET", target, headers={"Accept": "application/json"})
        r = c.getresponse()
        body = r.read()
        try:
            return r.status, json.loads(body)
        except ValueError:
            return r.status, None
    except OSError:
        return 0, None
    finally:
        c.close()


def main():
    chain = sys.argv[1]
    bridge, charts = arg("--bridge", "127.0.0.1:8893"), arg("--charts", "127.0.0.1:8895")
    n_mk, n_w, gap = int(arg("--markets", 5)), int(arg("--wallets", 4)), 1.0 / float(arg("--rps", 2))
    _, d = get(charts, "/api/public/markets?" + urllib.parse.urlencode({"network": chain, "limit": 50}))
    toks = [t for t in (d or {}).get("tokens") or [] if re.fullmatch(r"[a-z0-9]+:0x[0-9a-f]{40}", str(t.get("id", "")))]
    toks.sort(key=lambda t: -(t.get("volume") or 0))
    sent, codes = 0, {}

    def hit(target):
        nonlocal sent
        time.sleep(gap)
        code, body = get(bridge, target)
        sent += 1
        codes[code] = codes.get(code, 0) + 1
        print("%3d %s" % (code, target[:150]), flush=True)
        return body

    for t in toks[:n_mk]:
        m = t["id"]
        body = hit("/api/public/em/holders-at?" + urllib.parse.urlencode({"market": m, "limit": 20}))
        hit("/api/public/em/tools/holders?" + urllib.parse.urlencode({"market": m}))
        rows = (body or {}).get("holders") or (body or {}).get("rows") or []
        wallets = [r.get("address") for r in rows if isinstance(r, dict) and re.fullmatch(r"0x[0-9a-f]{40}", str(r.get("address", "")))][:n_w]
        for w in wallets:
            hit("/api/public/em/wallet-trades?" + urllib.parse.urlencode({"address": w, "market": m}))
            hit("/api/public/em/acquisition?" + urllib.parse.urlencode({"address": w, "market": m, "chain": chain}))
            hit("/api/public/em/tools/wallet?" + urllib.parse.urlencode({"address": w, "chain": chain}))
            hit("/api/public/em/tools/trace?" + urllib.parse.urlencode({"address": w, "chain": chain}))
            hit("/api/public/em/trace?" + urllib.parse.urlencode({"chain": chain, "from": w}))
            hit("/api/public/em/trace-graph?" + urllib.parse.urlencode({"chain": chain, "address": w, "hops": 2}))
        if len(wallets) >= 2:
            hit("/api/public/em/trace?" + urllib.parse.urlencode({"chain": chain, "from": wallets[0], "to": wallets[1]}))
    print("sent %d requests for %s (%d markets): %s" % (sent, chain, min(n_mk, len(toks)), codes))


if __name__ == "__main__":
    main()
