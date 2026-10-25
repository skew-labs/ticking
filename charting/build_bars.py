"""Stage 1: swap trades -> per-pool 1-minute OHLCV in quote units, one Parquet per dex_trades range.

Only swaps with an anchor quote (USD stable, ETH, BTC, ARB) on one side are priced; the other side is
the base asset. Dust prints (quote amount under MIN_QUOTE) are excluded.
usage: build_bars.py <chain> [range_id ...]
"""
import glob
import os
import sys
import time

from common import ANCHORS, EM, MIN_QUOTE, NATIVE, ROOT, connect, guard


def setup(c, chain):
    c.execute(f"""CREATE OR REPLACE TABLE meta AS
        SELECT from_hex(substr(token, 3)) AS token, any_value(symbol) AS symbol, any_value(name) AS name,
               any_value(decimals) AS decimals
        FROM read_parquet('{EM}/econ/{chain}/token_meta.parquet')
        WHERE decimals BETWEEN 0 AND 36 AND token LIKE '0x%' AND length(token) = 42 GROUP BY 1""")
    c.execute(f"INSERT INTO meta SELECT from_hex('{NATIVE[2:]}'), 'ETH', 'Ether (native)', 18 "
              f"WHERE NOT EXISTS (SELECT 1 FROM meta WHERE token = from_hex('{NATIVE[2:]}'))")
    c.execute("CREATE OR REPLACE TABLE anchors(token BLOB, kind VARCHAR, priority INTEGER, symbol VARCHAR)")
    c.executemany("INSERT INTO anchors VALUES (from_hex(?), ?, ?, ?)",
                  [(a[2:], k, p, s) for a, k, p, s in ANCHORS[chain]])
    c.execute(f"CREATE OR REPLACE TABLE bm AS SELECT minute::BIGINT AS minute, first_block::UBIGINT AS first_block, "
              f"last_block::UBIGINT AS last_block FROM read_parquet('{ROOT}/data/block_minutes_{chain}.parquet') "
              f"WHERE minute > 0 ORDER BY first_block")
    mq = " ".join(f"WHEN '{k}' THEN {v}" for k, v in MIN_QUOTE.items())
    c.execute(f"CREATE OR REPLACE MACRO min_quote(k) AS CASE k {mq} ELSE 1e18 END")


# trade level: one priced row per swap with an anchor leg (base = the other token; side B = trader bought base)
PRICED = """
WITH x AS (
    SELECT t.bn, t.ts, t.tx_index, t.log_index, t.tx_hash, t.trader, t.pool, t.token_in, t.token_out,
           t.ai / 10 ^ mi.decimals AS qin, t.ao / 10 ^ mo.decimals AS qout,
           coalesce(ain.priority, 999) AS pin, coalesce(aout.priority, 999) AS pout, ain.kind AS kin, aout.kind AS kout
    FROM ({src}) t
    JOIN meta mi ON mi.token = t.token_in JOIN meta mo ON mo.token = t.token_out
    LEFT JOIN anchors ain ON ain.token = t.token_in LEFT JOIN anchors aout ON aout.token = t.token_out
    WHERE t.ai > 0 AND t.ao > 0 AND t.token_in <> t.token_out
      AND least(coalesce(ain.priority, 999), coalesce(aout.priority, 999)) < 999),
y AS (
    SELECT bn, ts, tx_index, log_index, tx_hash, trader, pool,
           CASE WHEN pin < pout THEN token_out ELSE token_in END AS base,
           CASE WHEN pin < pout THEN token_in ELSE token_out END AS "quote",
           CASE WHEN pin < pout THEN qout ELSE qin END AS base_amt,
           CASE WHEN pin < pout THEN qin ELSE qout END AS quote_amt,
           CASE WHEN pin < pout THEN kin ELSE kout END AS qkind,
           CASE WHEN pin < pout THEN 'B' ELSE 'A' END AS side
    FROM x),
z AS (
    SELECT y.*, quote_amt / base_amt AS px, (bn::BIGINT * 100000 + log_index) AS seq
    FROM y WHERE base_amt > 0 AND quote_amt >= min_quote(qkind) AND isfinite(quote_amt / base_amt))
SELECT z.*, bm.minute FROM z ASOF JOIN bm ON z.bn >= bm.first_block WHERE z.bn <= bm.last_block
"""

