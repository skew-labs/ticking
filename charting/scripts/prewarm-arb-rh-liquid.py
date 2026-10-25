#!/usr/bin/env python3
"""Prewarm em-charts candle LRU for top liquid Arb+RH markets (chart-cache 2026-10-23)."""
import json, urllib.request, time
BASE = "http://127.0.0.1:8895"
def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read())
def main():
    t0 = time.time(); n = 0
    for net in ("robinhood",):
        toks = get(f"/api/public/markets?network={net}")["tokens"]
        toks = sorted(toks, key=lambda t: -(t.get("volume") or 0))
        # prefer recently updated + stocks/anchors
        picks = []
        for t in toks:
            if t.get("kind") in ("stock", "anchor") or (t.get("updated_at") or 0) > time.time() - 7200:
                picks.append(t)
            if len(picks) >= 24:
                break
        if len(picks) < 12:
            picks = toks[:24]
        for t in picks[:24]:
            for iv in ("1m", "1h"):
                url = f"/api/public/engine/candles?network={net}&mint={t['address']}&interval={iv}&limit=180&aggregation=token"
                try:
                    get(url); n += 1
                except Exception as e:
                    print("skip", t.get("symbol"), iv, e)
    print(json.dumps({"prewarmed": n, "seconds": round(time.time() - t0, 2)}))
if __name__ == "__main__":
    main()
