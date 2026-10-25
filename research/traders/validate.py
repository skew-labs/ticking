#!/usr/bin/env python3
"""Independent hand-check of one wallet x token PnL row: raw fills -> USD -> average-cost ledger in plain Python.

usage: validate.py <chain> <wallet 0x..> <token 0x.. | SYMBOL> [--show 12]
Reads the same raw inputs as traders.py (wallet index + recent trade log, anchor_px, block minutes) but recomputes
everything here, then prints the arithmetic next to the stored wallet_token_pnl row.
"""
import argparse
import json

import duckdb

EM, CHARTS = "/srv/skew/em-data", "/srv/skew/em-charts"
QK = {"usd": 0, "eth": 1, "btc": 2, "arb": 3}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chain")
    ap.add_argument("wallet")
    ap.add_argument("token")
    ap.add_argument("--show", type=int, default=12)
    a = ap.parse_args()
    w = a.wallet.lower()
    c = duckdb.connect()
    c.execute(f"ATTACH '{CHARTS}/data/{a.chain}/candles.duckdb' AS d (READ_ONLY)")
    mk = c.execute(f"""SELECT w.mid, w.token, m.symbol FROM read_parquet('{CHARTS}/data/{a.chain}/wallet/markets.parquet') w
                       LEFT JOIN d.markets m ON m.token = w.token WHERE w.token = ? OR upper(m.symbol) = upper(?)
                       ORDER BY m.usd_volume DESC NULLS LAST LIMIT 1""", [a.token.lower(), a.token]).fetchone()
    mid, token, symbol = mk
    stored = c.execute(f"SELECT * FROM read_parquet('{EM}/traders/{a.chain}/wallet_token_pnl.parquet') WHERE wallet = ? AND token = ?", [w, token])
    cols = [d[0] for d in stored.description]
    row = stored.fetchone()
    srow = dict(zip(cols, row)) if row else {}
    info = json.load(open(f"{EM}/traders/{a.chain}/info.json"))
    now = info["now"]
    h = w[2:]
    w1, w2, w3 = int(h[:16], 16), int(h[16:32], 16), int(h[32:], 16)
    kinds = " ".join(f"WHEN '{k}' THEN {v}" for k, v in QK.items())
    # index fills: block -> minute, quote -> USD at the minute (independent of traders.py's SQL)
    idx = c.execute(f"""
      WITH t AS (SELECT bn, log_index, buy, base_amt::DOUBLE AS q, quote_amt::DOUBLE AS qa, qk
                 FROM read_parquet('{CHARTS}/data/{a.chain}/wallet/trades_b={h[0]}.parquet') WHERE w1 = ? AND w2 = ? AND w3 = ? AND mid = ?),
      bm AS (SELECT minute::BIGINT AS minute, first_block FROM read_parquet('{CHARTS}/data/block_minutes_{a.chain}.parquet') WHERE minute > 0),
      ax AS (SELECT (CASE kind {kinds} END)::UTINYINT AS qk, minute::BIGINT AS minute, px FROM d.anchor_px UNION ALL SELECT 0, 0, 1.0),
      tm AS (SELECT t.*, bm.minute AS ts FROM t ASOF JOIN bm ON t.bn >= bm.first_block)
      SELECT tm.ts, tm.bn, tm.log_index, tm.buy, tm.q, tm.qa * ax.px AS usd, 'index' AS src
      FROM tm ASOF JOIN ax ON ax.qk = tm.qk AND tm.ts >= ax.minute ORDER BY tm.ts, tm.bn, tm.log_index""", [w1, w2, w3, mid]).fetchall()
    rec = c.execute(f"""SELECT time, slot, event_order, side = 'B', base_volume, usd, 'recent' FROM read_parquet('{EM}/traders/{a.chain}/log/recent_trades.parquet')
                        WHERE trader = ? AND token = ? ORDER BY time, slot, event_order""", [w, token]).fetchall()
    fills = sorted(idx + rec, key=lambda r: (r[0], r[1], r[2]))
    # same-block buy+sell pairs are wash (both legs dropped), as in traders.py
    sides = {}
    for f in fills:
        sides.setdefault(f[1], set()).add(f[3])
    wash = {b for b, s in sides.items() if len(s) == 2}
    # bad prints: same hourly band test as traders.py
    bars = c.execute("""SELECT time // 1000 AS ht, low, high, close FROM d.token_bars WHERE token = ? AND period = 3600 ORDER BY 1""", [token]).fetchall()
    kind = c.execute("SELECT kind FROM d.markets WHERE token = ?", [token]).fetchone()[0]
    f_band = {"stock": 1.25, "anchor": 1.10}.get(kind, 1.5)
    reach = 21600 if kind in ("stock", "anchor") else 3600
    import bisect
    hts = [b[0] for b in bars]

    def glitch(ts, q, usd):
        if usd > 20e6:
            return True
        i = bisect.bisect_right(hts, ts) - 1
        if i < 0:
            return False
        ht, lo, hi, cl = bars[i]
        p = usd / q
        if ts - ht < reach:
            return p > hi * f_band or p < lo / f_band
        return p > cl * 10 or p < cl / 10

    qty = cost = real = real30 = real7 = peak = 0.0
    bq = bu = sq = su = xin_q = xin_u = 0.0
    nb = ns = ng = 0
    t30, t7 = now - 30 * 86400, now - 7 * 86400
    shown = []
    for ts, bn, li, buy, q, usd, src in fills:
        if bn in wash:
            continue
        if not (q and q > 0 and usd and usd > 0):
            continue
        if glitch(ts, q, usd):
            ng += 1
            continue
        if buy:
            if qty <= 0:
                peak = 0.0
            qty += q; cost += usd; bq += q; bu += usd; nb += 1
            peak = max(peak, qty)
            step = f"buy  {q:,.4f} for ${usd:,.2f} (px {usd / q:,.4f}) -> inventory {qty:,.4f}, avg {cost / qty:,.4f}"
        else:
            ns += 1; sq += q; su += usd
            cov = min(q, qty)
            r = 0.0
            if cov > 0:
                avg = cost / qty
                r = usd * cov / q - cov * avg
                real += r
                real30 += r if ts >= t30 else 0
                real7 += r if ts >= t7 else 0
                cost -= cov * avg; qty -= cov
                if qty <= 1e-5 * peak or qty <= 0:  # dust left by float32 amounts closes the position (as the kernel)
                    qty = cost = 0.0
            if q > cov:
                xin_q += q - cov; xin_u += usd * (q - cov) / q
            step = f"sell {q:,.4f} for ${usd:,.2f} (px {usd / q:,.4f}) covered {cov:,.4f} realized ${r:,.2f} -> inventory {qty:,.4f}"
        shown.append(f"  {ts} {src:6s} {step}")
    print(f"{a.chain} {w} {symbol} ({token}) kind={kind}")
    print(f"raw fills {len(fills)} (index {len(idx)}, recent {len(rec)}); wash blocks dropped {len(wash)}; bad prints dropped {ng}")
    for s in shown[:a.show]:
        print(s)
    if len(shown) > a.show:
        print(f"  ... {len(shown) - a.show} more; last:")
        for s in shown[-3:]:
            print(s)
    avg = cost / qty if qty > 0 else None
    held = srow.get("held")
    px = srow.get("price_now")
    unreal = (held * (px - avg)) if held and avg is not None and px else 0.0
    print(f"buys  {nb}: {bq:,.4f} units for ${bu:,.2f}")
    print(f"sells {ns}: {sq:,.4f} units for ${su:,.2f}; from transfers (no PnL) {xin_q:,.4f} units / ${xin_u:,.2f}")
    print(f"realized = sum(proceeds of covered units - units x avg cost) = ${real:,.2f} (30d ${real30:,.2f}, 7d ${real7:,.2f})")
    print(f"inventory {qty:,.4f} @ avg ${avg if avg is None else round(avg, 6)}; held now (min with balance+collateral) {held}; price now {px}")
    print(f"unrealized = held x (price now - avg) = ${unreal:,.2f}; total = ${real + unreal:,.2f}")
    if srow:
        print("stored:", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in srow.items()
                          if k in ("nb", "ns", "buy_usd", "sell_usd", "realized_usd", "realized_30d", "realized_7d", "swap_inventory", "avg_cost",
                                   "held", "unrealized_usd", "pnl_all", "pnl_30d", "transfer_in_usd", "glitches", "wash_rows", "trips", "wins")})
        d = abs(srow["realized_usd"] - real)
        print(f"realized match: |stored - recomputed| = ${d:,.4f} ({'OK' if d <= max(0.01, 1e-6 * abs(real)) else 'MISMATCH'})")


if __name__ == "__main__":
    main()
