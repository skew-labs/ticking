"""Directory read routes of the em-investigate service: protocols per chain, one protocol, and the cross-token smart-money board.
The protocol numbers are precomputed in the em-charts refresh (directory_core.py -> analytics.sqlite3 table 'directory'); the smart
money comes from the trader analytics (em-data/traders/<chain>: smart_money, wallet_pnl, wallet_token_pnl, trader_labels).
"""
import json
import os
import sqlite3
import threading
import time

import duckdb

EMC = os.environ.get("EM_CHARTS_ROOT", "/srv/skew/em-charts")
SAFE_CANARY = os.environ.get("EM_API_SAFE_CANARY", "0") == "1"
CANARY_ROW_BYTES = 4 << 20


class CanaryBlocked(Exception):
    pass

TRADERS = os.environ.get("EM_DATA_ROOT", "/srv/skew/em-data") + "/traders"
REGISTRY_FILE=os.environ.get("EM_CHAIN_REGISTRY", "/srv/skew/em-ticking-bridge/registry/chains.json")
with open(REGISTRY_FILE) as f:
    CHAINS=tuple(c["id"] for c in json.load(f)["chains"])
DEPLOYMENTS_FILE=os.environ.get("EM_PROTOCOL_DEPLOYMENTS", os.path.join(os.path.dirname(__file__), "protocol_deployments.json"))

def deployment_directory(ch):
    try:
        with open(DEPLOYMENTS_FILE) as f:
            return json.load(f).get("chains", {}).get(ch)
    except (OSError, ValueError):
        return None

SORT = {"liquidity": "liquidityUsd", "volume_24h": "volume24hUsd", "volume_7d": "volume7dUsd", "users_7d": "users7d"}
WINDOW = {"7d": ("pnl_7d", "volume_7d", "trades_7d", "n7"), "30d": ("pnl_30d", "volume_30d", "trades_30d", "n30"),
          "all": ("pnl_all", "volume_usd", "trades", "trades")}
SM_RULE = ("smart money = profitable across >= 2 tokens, >= 10 closed round trips, win rate >= 55%, PnL >= $10K and ROI >= 10%, mostly "
           "realized, no single trade > 50% of PnL, not losing in the last 30 days; ranked by the window's swap PnL (average cost)")
_cache, _lock = {}, threading.Lock()


def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    with _lock:
        _cache[key] = (time.time(), v)
    return v


def chain_of(qs):
    ch = (qs.get("chain") or "").lower()
    if ch not in CHAINS:
        return None, (400, {"detail": "Choose a registered Economic Machine chain."})
    return ch, None


