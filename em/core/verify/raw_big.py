#!/usr/bin/env python3
"""raw_big.py IN.csv OUT.csv: finish raw_balances.sql output: balance = d0 + d1*2^64 + d2*2^128 + d3*2^192 for every row whose
balance DuckDB could not hold in HUGEINT (and re-check the ones it did). Output: address,token,kind,n_in,n_out,balance,first_block,last_block."""
import csv, sys
r = csv.DictReader(open(sys.argv[1])); w = csv.writer(open(sys.argv[2], "w", newline=""))
w.writerow(["address", "token", "kind", "n_in", "n_out", "balance", "first_block", "last_block"])
fixed = mism = 0
for x in r:
    if x["kind"] == "erc721":
        bal = int(x["n_in"]) - int(x["n_out"])
    else:
        bal = int(x["d0"]) + (int(x["d1"]) << 64) + (int(x["d2"]) << 128) + (int(x["d3"]) << 192)
    if x["balance"] in ("", None): fixed += 1
    elif int(x["balance"]) != bal: mism += 1
    w.writerow([x["address"], x["token"], x["kind"], x["n_in"], x["n_out"], bal, x["first_block"], x["last_block"]])
print(f"python-finished {fixed}, duckdb/python disagreements {mism}")
sys.exit(1 if mism else 0)
