#!/usr/bin/env python3
"""Reconcile decoded lending positions with the tokens the lending contracts hold on chain.

usage: reconcile_lending.py <chain> <sheet_rows.parquet> <reserves.json> [--protocols p1,p2] [--min-usd 100000] [--all-quality]

Decoded side: sheet rows (econ v3 or a dev run), per (protocol, asset): supplied + collateral - debt, in token units.
On-chain side: reserves.json "lenders" (em-charts reserves.py: balanceOf of every Comet, aToken, Morpho Blue singleton and
cToken for the assets Ticking prices), summed per (protocol, asset). By construction the contracts hold supplied + collateral
- borrowed (+ protocol reserves, - bad debt), so the two should agree up to accrued interest and reserves.
Rows flagged unverified_principal / morpho_interest_runaway are excluded unless --all-quality (what em-charts serves).
USD (econ head prices) only ranks and sums; the comparison is in token units.
"""
import argparse
import json

import duckdb

BAD_Q = ("unverified_principal", "morpho_interest_runaway", "reserve_unbacked", "decoded_exceeds_onchain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chain")
    ap.add_argument("rows")
    ap.add_argument("reserves")
    ap.add_argument("--protocols", default="")
    ap.add_argument("--min-usd", type=float, default=100_000)
    ap.add_argument("--all-quality", action="store_true")
    a = ap.parse_args()
    E = f"/srv/skew/em-data/econ/{a.chain}"
    R = json.load(open(a.reserves))
    c = duckdb.connect()
    c.execute("SET threads=4")
    dec = {t: (s, d) for t, s, d in c.execute(f"SELECT lower(token), any_value(symbol), any_value(decimals) FROM '{E}/token_meta.parquet' GROUP BY 1").fetchall()}
    px = {t: float(p) for t, p in c.execute(f"SELECT lower(token), price_usd FROM read_csv('{E}/v3/prices_head.v3.csv', all_varchar=true) "
                                            "WHERE price_usd IS NOT NULL AND price_usd <> ''").fetchall()}
    onc = {}
    for proto, hs in (R.get("lenders") or {}).items():
        for bal in hs.values():
            for t, raw in bal.items():
                if dec.get(t, (None, None))[1] is not None:
                    onc[(proto, t)] = onc.get((proto, t), 0.0) + int(raw) / 10 ** dec[t][1]
    q = "" if a.all_quality else "AND coalesce(quality, '') NOT IN (%s)" % ",".join("'%s'" % b for b in BAD_Q)
    decd = {(p, t): (s or 0, col or 0, d or 0) for p, t, s, col, d in c.execute(f"""
        SELECT protocol, lower(asset), sum(amount) FILTER (WHERE category = 'supplied'), sum(amount) FILTER (WHERE category = 'collateral'),
               sum(amount) FILTER (WHERE category = 'debt')
        FROM read_parquet('{a.rows}') WHERE category IN ('supplied', 'collateral', 'debt') {q} GROUP BY 1, 2""").fetchall()}
    want = set(x for x in a.protocols.split(",") if x)
    prot = {}
    lines = []
    for k in set(onc) | set(decd):
        p, t = k
        if want and p not in want:
            continue
        if k not in onc:          # no on-chain read for this asset (not Ticking-priced, or no known holder)
            continue
        s, col, d = decd.get(k, (0, 0, 0))
        net, held = s + col - d, onc[k]
        price = px.get(t) if (px.get(t) or 0) < 1e6 else None   # econ head prices carry a few absurd values (e.g. a detached LINK feed)
        P = prot.setdefault(p, {"held": 0.0, "net": 0.0, "absdiff": 0.0, "assets": 0})
        if price:
            P["held"] += held * price
            P["net"] += net * price
            P["absdiff"] += abs(net - held) * price
            P["assets"] += 1
        lines.append((p, dec.get(t, ("?",))[0], held, net, s, col, d, price))
    print("per protocol (USD at econ head prices; only assets read on chain):")
    print("%-28s %16s %16s %8s %16s" % ("protocol", "on-chain", "decoded net", "gap %", "sum |asset gap|"))
    for p, P in sorted(prot.items(), key=lambda x: -x[1]["held"]):
        if P["held"] < a.min_usd and P["net"] < a.min_usd:
            continue
        gap = 100 * (P["net"] - P["held"]) / P["held"] if P["held"] else None
        print("%-28s %16s %16s %8s %16s" % (p[:28], "{:,.0f}".format(P["held"]), "{:,.0f}".format(P["net"]),
                                            "%.1f" % gap if gap is not None else "-", "{:,.0f}".format(P["absdiff"])))
    print("\nlargest asset gaps (token units):")
    print("%-24s %-10s %16s %16s %8s %14s %14s %14s" % ("protocol", "asset", "on-chain", "decoded net", "gap %", "supplied", "collateral", "debt"))
    for p, sym, held, net, s, col, d, price in sorted(lines, key=lambda x: -abs(x[3] - x[2]) * (x[7] or 0))[:20]:
        gap = 100 * (net - held) / held if held else None
        print("%-24s %-10s %16s %16s %8s %14s %14s %14s" % (p[:24], (sym or "?")[:10], "{:,.2f}".format(held), "{:,.2f}".format(net),
                                                          "%.1f" % gap if gap is not None else "-", "{:,.2f}".format(s), "{:,.2f}".format(col), "{:,.2f}".format(d)))


if __name__ == "__main__":
    main()
