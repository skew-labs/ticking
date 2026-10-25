#!/usr/bin/env python3
"""polygon_remaining.py tokens|sums REMAINING.txt OUT.parquet [SAMPLE_TOKENS.txt] [--threads N] [--par K]: Polygon pieces the held exact run
did not reach, read straight from Blob with DuckDB httpfs (column projection, range reads; the SAS lives only in a root-only temp file).

  tokens: distinct token of every remaining token_transfers file   -> OUT (token BLOB, files BIGINT)
  sums:   for SAMPLE_TOKENS only, per (holder, token) the signed ERC20 amount over the remaining files, as four HUGEINT limb sums
          plus n_in/n_out, with the map's rules (ERC20 + WETH deposit/withdrawal; amounts >= 2^100 are kept, flagged in big_rows)
Files are processed in groups of K in parallel DuckDB processes, each writing its own part; OUT is the union. Limb sums are written as
decimal strings: parquet has no 128-bit integer and DuckDB stores HUGEINT columns as DOUBLE, which would round them."""
import concurrent.futures as cf
import os
import subprocess
import sys
import tempfile

mode, rem, out = sys.argv[1:4]
sample = sys.argv[4] if mode in ("sums", "sums2") else None
th = int(sys.argv[sys.argv.index("--threads") + 1]) if "--threads" in sys.argv else 2
par = int(sys.argv[sys.argv.index("--par") + 1]) if "--par" in sys.argv else 4
files = [l.strip() for l in open(rem) if l.strip()]
SAS = open("/root/.em-blob.sas").read().strip().lstrip("?")
URL = "https://arbitrum.blob.core.windows.net/em-mapping/"
work = out + ".parts"
os.makedirs(work, exist_ok=True)
groups = [files[i:i + 8] for i in range(0, len(files), 8)]
toks = ""
if sample:
    toks = ", ".join(f"from_hex('{t.strip()[2:]}')" for t in open(sample) if t.strip())


def run(gi):
    part = f"{work}/part-{gi:04d}.parquet"
    if os.path.exists(part):
        return 0
    lst = ", ".join(f"'{URL}{f}?{SAS}'" for f in groups[gi])
    if mode == "tokens":
        q = f"COPY (SELECT token, count(*) AS rows FROM read_parquet([{lst}]) GROUP BY token) TO '{part}.tmp' (FORMAT parquet);"
    elif mode == "sums2":   # the same signed limb sums twice: all transfers, and without amounts >= 2^100 (the exact run's rule)
        big = "substr(h, 1, 39) <> '000000000000000000000000000000000000000'"
        lim = lambda i, cond: f"sum(CASE WHEN {cond} THEN sgn * ('0x' || substr(h, {49 - 16 * i}, 16))::UBIGINT::HUGEINT ELSE 0 END)::VARCHAR"
        q = (f"COPY (WITH t AS (SELECT token, \"from\" f, \"to\" tt, lpad(hex(coalesce(amount, ''::BLOB)), 64, '0') h FROM read_parquet([{lst}]) "
             f"WHERE standard IN ('ERC20', 'WETH_DEPOSIT', 'WETH_WITHDRAWAL') AND token IN ({toks})), "
             f"legs AS (SELECT tt AS holder, token, 1 AS sgn, h FROM t UNION ALL SELECT f, token, -1, h FROM t) "
             f"SELECT holder, token, " + ", ".join(f"{lim(i, 'true')} AS d{i}" for i in range(4)) + ", "
             + ", ".join(f"{lim(i, 'NOT (' + big + ')')} AS e{i}" for i in range(4)) + f", count(*) FILTER (WHERE {big}) AS big_rows "
             f"FROM legs GROUP BY holder, token) TO '{part}.tmp' (FORMAT parquet);")
    else:
        q = (f"COPY (WITH t AS (SELECT token, \"from\" f, \"to\" tt, lpad(hex(coalesce(amount, ''::BLOB)), 64, '0') h FROM read_parquet([{lst}]) "
             f"WHERE standard IN ('ERC20', 'WETH_DEPOSIT', 'WETH_WITHDRAWAL') AND token IN ({toks})), "
             f"legs AS (SELECT tt AS holder, token, 1 AS sgn, h FROM t UNION ALL SELECT f, token, -1, h FROM t) "
             f"SELECT holder, token, sum(CASE WHEN sgn = 1 THEN 1 ELSE 0 END)::UBIGINT AS n_in, sum(CASE WHEN sgn = -1 THEN 1 ELSE 0 END)::UBIGINT AS n_out, "
             f"sum(sgn * ('0x' || substr(h, 49, 16))::UBIGINT::HUGEINT)::VARCHAR AS d0, sum(sgn * ('0x' || substr(h, 33, 16))::UBIGINT::HUGEINT)::VARCHAR AS d1, "
             f"sum(sgn * ('0x' || substr(h, 17, 16))::UBIGINT::HUGEINT)::VARCHAR AS d2, sum(sgn * ('0x' || substr(h, 1, 16))::UBIGINT::HUGEINT)::VARCHAR AS d3, "
             f"count(*) FILTER (WHERE substr(h, 1, 39) <> '000000000000000000000000000000000000000') AS big_rows "
             f"FROM legs GROUP BY holder, token) TO '{part}.tmp' (FORMAT parquet);")
    with tempfile.NamedTemporaryFile("w", dir="/root", suffix=".sql", delete=False) as fh:
        os.chmod(fh.name, 0o600)
        fh.write(f"LOAD httpfs; SET threads={th}; SET memory_limit='4GB'; SET temp_directory='/data/emv2-dev/verify/tmp/rem-{gi}'; " + q)
        qf = fh.name
    try:
        p = subprocess.run(["nice", "-n", "15", "duckdb", "-f", qf], capture_output=True, text=True)
    finally:
        os.unlink(qf)
    if p.returncode:
        err = p.stderr.replace(SAS, "<sas>")[:400]
        raise RuntimeError(f"group {gi}: {err}")
    os.replace(part + ".tmp", part)
    return len(groups[gi])


with cf.ThreadPoolExecutor(par) as ex:
    done = sum(ex.map(run, range(len(groups))))
agg = ("SELECT token, sum(rows) AS rows, count(*) AS file_groups FROM read_parquet('{w}/part-*.parquet') GROUP BY token" if mode == "tokens" else
       "SELECT holder, token, " + ", ".join(f"sum({c}{i}::HUGEINT)::VARCHAR {c}{i}" for c in "de" for i in range(4)) + ", sum(big_rows)::BIGINT big_rows "
       "FROM read_parquet('{w}/part-*.parquet') GROUP BY holder, token" if mode == "sums2" else
       "SELECT holder, token, sum(n_in)::BIGINT n_in, sum(n_out)::BIGINT n_out, sum(d0::HUGEINT)::VARCHAR d0, sum(d1::HUGEINT)::VARCHAR d1, "
       "sum(d2::HUGEINT)::VARCHAR d2, sum(d3::HUGEINT)::VARCHAR d3, sum(big_rows)::BIGINT big_rows "
       "FROM read_parquet('{w}/part-*.parquet') GROUP BY holder, token").format(w=work)
p = subprocess.run(["duckdb", "-c", f"COPY ({agg}) TO '{out}' (FORMAT parquet)"], capture_output=True, text=True)
print(mode, len(files), "files", done, "newly read", "->", out, p.stderr[:200])
