#!/usr/bin/env python3
"""cashcat_check.py DUMP_DIR [--serve URL]: the CASHCAT golden case against EMV2 (run after compare.py robinhood, which leaves
results/robinhood/v2_edges.csv and v2_nodes.csv for the sampled addresses, the creator included).

Engine facts (golden/cashcat.json engine_exact, map end 88,098,834) must match exactly. With --serve (emv2-serve base URL) the
holder counts at launch and launch + 1 h are also checked against the reference (2 and 87) and the creator's first receipt.
Writes results/robinhood/CASHCAT.json.
"""
import json
import subprocess
import sys
import urllib.request

d = sys.argv[1]
serve = sys.argv[sys.argv.index("--serve") + 1] if "--serve" in sys.argv else None
G = json.load(open("golden/cashcat.json"))
E = G["engine_exact"]
cre, tok = G["creator"], G["market"].split(":")[1]
out, checks = "results/robinhood", []


def duck(sql):
    p = subprocess.run(["duckdb", "-json", "-c", sql], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[:500])
    return json.loads(p.stdout or "[]")


def add(name, got, exp):
    checks.append({"check": name, "got": got, "expected": exp, "ok": str(got) == str(exp)})


def first(rows):
    return rows[0] if rows else {}


pick = lambda stem: next((f"{d}/{stem}{x}" for x in (".tsv.zst", ".csv", ".tsv") if __import__("os").path.exists(f"{d}/{stem}{x}")), f"{d}/{stem}.csv")
b = first(duck(f"SELECT n_in, n_out, balance, first_block, last_block FROM read_csv('{pick('bal')}', header=true, all_varchar=true) "
               f"WHERE lower(address) = '{cre}' AND lower(token) = '{tok}'"))
row = E["creator_cashcat_balance_row"]
add("creator CASHCAT n_in", b.get("n_in"), row["n_in"])
add("creator CASHCAT n_out", b.get("n_out"), row["n_out"])
add("creator CASHCAT first_block", b.get("first_block"), row["first_block"])
add("creator CASHCAT last_block", b.get("last_block"), row["last_block"])
add("creator CASHCAT holds (balance > 0)", b.get("balance") is not None and int(b["balance"]) > 0, row["holds"])
if b.get("balance") is not None:
    checks.append({"check": "creator CASHCAT balance vs map net_approx (double, informational)", "got": b["balance"], "expected": row["net_approx"],
                   "ok": abs(float(b["balance"]) - row["net_approx"]) <= abs(row["net_approx"]) * 1e-12})
t = first(duck(f"SELECT sum(n_transfers::UBIGINT) AS n FROM read_csv('{pick('token_stats')}', header=true, all_varchar=true) WHERE lower(token) = '{tok}'"))
add("CASHCAT n_transfers", t.get("n"), E["cashcat_token_stats"]["n_transfers"])
e = first(duck(f"SELECT count(*) AS rows, sum(n::UBIGINT) AS n_sum, count(*) FILTER (WHERE lower(asset) = '{tok}') AS cashcat_rows "
               f"FROM read_csv('{out}/v2_edges.csv', header=true, all_varchar=true) WHERE lower(\"from\") = '{cre}'"))
for k in ("rows", "n_sum", "cashcat_rows"):
    add(f"creator out-edges {k}", e.get(k), E["creator_out_edges"][k])
n = first(duck(f"SELECT * FROM read_csv('{out}/v2_nodes.csv', header=true, all_varchar=true) WHERE lower(address) = '{cre}'"))
for k in ("first_block", "last_block", "tx_sent", "tx_received", "token_transfers_in", "token_transfers_out"):
    add(f"creator node {k}", n.get(k), E["creator_node"][k])
if serve:
    # exact reference: raw recompute at the last block with timestamp <= t (88,836 -> 3 incl. a 12,509-wei pool-path holder; 90,043 -> 90).
    # invscreen answers 0 / 87: it maps t ~20 s earlier (88,829 / 90,035) and drops dust holders; see REPORT.
    for label, t0, exp in (("holders at launch (exact block 88,836)", E["launch"]["time"], 3), ("holders at launch + 1h (exact block 90,043)", E["launch"]["time"] + 3600, 90)):
        try:
            j = json.loads(urllib.request.urlopen(f"{serve}/api/public/em/holders-at?market={G['market']}&t={t0}&limit=100", timeout=60).read())
            snap = (j.get("snaps") or [j])[0]
            got = snap.get("holders") if isinstance(snap.get("holders"), int) else snap.get("nHolders", snap.get("holderCount"))
            add(f"{label} (served)", got, exp)
        except Exception as ex:
            checks.append({"check": f"{label} (served)", "ok": False, "error": str(ex)[:200]})
ok = all(c["ok"] for c in checks)
json.dump({"ok": ok, "checks": checks}, open(f"{out}/CASHCAT.json", "w"), indent=1)
for c in checks:
    print(("PASS " if c["ok"] else "FAIL ") + c["check"], c.get("got"), "/", c.get("expected"))
print("CASHCAT", "PASS" if ok else "FAIL")
