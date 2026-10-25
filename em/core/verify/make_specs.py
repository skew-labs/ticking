#!/usr/bin/env python3
"""make_specs.py CHAIN OLD_BASE NEW_BASE OUT.jsonl [N]: API comparison requests for compare_api.py from the frozen golden sample.

Covers the phase 1 routes EMV2 answers (serve/routes.cpp): address-chains, trace (in/out, 1 and 2 hops), holders-at.
Old answers come from invscreen (arbitrum/robinhood only). Everything is pinned to the map end so map-only V2 and map+delta
invscreen are compared on the same data: holders-at uses block=<map end>; trace carries old_pinned with to=<map end time>.
Addresses: a fixed mix from golden/<chain>/sample.csv (hubs, recent, new, contracts, random; deterministic order).
"""
import csv
import json
import subprocess
import sys

chain, old, new, out = sys.argv[1:5]
n = int(sys.argv[5]) if len(sys.argv) > 5 else 40
g = f"golden/{chain}"
end_block = int(open(f"{g}/ranges_included.tsv").read().strip().splitlines()[-1].split("\t")[-1])
end_time = json.loads(subprocess.run(["duckdb", "-json", "-c", f"SELECT epoch(max(last_time))::BIGINT AS t FROM read_parquet('{g}/addresses.parquet') "
                                      f"WHERE last_block <= {end_block}"], capture_output=True, text=True).stdout)[0]["t"]
rows = list(csv.DictReader(open(f"{g}/sample.csv")))
pick, seen = [], set()
for why in ("named", "hub_out", "hub_in", "recent", "new", "contract", "random"):
    take = [r["address"] for r in rows if why in r["why"].split(",") and r["address"] not in seen][: (99 if why == "named" else max(2, n // 6))]
    pick += take; seen.update(take)
pick = pick[:n]
tokens = json.loads(subprocess.run(["duckdb", "-json", "-c", f"SELECT '0x' || lower(hex(token)) AS t FROM read_parquet('{g}/token_stats.parquet') "
                                    f"WHERE standard = 'erc20' ORDER BY n_transfers DESC LIMIT 8"], capture_output=True, text=True).stdout)
if chain == "robinhood":
    tokens = [{"t": "0x020bfc650a365f8bb26819deaabf3e21291018b4"}] + tokens
P = "/api/public/em"
with open(out, "w") as f:
    for a in pick:
        f.write(json.dumps({"name": f"{chain} address-chains {a}", "old": f"{old}{P}/address-chains?address={a}", "new": f"{new}{P}/address-chains?address={a}"}) + "\n")
        for d in ("in", "out"):
            for h in (1, 2):
                q = f"/trace?chain={chain}&address={a}&direction={d}&hops={h}"
                f.write(json.dumps({"name": f"{chain} trace {d}{h} {a}", "old": f"{old}{P}{q}", "new": f"{new}{P}{q}",
                                    "old_pinned": f"{old}{P}{q}&from=0&to={end_time}"}) + "\n")
    for t in tokens:
        q = f"/holders-at?market={chain}:{t['t']}&block={end_block}&limit=30"
        f.write(json.dumps({"name": f"{chain} holders-at {t['t']}@{end_block}", "old": f"{old}{P}{q}", "new": f"{new}{P}{q}"}) + "\n")
print(json.dumps({"chain": chain, "addresses": len(pick), "tokens": len(tokens), "end_block": end_block, "end_time": end_time, "out": out}))
