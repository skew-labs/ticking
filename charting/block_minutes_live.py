"""Extend data/block_minutes_<chain>.parquet with blocks from the local real-time follower chunks."""
import glob
import json
import os
import re
import sys

from common import ROOT, connect

RT = "/srv/skew/em-realtime/out"


def extend(chain, c=None):
    c = c or connect(threads=2, mem="1GB")
    path = f"{ROOT}/data/block_minutes_{chain}.parquet"
    last = int(c.execute(f"SELECT max(last_block) FROM read_parquet('{path}')").fetchone()[0])
    files = []
    for f in glob.glob(f"{RT}/{chain}/blocks/*.parquet"):
        a, b = map(int, re.search(r"_(\d+)-(\d+)\.parquet$", f).groups())
        m = f"{RT}/{chain}/manifests/{a:010d}-{b:010d}.json"
        if b > last and os.path.exists(m) and json.load(open(m)).get("status") == "OK":
            files.append(f)
    if not files:
        return last
    lst = ",".join("'%s'" % f for f in files)
    c.execute(f"""COPY (
        SELECT minute, min(first_block) AS first_block, max(last_block) AS last_block, sum(n)::UINTEGER AS n FROM (
            SELECT minute, first_block, last_block, n FROM read_parquet('{path}')
            UNION ALL
            SELECT timestamp // 60 * 60, min(number), max(number), count(*)::UINTEGER
            FROM read_parquet([{lst}]) WHERE number > {last} GROUP BY 1)
        GROUP BY minute ORDER BY minute) TO '{path}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
    os.replace(path + ".tmp", path)
    return int(c.execute(f"SELECT max(last_block) FROM read_parquet('{path}')").fetchone()[0])


if __name__ == "__main__":
    for ch in sys.argv[1:]:
        print(ch, "block_minutes now through block", extend(ch))
