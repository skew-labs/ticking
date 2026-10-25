"""Money-flow map reads for em-charts (/api/public/em/flowmap, /api/public/em/flowmap/wallet). Precomputed by flowmap.build in the
analytics refresh (analytics.sqlite3 tables flowmap and flow_pools); the wallet view reads the trade tape (deep + hot) for one sender."""
import json
import threading
import time

from analytics import res_path

WIN = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
_local = threading.local()
_sym = {}


def _db(chain):
    import sqlite3
    cons = getattr(_local, "cons", None)
    if cons is None:
        cons = _local.cons = {}
    c = cons.get(chain)
    if c is None:
        try:
            c = cons[chain] = sqlite3.connect(f"file:{res_path(chain)}?mode=ro", uri=True, timeout=5, check_same_thread=False)
        except sqlite3.Error:
            return None
    return c


def _row(chain, key):
    c = _db(chain)
    if c is None:
        return None
    try:
        r = c.execute("SELECT json FROM flowmap WHERE window = ?", (key,)).fetchone()
    except Exception:   # the table appears after the first refresh with the flow map
        return None
    return json.loads(r[0]) if r else None


def flowmap(chain, window):
    return _row(chain, window)


def symbols(ch):
    s = _sym.get(ch.name)
    if s is None or s[0] < time.time() - 300:
        s = _sym[ch.name] = (time.time(), {m["token"]: m["symbol"] for m in ch.markets()})
    return s[1]


def wallet(ch, address, window):
    """DEX venues one wallet used in the window (tape rows where it is the transaction sender), as flow-map nodes."""
    lo = int(time.time()) - WIN[window]
    venues = _row(ch.name, "venues") or {}
    rows = []
    cut = int(ch.deep_until_ms() // 1000)
    con = ch.con.cursor()
    try:
        rows += con.execute("""SELECT pool, token, side, sum(usd), count(*) FROM tape WHERE trader = from_hex(?) AND time >= ? AND time < ?
                               GROUP BY ALL""", [address[2:], lo, cut]).fetchall()
    finally:
        con.close()
    h = ch.hot() if hasattr(ch, "hot") else None
    if h is not None:
        rows += h.execute("""SELECT f.pool, m.token, m.side, sum(m.usd), count(*) FROM fill_meta m JOIN fills f ON f.identity = m.identity
                             WHERE m.trader = ? AND f.time >= ? GROUP BY f.pool, m.token, m.side""", (address, max(lo, cut))).fetchall()
    pools = {}
    c = _db(ch.name)
    if c is not None and rows:
        ps = sorted({r[0] for r in rows if r[0]})
        try:
            for i in range(0, len(ps), 500):
                part = ps[i:i + 500]
                pools.update(dict(c.execute("SELECT pool, venue FROM flow_pools WHERE pool IN (%s)" % ",".join("?" * len(part)), part).fetchall()))
        except Exception:
            pass
    sym = symbols(ch)
    agg = {}
    for pool, token, side, usd, n in rows:
        key = pools.get(pool, "dex_other")
        v = venues.get(key) or {"node": "v:other-dexs", "name": "Other DEXs", "kind": "dex"}
        a = agg.setdefault(v["node"], {"node": v["node"], "name": v["name"], "kind": v["kind"], "fwdUsd": 0.0, "backUsd": 0.0, "events": 0, "tokens": {}})
        usd = usd or 0.0
        a["fwdUsd" if side == "B" else "backUsd"] += usd
        a["events"] += n
        t = a["tokens"].setdefault(token, 0.0)
        a["tokens"][token] = t + usd
    out = []
    for a in agg.values():
        toks = sorted(a.pop("tokens").items(), key=lambda x: -x[1])[:5]
        a["tokens"] = [{"token": t, "symbol": sym.get(t), "usd": round(u, 2)} for t, u in toks]
        a["fwdUsd"], a["backUsd"] = round(a["fwdUsd"], 2), round(a["backUsd"], 2)
        a["usd"] = round(a["fwdUsd"] + a["backUsd"], 2)
        a["type"] = "swap"
        out.append(a)
    out.sort(key=lambda x: -x["usd"])
    return {"chain": ch.name, "address": address, "window": window, "from": lo, "to": int(time.time()), "venues": out[:24],
            "totals": {"usd": round(sum(x["usd"] for x in out), 2), "events": sum(x["events"] for x in out)},
            "coverage": {"notes": ["DEX trades of the served tokens where this wallet is the transaction sender (deep tape + hot fills)"]}}
