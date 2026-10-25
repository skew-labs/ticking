#!/usr/bin/env python3
"""ticking equities reference-price sidecar (:19466). Ref source: Yahoo Finance v8 chart (public, no key, unofficial).
Never presents stale as live: every quote carries ref_ts, session, ref_age_s, is_live."""
import json, os, re, time, threading, urllib.request, urllib.parse, sys
import equity_compare
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
BASE = "/srv/skew/em-charts/data"
OVERLAY = f"{BASE}/category_overlay.json"; MAPF = os.environ.get("EQUITIES_MAP_FILE", f"{BASE}/equities_underlying_map.json")
CHARTS = "http://127.0.0.1:18895/api/public/engine/candles"
MARKETS = "https://api.ticking.dev/api/public/markets?view=table&networks=all"
UA = {"User-Agent": "Mozilla/5.0 (ticking-ref)"}
OVERRIDES = {}  # token symbol -> underlying, when not identical
IVS = {"1m": ("1m", "5d", 60), "5m": ("5m", "1mo", 300), "1h": ("60m", "6mo", 3600), "1d": ("1d", "5y", 86400)}
QUOTE_TTL, SERIES_TTL, MIN_GAP = 20, 120, 0.25
_cache, _lock, _last = {}, threading.Lock(), [0.0]

def http_json(url, timeout=12, headers=UA):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout))

def build_map():
    ov = json.load(open(OVERLAY))["by_id"]
    try:m = json.load(open(MAPF)).get("by_id", {})
    except (OSError, ValueError):m = {}
    for k, v in ov.items():
        if v.get("category") in ("stock", "rwa", "bond"):
            m[k] = dict(id=k, network=v["network"], address=v["address"], symbol=v["symbol"], category=v["category"],
                        underlying=OVERRIDES.get(v["symbol"], v["symbol"]))
    try:
        for t in http_json(MARKETS, headers={})["tokens"]:
            s = t.get("symbol") or ""
            if t.get("network") == "solana" and t.get("category") == "RWA" and re.fullmatch(r"[A-Z]{1,6}x", s):
                k = f"solana:{t['mint']}"
                m[k] = dict(id=k, network="solana", address=t["mint"], symbol=s, category="rwa", underlying=OVERRIDES.get(s, s[:-1]), issuer="xStocks", underlyingIsin=(t.get("underlying") or {}).get("isin"))
    except Exception as e:
        print("markets fetch failed", e)
    out = {"version": 1, "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source_overlay": OVERLAY, "by_id": m}
    tmp = MAPF + ".tmp"; json.dump(out, open(tmp, "w"), indent=1); os.replace(tmp, MAPF)
    return m

MAP = {}
def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl: return hit[1]
    val = fn()
    with _lock: _cache[key] = (time.time(), val)
    return val

def yahoo(sym, iv="1m"):
    yi, rng, _ = IVS[iv]
    def f():
        with _lock:
            wait = _last[0] + MIN_GAP - time.time(); _last[0] = max(time.time(), _last[0] + MIN_GAP)
        if wait > 0: time.sleep(wait)
        u = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval={yi}&range={rng}&includePrePost=true"
        try:
            return {"ok": True, "r": http_json(u)["chart"]["result"][0]}
        except urllib.error.HTTPError as e:
            return {"ok": False, "reason": f"ref_source_http_{e.code}"}
        except Exception as e:
            return {"ok": False, "reason": f"ref_source_error:{type(e).__name__}"}
    return cached(("y", sym, iv), QUOTE_TTL if iv == "1m" else SERIES_TTL, f)

def session_of(meta, now):
    p = meta.get("currentTradingPeriod") or {}
    for name in ("pre", "regular", "post"):
        w = p.get(name) or {}
        if w.get("start", 0) <= now < w.get("end", 0): return name
    return "closed"

def ref_quote(sym):
    y = yahoo(sym, "1m")
    if not y["ok"]: return {"ref_price": None, "reason": y["reason"]}
    r = y["r"]; meta = r["meta"]; now = time.time()
    ts = r.get("timestamp") or []; cl = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    last = next(((t, c) for t, c in zip(reversed(ts), reversed(cl)) if c is not None), None)
    sess = session_of(meta, now)
    if last: ref_ts, ref = last[0], last[1]
    else: ref_ts, ref = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if ref is None: return {"ref_price": None, "reason": "ref_no_price"}
    age = now - ref_ts if ref_ts else None
    live = sess != "closed" and age is not None and age < 300
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    return {"ref_symbol": meta.get("symbol"), "ref_price": ref, "ref_ts": int(ref_ts * 1000) if ref_ts else None, "ref_age_s": round(age) if age is not None else None,
            "session": sess, "is_live": live, "ref_basis": "last_1m_bar_incl_ext_hours" if last else "regularMarketPrice",
            "reason": None if live else ("market_closed" if sess == "closed" else "ref_quote_stale"),
            "research": {"name": meta.get("longName") or meta.get("shortName"), "exchange": meta.get("fullExchangeName"), "instrument": meta.get("instrumentType"),
                         "regular_close": meta.get("regularMarketPrice"), "regular_close_ts": int(meta["regularMarketTime"] * 1000) if meta.get("regularMarketTime") else None,
                         "prev_close": prev, "day_change_pct": round((meta["regularMarketPrice"] / prev - 1) * 100, 3) if prev and meta.get("regularMarketPrice") else None,
                         "day_high": meta.get("regularMarketDayHigh"), "day_low": meta.get("regularMarketDayLow"), "volume": meta.get("regularMarketVolume"),
                         "week52_high": meta.get("fiftyTwoWeekHigh"), "week52_low": meta.get("fiftyTwoWeekLow"), "currency": meta.get("currency")}}