def row(ch, key):
    if not os.path.isfile(f"{EMC}/data/{ch}/analytics/analytics.sqlite3"):
        return None, None
    db = sqlite3.connect(f"file:{EMC}/data/{ch}/analytics/analytics.sqlite3?mode=ro", uri=True, timeout=2 if SAFE_CANARY else 10)
    try:
        if SAFE_CANARY:
            deadline=time.monotonic()+1
            db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
            r=db.execute("SELECT substr(json,1,?),updated_at FROM directory WHERE key=?",(CANARY_ROW_BYTES+1,key)).fetchone()
            if r and len(r[0].encode("utf-8"))>CANARY_ROW_BYTES:
                raise CanaryBlocked("directory_row_limit")
        else:
            r = db.execute("SELECT json, updated_at FROM directory WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        if SAFE_CANARY:raise CanaryBlocked("directory_read_budget")
        r = None
    finally:
        db.close()
    return (json.loads(r[0]), r[1]) if r else (None, None)


def protocols(qs):
    ch, err = chain_of(qs)
    if err:
        return err
    sort = (qs.get("sort") or "volume_7d").lower()
    if sort not in SORT:
        return 400, {"detail": "sort must be liquidity, volume_24h, volume_7d or users_7d."}
    lim = min(100, max(1, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 50
    d, upd = row(ch, "protocols")
    fallback = not bool(d)
    if fallback:
        d=deployment_directory(ch) or {"rows":[], "asOf":None}
        upd=d.get("builtAt")
    k = SORT[sort]
    rows = sorted(d["rows"], key=lambda r: (r.get(k) is None, -(r.get(k) or 0)))
    return 200, {"schema": "protocols/1", "chain": ch, "asOf": d["asOf"], "builtAt": upd, "sort": sort, "total": len(rows), "rows": rows[:lim],
                 "reconciliation": d.get("reconciliation"),
                 "coverage": {"directory": True, "metrics": not fallback, "basis": "published_deployments" if fallback else "computed_analytics"},
                 "notes": (["Verified deployment contracts from the protocol's published registry. This is not a complete activity ranking. Liquidity, volume and active wallets are unavailable until chain analytics are published; no zero values are inferred."] if fallback else ["One row per protocol entity (venues with the same evidence-backed name are grouped).",
                           "liquidityUsd is null when it cannot be computed reliably; liquidity.reason says why. DEX liquidity = tokens held by "
                           "the pool contracts at one block; lending = supplied + collateral - borrowed at the map snapshot.",
                           "volume: DEX = swap volume of Ticking-served tokens, each swap once; others = money moved through decoded actions."])}


def protocol(qs):
    ch, err = chain_of(qs)
    if err:
        return err
    pid = (qs.get("id") or "").lower()
    d, upd = row(ch, "protocol:" + pid)
    if not d:
        found=deployment_directory(ch) or {}
        d=next((r for r in found.get("rows", []) if r.get("id")==pid),None)
        upd=found.get("builtAt")
    if not d:
        return 404, {"detail": "Unknown protocol id for this chain; ids come from /protocols."}
    return 200, {**d, "builtAt": upd}


def smart_money(qs):
    ch = (qs.get("chain") or "").lower()
    if ch not in CHAINS:
        return 400, {"detail": "Choose a registered Economic Machine chain."}
    T = f"{TRADERS}/{ch}"
    if not os.path.exists(f"{T}/smart_money.parquet"):
        return 404, {"detail": f"Trader analytics are not built for {ch} yet."}
    win = (qs.get("window") or "30d").lower()
    if win not in WINDOW:
        return 400, {"detail": "window must be 7d, 30d or all."}
    lim = min(100, max(1, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 25
    return 200, cached(f"sm:{ch}:{win}:{lim}", 600, lambda: _smart(ch, T, win, lim))


def _smart(ch, T, win, lim):
    if SAFE_CANARY:raise CanaryBlocked("smart_money_scan")
    pcol, vcol, ncol, tcol = WINDOW[win]
    c = duckdb.connect()
    c.execute("SET threads=2")
    c.execute("SET memory_limit='1GB'")
    q = lambda sql, a=(): [dict(zip([d[0] for d in cur.description], r)) for cur in [c.execute(sql, list(a))] for r in cur.fetchall()]
    rows = q(f"SELECT * FROM '{T}/smart_money.parquet' WHERE {ncol} > 0 ORDER BY {pcol} DESC LIMIT ?", [lim])
    W = [r["wallet"] for r in rows]
    ph = ",".join("?" * len(W)) or "NULL"
    labels, toks = {}, {}
    for r in q(f"SELECT wallet, label, symbol FROM '{T}/trader_labels.parquet' WHERE wallet IN ({ph})", W):
        labels.setdefault(r["wallet"], []).append(r["label"] + (":" + r["symbol"] if r["symbol"] else ""))
    for r in q(f"""SELECT wallet, mid, token, symbol, kind, {pcol} AS pnl, {'volume_usd' if win == 'all' else vcol} AS vol, trips
                   FROM '{T}/wallet_token_pnl.parquet' WHERE wallet IN ({ph}) AND {tcol} > 0""", W):
        toks.setdefault(r["wallet"], []).append(r)
    try:
        asof = json.load(open(f"{T}/info.json")).get("now")
    except (OSError, ValueError):
        asof = None
    out = []
    for r in rows:
        a, pnl = r["wallet"], r[pcol] or 0
        tk = toks.get(a, [])
        pick = [t for t in tk if t["kind"] != "anchor"] or tk   # open a token or stock market, not WETH / a stablecoin, when there is one
        main = max(pick, key=lambda t: t["vol"] or 0) if pick else None
        flags = []
        if (r["max_single_gain"] or 0) > 0.5 * max(pnl, 1e-9):
            flags.append("max_single_gain")
        if (r["borrows"] or 0) > 0 or (r["debt_usd"] or 0) > 0 or any(l.startswith("leveraged_trader") for l in labels.get(a, [])):
            flags.append("leveraged")
        if r["transfer_adjusted"]:
            flags.append("transfer_adjusted")
        out.append({"wallet": a, "id": f"{ch}:w:{a}", "entity_id": "addr:" + a, "labels": labels.get(a, []),
                    "pnlUsd": round(pnl, 2), "pnlAllUsd": round(r["pnl_all"] or 0, 2), "realizedUsd": round(r["realized_usd"] or 0, 2),
                    "unrealizedUsd": round(r["unrealized_usd"] or 0, 2), "volumeUsd": round(r[vcol] or 0, 2), "roi": r["roi"],
                    "winRate": r["win_rate"], "roundTrips": r["trips"], "tokens": r["tokens"], "maxSingleGainUsd": round(r["max_single_gain"] or 0, 2),
                    "flags": flags,
                    "topTokens": [{"symbol": t["symbol"], "market": f"{ch}:{t['token']}", "kind": t["kind"], "pnlUsd": round(t["pnl"] or 0, 2)}
                                  for t in sorted(tk, key=lambda t: -(t["pnl"] or 0))[:3]],
                    "mainMarket": {"market": f"{ch}:{main['token']}", "symbol": main["symbol"], "kind": main["kind"], "volumeUsd": round(main["vol"] or 0, 2)} if main else None,
                    "firstTrade": r["first_trade"], "lastTrade": r["last_trade"]})
    return {"schema": "smart-money/1", "chain": ch, "window": win, "asOf": asof, "rule": SM_RULE, "rows": out,
            "notes": ["PnL is swap PnL at average cost; transfers in realize nothing, transfers out close at cost (flag transfer_adjusted).",
                      "max_single_gain: one trade is more than half of the window's PnL. leveraged: the wallet borrowed or holds debt.",
                      "mainMarket = the token it traded most in the window; open it in the workspace with that market id."]}


ROUTES = {"/protocols": protocols, "/protocol": protocol, "/smart-money": smart_money}
