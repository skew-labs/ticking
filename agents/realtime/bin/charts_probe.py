"""charts_probe.py <out.json> [--compare before.json]: em-charts (:8895) health, per-chain market counts and one candle
read per chain (code, latency, bar count, keys), for before/after comparison of a charts change. Read only."""
import json, sys, time, urllib.request
H = "http://127.0.0.1:8895"
def get(p, t=30):
    t0 = time.time()
    try:
        with urllib.request.urlopen(H + p, timeout=t) as r:
            return r.status, json.loads(r.read()), round((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, None, round((time.time() - t0) * 1000)
    except Exception as e:
        return 0, str(e)[:80], round((time.time() - t0) * 1000)
out = {}
code, h, ms = get("/api/health")
out["health"] = {"code": code, "ms": ms, "status": (h or {}).get("status") if isinstance(h, dict) else None,
                 "chains": {c: {"markets": v.get("markets"), "status": v.get("status")} for c, v in ((h or {}).get("chains") or {}).items()} if isinstance(h, dict) else None}
for ch in ("arbitrum", "robinhood", "arc", "ethereum", "optimism", "unichain", "linea", "base", "polygon"):
    code, m, ms = get("/api/public/markets?chain=%s&limit=500" % ch)
    toks = [t for t in ((m or {}).get("tokens") or []) if t.get("network") == ch] if isinstance(m, dict) else []
    rec = {"code": code, "ms": ms, "n": len(toks)}
    if toks:
        mid = sorted(toks, key=lambda t: -(t.get("volume") or 0))[0]["id"]
        lat = []
        for iv in ("1m", "1h"):
            c2, cd, ms2 = get("/api/public/engine/candles?aggregation=token&market=%s&interval=%s&limit=200" % (mid, iv))
            bars = (cd or {}).get("candles") if isinstance(cd, dict) else None
            rec[iv] = {"code": c2, "ms": ms2, "bars": len(bars or []), "keys": sorted(cd)[:8] if isinstance(cd, dict) else None}
        rec["market"] = mid
    out[ch] = rec
json.dump(out, open(sys.argv[1], "w"), indent=1)
print(json.dumps({k: (v.get("n"), v.get("1m", {}).get("ms"), v.get("1m", {}).get("bars")) if k != "health" else (v["code"], v["status"]) for k, v in out.items()}))
if "--compare" in sys.argv:
    b = json.load(open(sys.argv[sys.argv.index("--compare") + 1])); bad = []
    for ch, v in b.items():
        if ch == "health":
            continue
        n = out.get(ch) or {}
        if v.get("code") == 200 and n.get("code") != 200: bad.append("%s markets %s -> %s" % (ch, v.get("code"), n.get("code")))
        if (v.get("n") or 0) > (n.get("n") or 0): bad.append("%s markets %s -> %s" % (ch, v.get("n"), n.get("n")))
        for iv in ("1m", "1h"):
            if (v.get(iv) or {}).get("code") == 200 and (n.get(iv) or {}).get("code") != 200: bad.append("%s %s candles fail" % (ch, iv))
            if ((v.get(iv) or {}).get("bars") or 0) > 0 and not ((n.get(iv) or {}).get("bars") or 0): bad.append("%s %s candles empty" % (ch, iv))
    print("compare:", "OK" if not bad else "REGRESSION " + "; ".join(bad))
    sys.exit(1 if bad else 0)