def onchain(ent):
    def f():
        try:
            if ent["network"] == "solana":
                for t in http_json(MARKETS, headers={})["tokens"]:
                    if t.get("mint") == ent["address"]:
                        ts = t.get("updated_at") or t.get("fetched_at")
                        if isinstance(ts, (int, float)) and ts < 1e12: ts = int(ts * 1000)
                        return {"onchain_price": t.get("price"), "onchain_ts": ts, "onchain_source": "ticking markets (" + str(t.get("source")) + ")"}
                return {"onchain_price": None, "onchain_reason": "not_in_markets"}
            q = urllib.parse.urlencode(dict(network=ent["network"], mint=ent["network"]+":"+ent["address"], interval="1m", limit=1, aggregation="token"))
            body = http_json(f"{CHARTS}?{q}", headers={})
            ident = body.get("identity") or {}
            if ident.get("network") != ent["network"] or ident.get("baseMint") != ent["id"] or ident.get("quoteMint") != "USD":
                return {"onchain_price":None,"onchain_reason":"exact_source_identity_mismatch"}
            c = body.get("candles") or []
            if not c: return {"onchain_price": None, "onchain_reason": "no_bars"}
            return {"onchain_price": c[-1]["close"], "onchain_ts": c[-1].get("sourceTime") or c[-1]["time"], "onchain_source": "em-charts 1m token bar close"}
        except Exception as e:
            return {"onchain_price": None, "onchain_reason": f"onchain_error:{type(e).__name__}"}
    return cached(("o", ent["id"]), 10, f)

def entry(ent):
    return equity_compare.compare(ent, sys.modules[__name__])

def resolve(qs):
    ids = [i for i in (qs.get("id", [""])[0]).split(",") if i]
    syms = [s.upper() for s in (qs.get("symbol", [""])[0]).split(",") if s]
    nets = qs.get("network", [None])[0]
    res = [MAP[i] for i in ids if i in MAP]
    for s in syms:
        res += [e for e in MAP.values() if (e["underlying"].upper() == s or e["symbol"].upper() == s) and (not nets or e["network"] == nets)]
    return res[:40]

def series(ent, iv):
    y = yahoo(ent["underlying"], iv)
    if not y["ok"]: return {"series": [], "reason": y["reason"]}
    r = y["r"]; sec = IVS[iv][2]; q = (r.get("indicators", {}).get("quote") or [{}])[0]
    pts = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = (q.get("close") or [None])[i]
        if c is None: continue
        b = t - t % sec if iv != "1d" else t - t % 86400
        pts.append({"time": b * 1000, "open": q["open"][i], "high": q["high"][i], "low": q["low"][i], "close": c, "volume": q["volume"][i]})
    m = r.get("meta", {})
    if iv == "1d" and m.get("regularMarketTime") and m.get("regularMarketPrice"):
        d0 = (m["regularMarketTime"] - m["regularMarketTime"] % 86400) * 1000
        if not pts or pts[-1]["time"] < d0:
            pts.append({"time": d0, "open": None, "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"), "close": m["regularMarketPrice"], "volume": m.get("regularMarketVolume"), "from_meta": True})
    return {"series": pts, "reason": None if pts else "ref_no_bars"}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, cache=10):
        b = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", f"public, max-age={cache}")
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); qs = urllib.parse.parse_qs(u.query); p = u.path.rstrip("/")
        base = "/api/public/equities"
        if p == base + "/healthz": return self.send(200, {"ok": True, "mapped": len(MAP)}, 0)
        if p == base + "/map":
            return self.send(200, {"count": len(MAP), "items": list(MAP.values())}, 300)
        if p == base + "/underlying":
            ids = qs.get("id", [])
            if len(ids) != 1 or not ids[0] or "," in ids[0] or "symbol" in qs:
                return self.send(400, {"detail":"One exact network:contract id required"}, 0)
            try:
                import rwa_underlying
                return self.send(200, rwa_underlying.read_exact(ids[0], sys.modules[__name__]), 0)
            except ValueError as error:
                return self.send(400, {"detail":str(error)}, 0)
        if p == base + "/compare":
            ents = resolve(qs)
            if not ents: return self.send(404, {"detail":"Exact tokenized equity identity not mapped"}, 10)
            if len(ents)>1:return self.send(400, {"detail":"One exact asset per comparison"}, 0)
            return self.send(200, equity_compare.compare(ents[0],sys.modules[__name__]), 0)
        if p == base + "/ref":
            ents = resolve(qs)
            if not ents: return self.send(404, {"detail": "no mapped tokenized equity; pass id=network:address or symbol=SPY"}, 30)
            return self.send(200, {"source": "yahoo_finance_v8_chart (unofficial, no key, may be delayed/rate-limited)", "server_ts": int(time.time() * 1000), "items": [entry(e) for e in ents]}, 10)
        if p == base + "/ref/series":
            ents = resolve(qs); iv = qs.get("interval", ["1m"])[0]
            if iv not in IVS: return self.send(400, {"detail": "interval in 1m,5m,1h,1d"}, 60)
            if not ents: return self.send(404, {"detail": "not mapped"}, 30)
            e = ents[0]; s = series(e, iv)
            return self.send(200, {"id": e["id"], "underlying": e["underlying"], "interval": iv, "time_unit": "ms_bucket_start_utc", **s}, 60)
        return self.send(404, {"detail": "not found"}, 30)

def refresher():
    global MAP
    while True:
        time.sleep(900)
        try: MAP = build_map()
        except Exception as e: print("map refresh failed", e)

if __name__ == "__main__":
    MAP = build_map(); print("mapped", len(MAP), flush=True)
    threading.Thread(target=refresher, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("EQUITIES_PORT", "19466"))), H).serve_forever()
