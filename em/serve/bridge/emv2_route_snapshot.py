#!/usr/bin/env python3
"""Read-only snapshot of the EM routes the EMV2 block wraps, straight from a bridge port (GET only, a handful of requests):
status and top-level JSON keys per route. With --compare, exit 1 when any route's status or keys changed; an upstream timeout
(502/503/504 on either side) is reported and not counted, since it is the upstream's state, not the bridge's.
usage: emv2_route_snapshot.py <port> <out.json> [--compare <before.json>]"""
import http.client
import json
import sys

W = "0x0e6d23f0babd02ede4aefaa923486591d783e118"   # a CASHCAT wallet (home bundle)
M = "robinhood:0x020bfc650a365f8bb26819deaabf3e21291018b4"
ROUTES = ["/api/public/em/health", "/api/public/em/chains",
          "/api/public/em/tools/wallet?address=%s&chain=robinhood" % W, "/api/public/em/tools/trace?address=%s&chain=robinhood" % W,
          "/api/public/em/tools/holders?market=%s" % M, "/api/public/em/trace?chain=robinhood&from=%s" % W,
          "/api/public/em/address-chains?address=%s" % W, "/api/public/em/holders-at?market=%s&limit=5" % M,
          "/api/public/em/wallet-trades?address=%s&market=%s" % (W, M), "/api/public/em/acquisition?address=%s&market=%s" % (W, M)]
FLAKY = {502, 503, 504}


def snap(port):
    out = {}
    for t in ROUTES:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=40)
        try:
            c.request("GET", t, headers={"Accept": "application/json"})
            r = c.getresponse()
            body = r.read()
            try:
                d = json.loads(body)
                keys = sorted(d)[:40] if isinstance(d, dict) else ["list:%d" % len(d)]
            except ValueError:
                keys = ["not-json"]
            out[t] = {"code": r.status, "keys": keys}
        except OSError as e:
            out[t] = {"code": 0, "keys": [type(e).__name__]}
        finally:
            c.close()
        print("%3d %-70s %s" % (out[t]["code"], t.split("?")[0], ",".join(out[t]["keys"])[:70]))
    return out


def main():
    port, path = int(sys.argv[1]), sys.argv[2]
    now = snap(port)
    with open(path, "w") as f:
        json.dump(now, f, indent=1)
    if "--compare" not in sys.argv:
        return 0
    before = json.load(open(sys.argv[sys.argv.index("--compare") + 1]))
    bad = 0
    for t in ROUTES:
        a, b = before.get(t, {}), now[t]
        if {a.get("code"), b["code"]} & FLAKY or a.get("code") == 0:
            if a != b:
                print("upstream state, not counted: %s %s -> %s" % (t.split("?")[0], a.get("code"), b["code"]))
            continue
        if a != b:
            bad += 1
            print("CHANGED %s: %s %s -> %s %s" % (t.split("?")[0], a.get("code"), a.get("keys"), b["code"], b["keys"]))
    print("EM routes: %s" % ("unchanged" if not bad else "%d changed" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
