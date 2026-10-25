"""Top traders and wallet PnL reads for em-charts (/api/public/em/top-traders, /api/public/em/wallet-pnl).

Precomputed every 6 h by /srv/skew/em-traders/traders.py into em-data/traders/<chain>/*.parquet (sorted by token / wallet
with small row groups, so one lookup reads a few KB). PnL is average cost over on-chain swaps on served markets,
attributed to tx.from; units received by transfer realize nothing and inventory sent away is closed at cost.
"""
import json
import os
import re
import threading

import duckdb

from common import EM

DIR = f"{EM}/traders"
WINDOWS = ("7d", "30d", "all")
ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
METHOD = {
    "pnl": "average cost per wallet and token over on-chain swaps on served markets (one side USD / ETH / BTC / ARB), attributed to tx.from",
    "realized": "each sell realizes against the average cost of swap-bought inventory; units received by transfer realize nothing",
    "unrealized": "held x (price now - average cost); held = min(swap inventory, balance now + lending collateral); "
                  "the held value is capped at 25% of the token's 7-day turnover",
    "windows": "7d / 30d = realized on sells inside the window + the share of unrealized PnL bought inside the window",
    "excluded": "MEV bots, routers, pools, exchanges' hot wallets, bridges, contracts and bot-like senders; "
                "fills outside the token's hourly reference band; same-block buy+sell pairs",
}
_local = threading.local()
_info = {}


def _con():
    c = getattr(_local, "c", None)
    if c is None:
        c = _local.c = duckdb.connect()
        c.execute("SET threads=1")
        c.execute("SET memory_limit='512MB'")
        c.execute("SET enable_object_cache=true")  # keep Parquet metadata between lookups
    return c


def _p(chain, name):
    return f"{DIR}/{chain}/{name}.parquet"


def info(chain):
    p = f"{DIR}/{chain}/info.json"
    try:
        m = os.path.getmtime(p)
    except OSError:
        return None
    hit = _info.get(chain)
    if not hit or hit[0] != m:
        hit = _info[chain] = (m, json.load(open(p)))
    return hit[1]


def _rows(sql, args):
    cur = _con().execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _clean(d):
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}


def _labels(chain, wallets):
    if not wallets or not os.path.exists(_p(chain, "trader_labels")):
        return {}
    out = {}
    q = ",".join("?" for _ in wallets)
    for r in _rows(f"SELECT wallet, label, symbol, confidence FROM read_parquet('{_p(chain, 'trader_labels')}') WHERE wallet IN ({q})", list(wallets)):
        out.setdefault(r["wallet"], []).append(r["label"] + (":" + r["symbol"] if r["symbol"] else ""))
    return out


def top_traders(chain, market, window="30d", limit=20):
    """market: em-charts market dict (id, token, symbol, kind)."""
    inf = info(chain)
    if inf is None:
        return 503, {"detail": "Trader analytics are not built yet for this chain; retry later."}
    if window not in WINDOWS:
        return 400, {"detail": "window must be 7d, 30d or all"}
    limit = max(1, min(100, int(limit)))
    rows = _rows(f"""SELECT rank, wallet, pnl_usd, realized_usd, unrealized_usd, volume_usd, trades, roi, win_rate, trips, avg_hold_s,
                            held, avg_cost, price_now, transfer_adjusted, early_score, entry_multiple, max_single_gain,
                            first_trade, last_trade, eligible_traders
                     FROM read_parquet('{_p(chain, 'token_leaderboard')}') WHERE token = ? AND win = ? ORDER BY rank LIMIT ?""",
                 [market["token"], window, limit])
    labels = _labels(chain, {r["wallet"] for r in rows})
    body = {"market": market["id"], "chain": chain, "token": market["token"], "symbol": market["symbol"], "window": window,
            "asOf": inf.get("now"), "builtAt": inf.get("built_at"), "eligibleTraders": rows[0]["eligible_traders"] if rows else 0,
            "traders": [dict(_clean({k: v for k, v in r.items() if k != "eligible_traders"}), labels=labels.get(r["wallet"], []))
                        for r in rows],
            "method": METHOD}
    if not rows:
        st = _rows(f"""SELECT bool_or(price_flag) AS flag, bool_or(stable) AS stable, sum(volume_30d) AS v30, count(*) AS n
                       FROM read_parquet('{_p(chain, 'wallet_token_pnl')}') WHERE token = ?""", [market["token"]])
        s = st[0] if st else {}
        body["reason"] = ("its swap prints disagree with the price reference, so PnL is not reliable" if s.get("flag") else
                          "it is a stable-value asset (PnL is not trading skill)" if s.get("stable") else
                          "not enough eligible traders or volume in this window")
    return 200, body


def wallet_pnl(chain, address, limit=20):
    inf = info(chain)
    if inf is None:
        return 503, {"detail": "Trader analytics are not built yet for this chain; retry later."}
    addr = address.lower()
    if not ADDR_RE.match(addr):
        return 400, {"detail": "address must be a 0x-prefixed 20-byte hex address."}
    w = _rows(f"SELECT * FROM read_parquet('{_p(chain, 'wallet_pnl')}') WHERE wallet = ?", [addr])
    toks = _rows(f"""SELECT token, symbol, kind, pnl_all AS pnl_usd, pnl_30d, pnl_7d, realized_usd, unrealized_usd, volume_usd, trades,
                            win_rate, trips, held, avg_cost, price_now, transfer_adjusted, early_score, entry_multiple, price_flag, stable,
                            first_trade, last_trade
                     FROM read_parquet('{_p(chain, 'wallet_token_pnl')}') WHERE wallet = ?
                     ORDER BY abs(pnl_all) DESC LIMIT ?""", [addr, max(1, min(200, int(limit)))])
    if not w and not toks:
        return 404, {"detail": "No swap PnL for this wallet on this chain: no priced swaps on served markets, or it is a protocol, "
                               "exchange or bot address excluded from trader analytics."}
    labs = _rows(f"SELECT label, symbol, confidence, evidence FROM read_parquet('{_p(chain, 'trader_labels')}') WHERE wallet = ?", [addr])
    return 200, {"chain": chain, "address": addr, "asOf": inf.get("now"), "builtAt": inf.get("built_at"),
                 "summary": _clean(w[0]) if w else None,
                 "tokens": [_clean(t) for t in toks],
                 "labels": [dict(_clean({k: v for k, v in r.items() if k != "evidence"}), evidence=json.loads(r["evidence"]) if r["evidence"] else None)
                            for r in labs],
                 "method": METHOD}
