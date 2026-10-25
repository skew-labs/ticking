#!/usr/bin/env python3
"""compare_counts.py CHAIN GRAPH_COUNTS.csv [LEDGER_COUNTS.tsv]: recompute the map's checks.json equations from EMV2 whole-chain totals.

GRAPH_COUNTS: key,value CSV from `emv2-graph dump --table counts` (or from count_stream.sh over `--all`).
LEDGER_COUNTS: the ledger's counts.tsv (key<TAB>value).
Every equation EMV2 can recompute is PASS/FAIL against the map's `expected` (the source-side count, independent of the map);
equations over map-only columns (logs, contracts_created, token_stats) are listed as not_covered. For Base, RECHECK.json
(the 2026-10-20 re-check of the promoted map) supplies the same expected values.
"""
import csv
import json
import os
import sys

chain, gpath = sys.argv[1], sys.argv[2]
lpath = sys.argv[3] if len(sys.argv) > 3 else None
g = {r[0]: r[1] for r in csv.reader(open(gpath)) if len(r) >= 2 and r[0] != "key"}
l = {}
if lpath:
    for line in open(lpath):
        k, _, v = line.rstrip("\n").partition("\t")
        if k and k != "key":
            l[k] = v
exp = {c["check_name"]: c["expected"] for c in json.load(open(f"golden/{chain}/checks.json"))}
got_map = {c["check_name"]: c["got"] for c in json.load(open(f"golden/{chain}/checks.json"))}
tt = exp["sum(address_tokens.n_in) == all token transfers"]
rows = [
    ("native_tx edges.n == count(tx with to)", g.get("edges_n_native_tx")),
    ("erc20 edges.n == token_transfers ERC20(+WETH)", g.get("edges_n_erc20")),
    ("erc721 edges.n == token_transfers ERC721", g.get("edges_n_erc721")),
    ("erc1155 edges.n == token_transfers ERC1155", g.get("edges_n_erc1155")),
    ("sum(addresses.tx_sent) == count(tx)", g.get("sum_tx_sent")),
    ("sum(addresses.tx_received) == count(tx with to)", g.get("sum_tx_received")),
    ("sum(token_transfers_out) == all token transfers", g.get("sum_token_transfers_out")),
    ("sum(token_transfers_in) == all token transfers", g.get("sum_token_transfers_in")),
    ("native edges total_amount == raw recompute (hex path)", g.get("native_total_amount")),
    ("is_contract count == contract_creations rows (distinct)", g.get("n_is_contract")),
    ("addresses unique", g.get("nodes")),
]
out = []
for name, got in rows:
    if name not in exp:
        continue
    e = exp[name]
    out.append({"check": name, "got": got, "expected": e, "ok": got is not None and str(got) == str(e)})
for key in ("sum_n_in", "sum_n_out"):
    if lpath:
        out.append({"check": f"ledger {key} == all token transfers", "got": l.get(key), "expected": tt, "ok": str(l.get(key)) == str(tt)})
if "edges_rows" in g:
    out.append({"check": "edge rows == map edge rows (map got, informational)", "got": g["edges_rows"], "expected": None, "ok": True})
covered = {r["check"] for r in out}
not_covered = [k for k in exp if k not in covered]
ok = all(r["ok"] for r in out) and bool(out)
res = {"chain": chain, "ok": ok, "checks": out, "not_covered": not_covered, "graph_build": g.get("build_id"), "ledger_build": l.get("build_id")}
os.makedirs(f"results/{chain}", exist_ok=True)
json.dump(res, open(f"results/{chain}/COUNTS.json", "w"), indent=1)
for r in out:
    print(("PASS " if r["ok"] else "FAIL ") + r["check"], r["got"], "expected", r["expected"])
print("not covered:", not_covered)
print(chain, "COUNTS", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
