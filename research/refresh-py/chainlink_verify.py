"""Verified Chainlink aggregators per chain -> src/chainlink_verified.csv (chain, name, base, quote, proxy, aggregator, phase).

Anyone can deploy a contract that emits AnswerUpdated with a "BASE / USD" description, and several bases on both chains
have 2-4 such emitters (e.g. robinhood "MSTR / USD" at $95.50 next to Chainlink's "Robinhood MSTR / USD" at $153.88).
Only aggregators that an official Chainlink proxy points to are trusted: the proxy list comes from Chainlink's
reference data directory (reference-data-directory.vercel.app/feeds-<network>.json) and every phase aggregator of each
proxy (phaseId(), phaseAggregators(i)) is read on-chain, so historical aggregators of a feed stay valid.
"""
import csv
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CHAINS, SRC, log  # noqa: E402

RDD = {"arbitrum": "ethereum-mainnet-arbitrum-1", "robinhood": "robinhood-mainnet"}


def norm(name):
    """'Robinhood AAPL / USD' -> ('AAPL', 'USD'); 'Robinhood DELL-USD' -> ('DELL', 'USD'); 'WEETH / EETH Exchange Rate' -> ('WEETH', 'EETH ...')."""
    n = re.sub(r"^Robinhood\s+", "", name.strip())
    if "/" in n:
        b, q = [x.strip() for x in n.split("/", 1)]
    elif re.match(r"^[A-Za-z0-9.]+-USD$", n):
        b, q = n[:-4], "USD"
    else:
        return None, None
    return b.upper(), q.upper()


def main():
    rows = []
    for chain, net in RDD.items():
        import rpc
        rpc.configure(CHAINS[chain]["rpcs"])
        with urllib.request.urlopen(f"https://reference-data-directory.vercel.app/feeds-{net}.json", timeout=60) as r:
            feeds = json.load(r)
        feeds = [f for f in feeds if f.get("proxyAddress") and f.get("name")]
        res = rpc.aggregate3([(f["proxyAddress"].lower(), "58303b10") for f in feeds], chunk=100)   # phaseId()
        calls, meta = [], []
        for f, (ok, b) in zip(feeds, res):
            ph = int.from_bytes(b[:32], "big") if ok and len(b) >= 32 else 0
            base, quote = norm(f["name"])
            if not base:
                continue
            for i in range(1, min(ph, 64) + 1):
                calls.append((f["proxyAddress"].lower(), "c1597304" + "%064x" % i))           # phaseAggregators(uint16)
                meta.append((f, base, quote, i))
            if f.get("contractAddress"):
                rows.append((CHAINS[chain]["id"], f["name"], base, quote, f["proxyAddress"].lower(), f["contractAddress"].lower(), ph))
        res = rpc.aggregate3(calls, chunk=150) if calls else []
        for (f, base, quote, i), (ok, b) in zip(meta, res):
            if ok and len(b) >= 32:
                agg = "0x" + b[12:32].hex()
                if agg != "0x" + "0" * 40:
                    rows.append((CHAINS[chain]["id"], f["name"], base, quote, f["proxyAddress"].lower(), agg, i))
        log(f"{chain}: {len(feeds)} official proxies, {len(calls)} phase lookups")
    seen, out = set(), []
    for r in rows:
        if (r[0], r[5]) not in seen:
            seen.add((r[0], r[5]))
            out.append(r)
    with open(f"{SRC}/chainlink_verified.csv.tmp", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["chain", "name", "base", "quote", "proxy", "aggregator", "phase"])
        w.writerows(out)
    os.replace(f"{SRC}/chainlink_verified.csv.tmp", f"{SRC}/chainlink_verified.csv")
    log(f"verified aggregators: {len(out)}")


if __name__ == "__main__":
    main()
