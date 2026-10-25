"""One wallet's holdings and swap PnL on every chain with trader analytics, in one read (wallet-pnl/1) for the explorer wallet page.

GET /wallet-pnl-xc?address=0x…[&chain=arbitrum|robinhood][&limit=]
Source: the trader analytics (em-data/traders/<chain>: wallet_token_pnl, wallet_pnl, trader_labels; rebuilt every 6 h by
em-traders), the same rows em-charts serves per chain at /api/public/em/wallet-pnl. Average cost per wallet and token over decoded
swaps on served markets, attributed to tx.from. A missing price is null, never 0: such a row has no value and no unrealized PnL,
and the totals say how many rows they leave out.
"""
import json
import os
import re
import threading

import duckdb

TRADERS = "/srv/skew/em-data/traders"
CHAINS = ("arbitrum", "robinhood")
ADDR = re.compile(r"0x[0-9a-f]{40}")
_local = threading.local()


def _con():
    c = getattr(_local, "c", None)
    if c is None:
        c = _local.c = duckdb.connect()
        c.execute("SET threads=1")
        c.execute("SET memory_limit='512MB'")
        c.execute("SET enable_object_cache=true")
    return c


def _rows(sql, args):
    cur = _con().execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _r(v, n=2):
    return None if v is None else round(v, n)


def _chain(ch, a, lim):
    T = f"{TRADERS}/{ch}"
    if not os.path.exists(f"{T}/wallet_token_pnl.parquet"):
        return None
    try:
        info = json.load(open(f"{T}/info.json"))
    except (OSError, ValueError):
        info = {}
    toks = _rows(f"""SELECT token, symbol, kind, held, price_now, price_live, avg_cost, swap_inventory, nb, ns, first_buy, first_trade,
                            last_trade, realized_usd, unrealized_usd, pnl_all, pnl_30d, pnl_7d, volume_usd, trips, win_rate, xin_q,
                            transfer_in_usd, transferred_out_q, transfer_adjusted, price_flag, stable
                     FROM read_parquet('{T}/wallet_token_pnl.parquet') WHERE wallet = ? ORDER BY abs(pnl_all) DESC LIMIT ?""", [a, lim])
    w = _rows(f"SELECT * FROM read_parquet('{T}/wallet_pnl.parquet') WHERE wallet = ?", [a])
    labs = []
    if os.path.exists(f"{T}/trader_labels.parquet"):
        labs = _rows(f"SELECT label, symbol, evidence FROM read_parquet('{T}/trader_labels.parquet') WHERE wallet = ?", [a])
    rows = []
    for t in toks:
        priced = t["price_now"] is not None
        held = t["held"] or 0
        why = [k for k, on in (("price_flag", t["price_flag"]), ("transfer_adjusted", t["transfer_adjusted"])) if on]
        value = held * t["price_now"] if priced else None
        rows.append({
            "chain": ch, "token": t["token"], "symbol": t["symbol"], "kind": t["kind"], "market": f"{ch}:{t['token']}",
            "balance": t["held"], "valueUsd": _r(value), "price": t["price_now"],
            "priceBasis": None if not priced else ("last hour's trades" if t["price_live"] else "reference close"),
            # unrealized marks the holding at no more than a quarter of the token's 7-day turnover (min $1k)
            "markCapped": bool(priced and held > 0 and t["avg_cost"] is not None and t["unrealized_usd"] is not None
                               and t["unrealized_usd"] + held * t["avg_cost"] < value - 0.01),
            "firstBuyTs": t["first_buy"], "firstTradeTs": t["first_trade"], "lastTradeTs": t["last_trade"],
            "buys": t["nb"], "sells": t["ns"], "avgCostUsd": t["avg_cost"], "costBasis": "average cost",
            "realizedUsd": _r(t["realized_usd"]), "unrealizedUsd": _r(t["unrealized_usd"]) if priced else None,
            "pnlUsd": _r(t["pnl_all"]) if priced else _r(t["realized_usd"]), "pnl30dUsd": _r(t["pnl_30d"]) if priced else None,
            "pnl7dUsd": _r(t["pnl_7d"]) if priced else None, "volumeUsd": _r(t["volume_usd"]), "roundTrips": t["trips"],
            "winRate": t["win_rate"],
            "transferAdjusted": bool(t["transfer_adjusted"]), "transfersIn": t["xin_q"], "transfersInUsd": _r(t["transfer_in_usd"]),
            "transfersOut": t["transferred_out_q"],
            "verified": not why, "unverifiedReason": why or None, "stable": bool(t["stable"])})
    s = w[0] if w else {}
    return {"chain": ch, "asOf": info.get("now"), "rows": rows,
            "summary": {"realizedUsd": _r(s.get("realized_usd")), "unrealizedUsd": _r(s.get("unrealized_usd")), "pnlUsd": _r(s.get("pnl_all")),
                        "pnl30dUsd": _r(s.get("pnl_30d")), "pnl7dUsd": _r(s.get("pnl_7d")), "volumeUsd": _r(s.get("volume_usd")),
                        "trades": s.get("trades"), "roundTrips": s.get("trips"), "wins": s.get("wins"), "winRate": s.get("win_rate"),
                        "transferAdjusted": bool(s.get("transfer_adjusted")), "botReason": s.get("bot_reason")} if s else None,
            "labels": [{"label": r["label"], "symbol": r["symbol"], "evidence": json.loads(r["evidence"]) if r["evidence"] else None} for r in labs]}


