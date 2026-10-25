#!/usr/bin/env python3
"""max_block.py (VM, as root): the map's last included block for a chain, used as the ledger --max-block (verify's rule).

    sudo python3 max_block.py <chain> [--local DIR]

Reads <map>/ranges_included.tsv (Blob, or DIR for maps that are not on Blob such as arc) and prints the end block of its last row.
Never prints the SAS.
"""
import csv
import io
import os
import sys
import urllib.parse
import urllib.request

MAP = {"linea": "linea/map", "unichain": "unichain/map", "optimism": "optimism/map", "robinhood": "map/robinhood",
       "arbitrum": "map/arbitrum", "ethereum": "ethereum/map", "base": "base/map", "polygon": "polygon/map", "arc": "arc/map"}


def main():
    ch = sys.argv[1]
    if "--local" in sys.argv:
        text = open(os.path.join(sys.argv[sys.argv.index("--local") + 1], "ranges_included.tsv")).read()
    else:
        sas = open("/root/.em-blob.sas").read().strip().lstrip("?")
        url = f"https://arbitrum.blob.core.windows.net/em-mapping/{urllib.parse.quote(MAP[ch] + '/ranges_included.tsv')}?{sas}"
        text = urllib.request.urlopen(url, timeout=60).read().decode()
    rows = [r for r in csv.reader(io.StringIO(text), delimiter="\t") if r and r[0].strip()]
    head, last = rows[0], rows[-1]
    cols = [c.lower() for c in head]
    for name in ("end_block", "end", "to_block", "last_block", "hi"):
        if name in cols:
            print(int(last[cols.index(name)]))
            return
    nums = [int(x) for x in last if x.strip().isdigit()]
    if not nums:
        raise SystemExit(f"cannot read an end block from {head} / {last}")
    print(max(nums))


if __name__ == "__main__":
    main()