AGG = """
SELECT pool, base, "quote", any_value(qkind) AS qkind, minute,
       arg_min(px, seq) AS o, max(px) AS h, min(px) AS l, arg_max(px, seq) AS c,
       sum(base_amt) AS bv, sum(quote_amt) AS qv, count(*)::UINTEGER AS n, min(seq) AS fs, max(seq) AS ls
FROM ({priced}) m GROUP BY pool, base, "quote", minute
"""

TRADE_COLS = """bn, ts, tx_index, log_index, tx_hash, trader, pool, base, "quote", qkind, minute, base_amt, quote_amt, px, side"""

# kept for callers that only need bars
BARS = AGG.replace("{priced}", PRICED)

SNAP_SRC = """SELECT block_number AS bn, NULL::BIGINT AS ts, tx_index, log_index, tx_hash, trader, pool, token_in, token_out,
       amount_in::DOUBLE AS ai, amount_out::DOUBLE AS ao
FROM read_parquet('{path}') WHERE NOT coalesce(amount_overflow, false) AND amount_in IS NOT NULL
  AND amount_out IS NOT NULL AND token_in IS NOT NULL AND token_out IS NOT NULL AND pool IS NOT NULL"""


def run(chain, rids):
    c = connect()
    setup(c, chain)
    out = f"{ROOT}/data/{chain}/stage/pool_1m"
    os.makedirs(out, exist_ok=True)
    parts = sorted(glob.glob(f"{EM}/map/{chain}/dex_trades/range_id=*/*.parquet"),
                   key=lambda p: int(p.split("range_id=")[1].split("/")[0]))
    for p in parts:
        rid = int(p.split("range_id=")[1].split("/")[0])
        if rids and rid not in rids:
            continue
        dst = f"{out}/part={rid:03d}.parquet"
        if os.path.exists(dst):
            continue
        guard(f"{chain} range {rid}")
        t = time.time()
        q = BARS.replace("{src}", SNAP_SRC.format(path=p))
        c.execute(f"COPY ({q}) TO '{dst}.tmp' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)")
        os.replace(f"{dst}.tmp", dst)
        n = c.execute(f"SELECT count(*), count(DISTINCT pool), count(DISTINCT base) FROM '{dst}'").fetchone()
        print(f"{chain} range {rid}: {n[0]} pool-minutes, {n[1]} pools, {n[2]} bases, "
              f"{os.path.getsize(dst) / 1e6:.0f} MB, {time.time() - t:.0f}s", flush=True)


def recent_trades(chain, days=7):
    """Trade-level rows of the map snapshot inside the tape window (last `days` before the newest known block time)."""
    c = connect()
    setup(c, chain)
    out = f"{ROOT}/data/{chain}/stage/trades"
    os.makedirs(out, exist_ok=True)
    since = c.execute(f"SELECT min(first_block) FROM bm WHERE minute >= (SELECT max(minute) FROM bm) - {days * 86400}").fetchone()[0]
    parts = [p for p in glob.glob(f"{EM}/map/{chain}/dex_trades/range_id=*/*.parquet")
             if int(p.split("range_id=")[1].split("/")[0]) >= since // 10_000_000]
    lst = ",".join("'%s'" % p for p in parts)
    src = SNAP_SRC.format(path="__P__").replace("read_parquet('__P__')", f"read_parquet([{lst}])") + f" AND block_number >= {since}"
    dst = f"{out}/part=s{since:010d}.parquet"
    c.execute(f"COPY (SELECT {TRADE_COLS} FROM ({PRICED.replace('{src}', src)})) TO '{dst}.tmp' (FORMAT parquet, COMPRESSION zstd)")
    os.replace(dst + ".tmp", dst)
    print(chain, "snapshot tape rows", c.execute(f"SELECT count(*) FROM '{dst}'").fetchone()[0], "since block", since, flush=True)


if __name__ == "__main__":
    if sys.argv[2:3] == ["recent"]:
        recent_trades(sys.argv[1])
    else:
        run(sys.argv[1], {int(x) for x in sys.argv[2:]})
