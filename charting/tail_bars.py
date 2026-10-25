"""Decode swaps from raw log chunks (blocks after the map snapshot) into the stage pool-minute bar format.

Reuses the map's dex_trades.sql decoder (dex_macros + the dl / sw0 statements of dex_range). Tokens are
resolved from the map's pool registry plus pool-creation events seen in the same chunks; Balancer legs carry
their tokens in the event. Swaps whose pool tokens are unknown are skipped (counted).
usage: tail_bars.py <chain> backfill      Blob tail chunks in tail/<chain>/logs  -> stage part=t<from>.parquet
       tail_bars.py <chain> realtime      local follower chunks after the tail      -> stage part=r<from>.parquet
"""
import glob
import json
import os
import re
import sys
import time

from build_bars import AGG, PRICED, TRADE_COLS, setup
from common import EM, ROOT, connect, guard

SQL = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dex_trades.sql")).read()


def section(name):
    m = re.search(r"-- @section %s\n(.*?)(?=\n-- @section |\Z)" % name, SQL, re.S)
    return m.group(1)


MACROS = section("dex_macros")
RANGE = section("dex_range")
DL = RANGE[RANGE.index("CREATE TEMP TABLE dl AS"):RANGE.index("-- 1. decode swaps")]
SW0 = RANGE[RANGE.index("CREATE TEMP TABLE sw0 AS"):RANGE.index("-- 2. exact-amount")]

POOLS_NEW = """
SELECT CASE sig WHEN 'pair_created' THEN w_addr(w(h, 0)) WHEN 'v3_pool_created' THEN w_addr(w(h, 1))
                WHEN 'algebra_pool' THEN w_addr(w(h, 0)) END AS pool, t_addr(topic1) AS token0, t_addr(topic2) AS token1
FROM (SELECT *, hex(data) AS h FROM dl WHERE role = 'pool' AND shape_ok AND sig <> 'v4_initialize')
UNION ALL
SELECT topic1, t_addr(topic2), t_addr(topic3) FROM dl WHERE role = 'pool' AND shape_ok AND sig = 'v4_initialize'
"""

TRADES = """
SELECT s.bn, blk.ts, s.tx_index, s.log_index, tx.hash AS tx_hash, tx."from" AS trader, s.pool,
       coalesce(s.tok_in_evt, CASE s.slot_in WHEN 0 THEN coalesce(r.token0, r.coins[1]) WHEN 1 THEN coalesce(r.token1, r.coins[2])
                                             ELSE r.coins[s.slot_in + 1] END) AS token_in,
       coalesce(s.tok_out_evt, CASE s.slot_out WHEN 0 THEN coalesce(r.token0, r.coins[1]) WHEN 1 THEN coalesce(r.token1, r.coins[2])
                                               ELSE r.coins[s.slot_out + 1] END) AS token_out,
       s.amt_in::DOUBLE AS ai, s.amt_out::DOUBLE AS ao
FROM sw0 s LEFT JOIN reg r ON r.pool = s.pool
LEFT JOIN src_tx tx ON tx.block_number = s.bn AND tx.tx_index = s.tx_index
LEFT JOIN src_blocks blk ON blk.number = s.bn
WHERE s.protocol NOT LIKE 'gmx%' AND NOT coalesce(s.ovf, true) AND s.pool IS NOT NULL
"""


def prepare(c, chain):
    setup(c, chain)
    c.execute(MACROS)
    c.execute(f"""CREATE OR REPLACE TABLE reg AS SELECT pool, any_value(token0) AS token0, any_value(token1) AS token1,
                  any_value(coins) AS coins, 'map' AS src FROM read_parquet('{EM}/map/{chain}/dex_pools/*.parquet') GROUP BY pool""")
    extra = f"{ROOT}/data/{chain}/reg_extra.parquet"
    if os.path.exists(extra):  # pools created after the map snapshot, learned from earlier tail batches
        c.execute(f"INSERT INTO reg SELECT pool, token0, token1, coins, 'new' FROM read_parquet('{extra}') "
                  f"WHERE pool NOT IN (SELECT pool FROM reg)")


def save_new_pools(c, chain):
    extra = f"{ROOT}/data/{chain}/reg_extra.parquet"
    c.execute(f"COPY (SELECT pool, token0, token1, coins FROM reg WHERE src = 'new') TO '{extra}.tmp' (FORMAT parquet)")
    os.replace(extra + ".tmp", extra)