def wallet_pnl_xc(qs):
    a = (qs.get("address") or "").lower()
    if not ADDR.fullmatch(a):
        return 400, {"detail": "address is a 0x wallet address."}
    ch = (qs.get("chain") or "").lower()
    if ch and ch not in CHAINS:
        return 400, {"detail": "chain must be arbitrum or robinhood (the chains with trader analytics)."}
    lim = min(200, max(1, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 100
    per = [p for p in (_chain(c, a, lim) for c in ([ch] if ch else CHAINS)) if p is not None]
    rows = [r for p in per for r in p["rows"]]
    # wallet totals are em-traders' own (price-flagged and stable rows are left out of them); rows without a price are counted apart
    tot = lambda k: _r(sum(p["summary"][k] or 0 for p in per if p["summary"])) if any(p["summary"] for p in per) else None
    unpriced = [r for r in rows if r["price"] is None and (r["balance"] or 0) > 0]
    return 200, {"schema": "wallet-pnl/1", "address": a, "asOf": min((p["asOf"] for p in per if p["asOf"] and (p["rows"] or p["summary"])), default=None),
                 "chains": [{"chain": p["chain"], "asOf": p["asOf"], "summary": p["summary"], "labels": p["labels"], "rows": len(p["rows"])} for p in per],
                 "rows": rows,
                 "totals": {"realizedUsd": tot("realizedUsd"), "unrealizedUsd": tot("unrealizedUsd"), "pnlUsd": tot("pnlUsd"),
                            "volumeUsd": tot("volumeUsd"), "unpricedRows": len(unpriced), "partial": bool(unpriced)},
                 "coverage": {"chains": [p["chain"] for p in per], "without": "the other mapped chains have no trader analytics yet; their "
                              "holdings are in wallet-xc balance sheets (no cost basis)"},
                 "notes": ["PnL is average cost over decoded swaps on served markets, attributed to tx.from. Units received by transfer "
                           "realize nothing; inventory sent away is closed at cost (transferAdjusted).",
                           "balance = the swap-bought units still held: min(swap inventory, balance now + lending collateral).",
                           "price null = no price (pending, cannot estimate): valueUsd and unrealizedUsd are null, never 0.",
                           "verified = false when the token's fills disagree with its reference price (price_flag) or transfers moved "
                           "a large share of the position (transfer_adjusted); unverifiedReason says which.",
                           "markCapped: unrealized PnL marks the holding at no more than a quarter of the token's 7-day turnover "
                           "(min $1k), so a thin market cannot print a large paper gain."]}


ROUTES = {"/wallet-pnl-xc": wallet_pnl_xc}
