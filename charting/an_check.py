"""Print an analytics summary for one market (dev helper): python an_check.py <chain> <market id> [wallet]"""
import json
import sqlite3
import sys

chain, m = sys.argv[1], sys.argv[2]
w = sys.argv[3] if len(sys.argv) > 3 else None
c = sqlite3.connect(f"file:/srv/skew/em-charts/data/{chain}/analytics/analytics.sqlite3?mode=ro", uri=True)
s = json.loads(c.execute("SELECT json FROM token_state WHERE market=?", (m,)).fetchone()[0])
print("price", s["price"], "supply", round(s["supply"], 1), "holders", s["holders"], "attributed", round(s["coverage"]["attributedShare"], 4))
for b in s["buckets"]:
    print("  %-22s amt %14.1f usd %14s share %5.1f%% wallets %s d24h %s d7d %s %s" % (
        b["key"], b["amount"], "%.0f" % b["usd"] if b["usd"] is not None else "-", b["share"] * 100, b["wallets"],
        None if b["d24h"] is None else round(b["d24h"], 2), None if b["d7d"] is None else round(b["d7d"], 2),
        [(p.get("name"), round(p["amount"], 1)) for p in b["protocols"]][:4]))
print("top", [(h["address"][:10], round(h["amount"]), h["bucket"], h["entity"]) for h in s["topHolders"][:6]])
print("liq rows", c.execute("SELECT count(*), sum(debt_usd), sum(coll_amount) FROM liq_positions WHERE market=?", (m,)).fetchone())
if w:
    for r in c.execute("SELECT * FROM liq_positions WHERE market=? AND wallet=?", (m, w)):
        print("  wallet", r)
for r in c.execute("SELECT wallet, protocol, coll_amount, debt_usd, debt_symbol, threshold, liq_price FROM liq_positions WHERE market=? ORDER BY liq_price DESC LIMIT 5", (m,)):
    print("  nearest", r)
b = json.loads(c.execute("SELECT json FROM brief WHERE market=?", (m,)).fetchone()[0])
print("headline:", b["headline"])
for f in b["facts"]:
    print("  fact", f["key"], "|", f["text"])
print("events", c.execute("SELECT kind, count(*), round(sum(usd)) FROM events WHERE market=? GROUP BY 1", (m,)).fetchall())
f = json.loads(c.execute("SELECT json FROM flows WHERE market=? AND window='24h'", (m,)).fetchone()[0])
print("flows24 classes", [(x["class"], round(x["netUsd"]), x["trades"]) for x in f["classes"]])
print("flows24 lending", {k: (round(v) if isinstance(v, float) else v) for k, v in f["lending"].items() if k != "netCollateralByClass"})
print("flows24 bridges", f["bridges"], "lp", f["lp"])