def decode_to(c, files, min_block, dst):
    """Decode swaps in `files` (log chunk Parquet) with block > min_block; write stage bars to dst and the
    priced trades (tape / wallet index input) next to it under stage/trades. Returns stats."""
    lst = ",".join("'%s'" % f for f in files)
    txs = ",".join("'%s'" % f.replace("/logs/logs_", "/transactions/transactions_") for f in files)
    c.execute(f"CREATE OR REPLACE TEMP VIEW src_logs AS SELECT * FROM read_parquet([{lst}]) WHERE block_number > {min_block}")
    c.execute(f"CREATE OR REPLACE TEMP VIEW src_tx AS SELECT block_number, tx_index, hash, \"from\" FROM read_parquet([{txs}]) "
              f"WHERE block_number > {min_block}")
    blks = [f.replace("/logs/logs_", "/blocks/blocks_") for f in files]
    if all(os.path.exists(b) for b in blks):  # follower chunks carry exact block times; Blob gap chunks here do not
        bl = ",".join("'%s'" % b for b in blks)
        c.execute(f"CREATE OR REPLACE TEMP VIEW src_blocks AS SELECT number, timestamp::BIGINT AS ts FROM read_parquet([{bl}]) "
                  f"WHERE number > {min_block}")
    else:
        c.execute("CREATE OR REPLACE TEMP VIEW src_blocks AS SELECT NULL::UBIGINT AS number, NULL::BIGINT AS ts WHERE false")
    for t in ("dl", "sw0"):
        c.execute(f"DROP TABLE IF EXISTS {t}")
    c.execute(DL)
    c.execute(SW0)
    c.execute(f"INSERT INTO reg SELECT n.pool, n.token0, n.token1, NULL, 'new' FROM ({POOLS_NEW}) n "
              f"WHERE n.pool IS NOT NULL AND NOT EXISTS (SELECT 1 FROM reg WHERE reg.pool = n.pool)")
    c.execute(f"CREATE OR REPLACE TEMP TABLE tr AS {TRADES}")
    stats = c.execute("SELECT count(*), count(*) FILTER (WHERE token_in IS NULL OR token_out IS NULL), max(bn) FROM tr").fetchone()
    c.execute("CREATE OR REPLACE TEMP TABLE priced AS " +
              PRICED.replace("{src}", "SELECT * FROM tr WHERE token_in IS NOT NULL AND token_out IS NOT NULL"))
    c.execute(f"COPY ({AGG.replace('{priced}', 'SELECT * FROM priced')}) TO '{dst}.tmp' (FORMAT parquet, COMPRESSION zstd)")
    tdir = os.path.join(os.path.dirname(os.path.dirname(dst)), "trades")
    os.makedirs(tdir, exist_ok=True)
    tdst = os.path.join(tdir, os.path.basename(dst))
    c.execute(f"COPY (SELECT {TRADE_COLS} FROM priced) TO '{tdst}.tmp' (FORMAT parquet, COMPRESSION zstd)")
    os.replace(tdst + ".tmp", tdst)
    os.replace(dst + ".tmp", dst)
    return {"swaps": stats[0], "unresolved": stats[1], "max_block": stats[2]}


def chunk_range(path):
    a, b = re.search(r"_(\d+)-(\d+)\.parquet$", path).groups()
    return int(a), int(b)


def snapshot_end(chain):
    c = connect(threads=2, mem="1GB")
    return int(c.execute(f"SELECT max(block_number) FROM read_parquet('{EM}/map/{chain}/dex_trades/*/*.parquet')").fetchone()[0])


def main(chain, mode):
    out = f"{ROOT}/data/{chain}/stage/pool_1m"
    os.makedirs(out, exist_ok=True)
    state_p = f"{ROOT}/data/{chain}/tail_state.json"
    state = json.load(open(state_p)) if os.path.exists(state_p) else {}
    c = connect(threads=4, mem="4GB")
    if mode == "realtime":
        from block_minutes_live import extend
        known = extend(chain, c)
    prepare(c, chain)
    if mode == "backfill":
        files = sorted(glob.glob(f"{ROOT}/tail/{chain}/logs/*.parquet"), key=chunk_range)
        low = snapshot_end(chain)
        tag = "t"
    else:
        files = []
        for f in sorted(glob.glob(f"/srv/skew/em-realtime/out/{chain}/logs/*.parquet"), key=chunk_range):
            a, b = chunk_range(f)
            m = f"/srv/skew/em-realtime/out/{chain}/manifests/{a:010d}-{b:010d}.json"
            if os.path.exists(m) and json.load(open(m)).get("status") == "OK":
                files.append(f)
        low = int(state.get("last_block", 0))
        files = [f for f in files if chunk_range(f)[1] <= known]  # only chunks whose block times are known
        tag = "r"
    files = [f for f in files if chunk_range(f)[1] > low]
    print(chain, mode, len(files), "chunks after block", low, flush=True)
    step = 120
    for i in range(0, len(files), step):
        guard(f"{chain} tail batch {i}")
        t = time.time()
        batch = files[i:i + step]
        a = max(chunk_range(batch[0])[0], low + 1)
        dst = f"{out}/part={tag}{a:010d}.parquet"
        s = decode_to(c, batch, low, dst)
        save_new_pools(c, chain)
        low = max(low, chunk_range(batch[-1])[1])
        state["last_block"] = low
        json.dump(state, open(state_p + ".tmp", "w"))
        os.replace(state_p + ".tmp", state_p)
        print(chain, mode, os.path.basename(dst), s, f"{time.time() - t:.0f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
