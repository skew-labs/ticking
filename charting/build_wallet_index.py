"""Per-wallet trade / position-action index for chart markers (GET /api/public/em/wallet-trades).

trades: every priced swap (anchor on one side) whose base token is a served market, attributed to the
        transaction sender. Snapshot dex_trades + stage trades (Blob gap + follower). One file per first hex
        nibble of the wallet, sorted by (trader, mid, bn, log_index) with small row groups so a wallet lookup
        reads a few KB. Columns are compact: no tx hash (resolved on demand), no time (from block_minutes).
actions: econ_actions of lending / vault kinds (supply, withdraw, borrow, repay, liquidations, stake, unstake)
        on served market tokens, sorted by (wallet, mid, bn).
usage: build_wallet_index.py <chain>
"""
import glob
import json
import os
import shutil
import sys
import time

from build_bars import PRICED, SNAP_SRC, setup
from common import EM, ROOT, connect, guard

ACTIONS = ("supply", "withdraw", "borrow", "repay", "liquidated", "liquidate", "stake", "unstake")
W3 = ("('0x' || substr(hex({col}), 1, 16))::UBIGINT AS w1, ('0x' || substr(hex({col}), 17, 16))::UBIGINT AS w2, "
      "('0x' || substr(hex({col}), 33, 8))::UINTEGER AS w3")
QKIND = "CASE qkind WHEN 'usd' THEN 0 WHEN 'eth' THEN 1 WHEN 'btc' THEN 2 WHEN 'arb' THEN 3 END::UTINYINT"
# PRICED without the minute join: the index stores blocks, time is resolved at query time
PRICED_NOMIN = PRICED.split("SELECT z.*, bm.minute")[0] + "SELECT z.* FROM z"


def main(chain):
    t0 = time.time()
    guard(f"{chain} wallet index", floor=100)
    out = f"{ROOT}/data/{chain}/wallet"
    work = f"{ROOT}/data/{chain}/wallet.building"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(f"{work}/p1", exist_ok=True)
    c = connect(threads=4, mem="6GB")
    setup(c, chain)
    c.execute(f"""CREATE TABLE mk AS SELECT (row_number() OVER (ORDER BY token))::USMALLINT AS mid, token, from_hex(substr(token, 3)) AS tb,
                 symbol, kind FROM read_parquet('{ROOT}/data/{chain}/markets_export.parquet')""")
    last_block = int(json.load(open(f"{ROOT}/data/{chain}/info_export.json"))["last_block"])
    snap_last = int(c.execute(f"SELECT max(block_number) FROM read_parquet('{EM}/map/{chain}/dex_trades/*/*.parquet')").fetchone()[0])
    c.execute(f"COPY (SELECT mid, token, symbol, kind FROM mk) TO '{work}/markets.parquet' (FORMAT parquet)")
    # wallet as 3 integers (8+8+4 bytes): Parquet BLOB statistics are not safe for pruning equality filters
    # in DuckDB (rows were silently skipped), integer statistics are
    sel = f"""SELECT {W3.format(col='z.trader')}, mk.mid, z.bn::UINTEGER AS bn, z.tx_index::UINTEGER AS tx_index, z.log_index::UINTEGER AS log_index,
                     (z.side = 'B') AS buy, z.base_amt::FLOAT AS base_amt, z.quote_amt::FLOAT AS quote_amt, {QKIND} AS qk,
                     substr(hex(z.trader), 1, 1) AS b
              FROM ({{priced}}) z JOIN mk ON mk.tb = z.base WHERE z.trader IS NOT NULL"""
    # snapshot ranges
    parts = sorted(glob.glob(f"{EM}/map/{chain}/dex_trades/range_id=*/*.parquet"), key=lambda p: int(p.split("range_id=")[1].split("/")[0]))
    for p in parts:
        rid = int(p.split("range_id=")[1].split("/")[0])
        guard(f"{chain} wallet index range {rid}", floor=100)
        q = sel.replace("{priced}", PRICED_NOMIN.replace("{src}", SNAP_SRC.format(path=p)))
        c.execute(f"COPY ({q}) TO '{work}/p1/s{rid:03d}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (b))")
        print(chain, "range", rid, round(time.time() - t0), "s", flush=True)
    # stage trades (Blob gap + follower), already priced
    tparts = [p for p in glob.glob(f"{ROOT}/data/{chain}/stage/trades/part=[tr]*.parquet")]
    if tparts:
        lst = ",".join("'%s'" % p for p in tparts)
        q = sel.replace("{priced}", f"SELECT * FROM read_parquet([{lst}], union_by_name=true)")
        c.execute(f"COPY ({q}) TO '{work}/p1/tail' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (b))")
    stage_last = c.execute(f"SELECT max(bn) FROM read_parquet([{lst}], union_by_name=true)").fetchone()[0] if tparts else last_block
    # pass 2: one sorted file per nibble
    rows = 0
    for nib in "0123456789ABCDEF":
        files = glob.glob(f"{work}/p1/*/b={nib}/*.parquet")
        if not files:
            continue
        guard(f"{chain} wallet index sort {nib}", floor=100)
        lst = ",".join("'%s'" % f for f in files)
        c.execute(f"""COPY (SELECT w1, w2, w3, mid, bn, tx_index, log_index, buy, base_amt, quote_amt, qk FROM read_parquet([{lst}])
                           ORDER BY w1, w2, w3, mid, bn, log_index)
                      TO '{work}/trades_b={nib.lower()}.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 16384)""")
        n = c.execute(f"SELECT count(*) FROM '{work}/trades_b={nib.lower()}.parquet'").fetchone()[0]
        rows += n
        for f in files:
            os.remove(f)
        print(chain, "bucket", nib, n, round(time.time() - t0), "s", flush=True)
    shutil.rmtree(f"{work}/p1", ignore_errors=True)
    # econ actions on served tokens
    al = ",".join("'%s'" % a for a in ACTIONS)
    c.execute(f"""COPY (
        SELECT {W3.format(col="from_hex(substr(lower(a.wallet), 3))")}, mk.mid, a.block_number::UINTEGER AS bn, a.tx_index::UINTEGER AS tx_index,
               a.log_index::UINTEGER AS log_index, a.action, a.protocol, a.pos_kind,
               (a.amount_raw / 10 ^ coalesce(m.decimals, 18))::DOUBLE AS amount, a.tx_hash
        FROM read_parquet('{EM}/econ/{chain}/econ_actions/*.parquet') a
        JOIN mk ON mk.token = lower(a.asset) LEFT JOIN meta m ON m.token = mk.tb
        WHERE a.action IN ({al}) AND a.wallet LIKE '0x%' AND length(a.wallet) = 42
        ORDER BY 1, 2, 3, 4, 5, 7) TO '{work}/actions.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 16384)""")
    nact = c.execute(f"SELECT count(*) FROM '{work}/actions.parquet'").fetchone()[0]
    json.dump({"chain": chain, "built_at": int(time.time()), "trades_rows": rows, "actions_rows": nact,
               "snapshot_last_block": snap_last, "trades_last_block": int(stage_last or last_block),
               "attribution": "transaction sender (tx.from)"}, open(f"{work}/info.json", "w"))
    shutil.rmtree(out, ignore_errors=True)
    os.replace(work, out)
    size = sum(os.path.getsize(f) for f in glob.glob(f"{out}/*")) / 1e9
    print("DONE", chain, rows, "trade rows", nact, "actions", f"{size:.2f} GB", round(time.time() - t0), "s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
