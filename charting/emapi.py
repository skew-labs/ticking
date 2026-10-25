"""Token-level Economic Machine endpoints (/api/public/em/*) over the precomputed analytics.sqlite3 per chain.

Every response is read from analytics.sqlite3 (written by analytics.refresh every ~10 min) and, for liquidations, combined
with the current price from emread; nothing heavy runs per request.
"""
import json
import os
import sqlite3
import threading
import time
import traceback

from analytics import build_static, d, refresh, res_path

REFRESH_S = 900  # 2026-10-20 owner: analytics every 15 min per chain (was 600, ran back to back and flooded the disk); live candles unaffected
_local = threading.local()


def db(chain):
    cons = getattr(_local, "cons", None)
    if cons is None:
        cons = _local.cons = {}
    c = cons.get(chain)
    if c is None and os.path.exists(res_path(chain)):
        c = cons[chain] = sqlite3.connect(f"file:{res_path(chain)}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    return c


def _one(chain, sql, args):
    c = db(chain)
    if c is None:
        return None
    r = c.execute(sql, args).fetchone()
    return r[0] if r else None


def token_state(chain, market):
    j = _one(chain, "SELECT json FROM token_state WHERE market=?", (market,))
    return json.loads(j) if j else None


def brief(chain, market):
    j = _one(chain, "SELECT json FROM brief WHERE market=?", (market,))
    return json.loads(j) if j else None


def flows(chain, market, window):
    j = _one(chain, "SELECT json FROM flows WHERE market=? AND window=?", (market, window))
    return json.loads(j) if j else None


def events(chain, market, frm, to, limit, kinds=None):
    c = db(chain)
    if c is None:
        return None
    q = ("SELECT time, kind, usd, amount, wallet, wallet_class, tx, block, log_index, protocol, text FROM events "
         "WHERE market=? AND time>=? AND time<=?")
    args = [market, frm, to]
    if kinds:
        q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args += kinds
    q += " ORDER BY time DESC, usd DESC LIMIT ?"
    rows = c.execute(q, args + [limit]).fetchall()
    return [{"time": r[0], "kind": r[1], "usd": r[2], "amount": r[3], "wallet": r[4], "walletClass": r[5], "tx": r[6],
             "block": r[7], "logIndex": r[8], "protocol": r[9], "text": r[10]} for r in rows]


def meta(chain):
    c = db(chain)
    return dict(c.execute("SELECT key, value FROM meta").fetchall()) if c else {}


def liquidations(chain, market, price, symbol, step=0.02, depth=0.5):
    c = db(chain)
    if c is None:
        return None
    rows = c.execute("""SELECT wallet, protocol, position, coll_amount, debt_usd, debt_symbol, threshold, threshold_kind, liq_price
                        FROM liq_positions WHERE market=?""", (market,)).fetchall()
    out = {"market": market, "symbol": symbol, "price": price}
    if price is None:
        out.update(ladder=[], positions=[], atRisk=[], totals={"positions": len(rows)}, note="token is unpriced; no ladder")
        return out
    below, at_risk, deep = [], [], []
    for w, proto, pos, ca, du, ds, th, tk, lp in rows:
        x = {"wallet": w, "protocol": proto, "position": pos, "liqPrice": lp, "distancePct": (lp / price - 1) * 100,
             "collateralAmount": ca, "collateralUsd": ca * price, "debtUsd": du, "debtSymbol": ds, "threshold": th, "thresholdKind": tk}
        (below if lp < price else at_risk if lp <= 1.5 * price else deep).append(x)
    below.sort(key=lambda x: -x["liqPrice"])
    at_risk.sort(key=lambda x: x["liqPrice"])
    nb = int(round(depth / step))
    bins = [{"price": price * (1 - i * step), "priceLow": price * (1 - (i + 1) * step), "collateralAmount": 0.0, "collateralUsd": 0.0,
             "debtUsd": 0.0, "positions": 0} for i in range(nb)]
    rest = {"price": price * (1 - depth), "priceLow": 0.0, "collateralAmount": 0.0, "collateralUsd": 0.0, "debtUsd": 0.0, "positions": 0}
    for x in below:
        i = int((1 - x["liqPrice"] / price) / step)
        b = bins[i] if i < nb else rest
        b["collateralAmount"] += x["collateralAmount"]
        b["collateralUsd"] += x["collateralUsd"]
        b["debtUsd"] += x["debtUsd"] or 0
        b["positions"] += 1
    cum = 0.0
    ladder = []
    for b in bins + [rest]:
        cum += b["collateralUsd"]
        b["cumulativeUsd"] = cum
        ladder.append(b)
    out.update(
        ladder=ladder, positions=below[:50], atRisk=at_risk[:50],
        totals={"positions": len(below), "debtUsd": sum(x["debtUsd"] or 0 for x in below),
                "collateralAmount": sum(x["collateralAmount"] for x in below), "collateralUsd": sum(x["collateralUsd"] for x in below),
                "atRiskPositions": len(at_risk), "atRiskCollateralUsd": sum(x["collateralUsd"] for x in at_risk),
                "excludedDeepUnderwater": len(deep), "excludedDeepUnderwaterDebtUsd": sum(x["debtUsd"] or 0 for x in deep)},
        coverage={"notes": [
            "Morpho Blue (and forks): liq price = debt x loan price / (collateral x LLTV); LLTV from CreateMarket, per market",
            "Aave v3 (and forks): cross-collateral; liq price of this token holding the wallet's other collateral and debt fixed, "
            "LT read on-chain (Pool.getConfiguration); wallets with unpriced collateral or debt only in this same token are left out "
            "(their liquidation does not depend on this token's USD price); Aave E-mode (higher LT for correlated assets) is not applied",
            "positions = econ positions + lending events since; interest accrual is not tracked (debt is a lower bound)",
            "excluded: econ-flagged positions (unverified_principal, morpho_interest_runaway, reserve_unbacked, decoded_exceeds_onchain), dust (< $10 collateral), "
            "and positions more than 1.5x past liquidation (stale or bad debt), counted in totals.excludedDeepUnderwater",
            "atRisk: at or past liquidation by up to 1.5x (Morpho uses its oracle price, which can differ from this DEX price)",
            "ladder: bins of 2% of the current price down to -50%; last bin = everything lower"],
            "unpricedShare": 0.0})
    return out


_ONE_AT_A_TIME = threading.Lock()  # chains refresh in turn, so analytics never takes more than 2 DuckDB threads on skew


class AnalyticsJob(threading.Thread):
    """Builds the static layer when missing or older than the deep DB, then refreshes every REFRESH_S seconds."""

    def __init__(self, chain, reader, log=print):
        super().__init__(daemon=True, name=f"analytics-{chain}")
        self.chain, self.reader, self.log = chain, reader, log
        self.last = None
        self.errors = 0

    def run(self):
        time.sleep(30)
        while True:
            _ONE_AT_A_TIME.acquire()
            t = time.time()
            try:
                st = f"{d(self.chain)}/static.json"
                built = int(self.reader.info().get("built_at", 0))
                if not os.path.exists(st) or json.load(open(st)).get("built_at", 0) < built:
                    build_static(self.chain)
                refresh(self.chain, self.reader, log=self.log)
                self.last = {"at": int(t), "seconds": round(time.time() - t, 1)}
            except Exception:
                self.errors += 1
                self.log(f"analytics {self.chain} failed:\n" + traceback.format_exc())
            finally:
                _ONE_AT_A_TIME.release()
            time.sleep(max(60, REFRESH_S - (time.time() - t)))
