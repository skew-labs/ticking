"""Econ v3: bring the v1/v2 economic layer from the snapshot to the latest processed block, incrementally.

Layout per chain (v1 and v2 stay untouched):
  /srv/skew/em-data/econ/<chain>/v3/
    econ.duckdb                 copy of the final v1 DuckDB (dims, meta caches, prices, sheets tables), updated in place
    stage/{logs_sel,swaps}/p=N  hard links to the v1 stage files + inc_<lo>_<uuid>.parquet per refresh
    stage/v2_reserves_head.parquet   merged (latest Sync per pair)
    econ_actions/part_*.parquet hard links to v1 parts + part_<lo>.parquet per refresh
    outputs written by the v1 SQL (positions_head.parquet, balance_sheets.parquet, prices_head.csv, focus_*.csv, ...)
Stateful steps (positions, prices, sheets) are recomputed over the full event history each run; additive inputs
(stage, actions, DEX daily prices, token balances) are appended. Token balances come from the wallet map
(map/<chain>/address_tokens, exact up to the snapshot) plus map/<chain>/_delta (all transfers since the snapshot),
because the raw token_transfers of the snapshot range are not on this host.
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CHAINS, DATA, SRC, bind_inc_views, cap_mem, connect, hardlink_tree, log, need_space, run_sql, warn)  # noqa: E402

GEN_EVENTS = None


def e1(chain):
    return f"{DATA}/econ/{chain}"


def e3(chain):
    return f"{DATA}/econ/{chain}/v3"


def init(chain):
    E1, E3 = e1(chain), e3(chain)
    db = f"{E3}/econ.duckdb"
    if os.path.exists(db):
        return
    os.makedirs(E3, exist_ok=True)
    src = f"{E1}/{CHAINS[chain]['db1']}"
    need_space(80 + os.path.getsize(src) / 1e9, "copy econ db")
    log(f"init v3: copy {src} ({os.path.getsize(src) / 1e9:.1f} GB)")
    shutil.copyfile(src, db + ".tmp")
    if os.path.exists(src + ".wal"):
        shutil.copyfile(src + ".wal", db + ".tmp.wal")
    for sub in ("stage/logs_sel", "stage/swaps", "econ_actions"):
        hardlink_tree(f"{E1}/{sub}", f"{E3}/{sub}")
    for f in ("stage/v2_reserves_head.parquet", "contract_topic_hist.parquet"):
        if os.path.exists(f"{E1}/{f}") and not os.path.exists(f"{E3}/{f}"):
            os.link(f"{E1}/{f}", f"{E3}/{f}")
    if os.path.exists(db + ".tmp.wal"):
        os.replace(db + ".tmp.wal", db + ".wal")
    os.replace(db + ".tmp", db)
    log("init v3: done")


def bind_stage(con, chain, lo=0, hi=1 << 62, materialize=False):
    """logs_sel / swaps_sel TEMP views (+ ev_* views) over the v3 stage, like econ run.py bind_partition."""
    global GEN_EVENTS
    E3 = e3(chain)
    plo, phi = lo // 5000000, min(hi, 1 << 40) // 5000000
    for (v,) in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT temporary AND (view_name LIKE 'ev_%' OR view_name IN ('logs_sel','swaps_sel'))").fetchall():
        con.execute(f"DROP VIEW IF EXISTS main.{v}")
    src = f"""SELECT * FROM read_parquet('{E3}/stage/logs_sel/*/*.parquet', hive_partitioning=true, union_by_name=true)
              WHERE p BETWEEN {plo} AND {phi} AND block_number BETWEEN {lo} AND {hi}"""
    if materialize:
        con.execute(f"CREATE OR REPLACE TEMP TABLE logs_part AS {src} ORDER BY topic0, block_number, log_index")
        con.execute("CREATE OR REPLACE TEMP VIEW logs_sel AS SELECT * FROM logs_part")
    else:
        con.execute(f"CREATE OR REPLACE TEMP VIEW logs_sel AS {src}")
    con.execute(f"""CREATE OR REPLACE TEMP VIEW swaps_sel AS SELECT * FROM read_parquet('{E3}/stage/swaps/*/*.parquet', hive_partitioning=true, union_by_name=true)
                    WHERE p BETWEEN {plo} AND {phi} AND block_number BETWEEN {lo} AND {hi}""")
    if GEN_EVENTS is None:
        GEN_EVENTS = open(f"{SRC}/gen_events.sql").read().replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TEMP VIEW")
    con.execute(GEN_EVENTS)


def stage_inc(con, chain, lo, hi):
    """10_stage.sql for [lo, hi] only, appended as new files (never overwriting the v1 stage files)."""
    E3 = e3(chain)
    con.execute(open(f"{SRC}/gen_macros.sql").read())
    con.execute(f"CREATE OR REPLACE TABLE event_defs AS SELECT * FROM read_csv('{SRC}/events.csv', header=true, all_varchar=true)")
    con.execute(f"CREATE OR REPLACE TABLE gmx2_names AS SELECT event_name, unhex(substr(name_hash,3)) h FROM read_csv('{SRC}/gmx_v2_events.csv', header=true, all_varchar=true)")
    con.execute("""CREATE OR REPLACE TABLE stage_t0 AS SELECT DISTINCT unhex(substr(topic0,3)) AS t0 FROM event_defs
        WHERE key NOT IN ('erc20.Transfer','robinhood_stock.TransferWithScaledUI','univ2.Sync','univ2.Swap','univ3.Swap',
                          'pancake_v3.Swap','univ4.Swap','gmx_v2.EventLog','gmx_v2.EventLog1','gmx_v2.EventLog2','relay.SolverCallExecuted')""")
    t = time.time()
    tag = f"inc_{lo}"
    import glob as _g
    for f in _g.glob(f"{E3}/stage/logs_sel/*/{tag}_*") + _g.glob(f"{E3}/stage/swaps/*/{tag}_*"):
        os.remove(f)          # re-running a range replaces its files
    con.execute(f"""COPY (
        SELECT chain, block_number, tx_index, log_index, address, topic0, topic1, topic2, topic3, topic_count, hex(data) AS h,
               (block_number // 5000000)::INTEGER AS p
        FROM logs
        WHERE topic0 IN (SELECT t0 FROM stage_t0)
           OR (topic0 IN (unhex('137a44067c8961cd7e1d876f4754a5a3a75989b4552f1843fc69c3b372def160'),
                          unhex('468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5'))
               AND topic1 IN (SELECT h FROM gmx2_names))
    ) TO '{E3}/stage/logs_sel' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (p), APPEND, FILENAME_PATTERN '{tag}_{{uuid}}', ROW_GROUP_SIZE 500000)""")
    log(f"stage logs_sel [{lo},{hi}] {time.time() - t:.0f}s")
    t = time.time()
    v2s, v4s = ("d78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822", "40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f")
    con.execute(f"""COPY (
      WITH s AS (
        SELECT chain, block_number, tx_index, log_index, address, topic0, topic1, topic2, hex(data) AS h FROM logs
        WHERE topic_count = 3 AND topic0 IN (unhex('{v2s}'), unhex('c42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'),
                                             unhex('19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83'), unhex('{v4s}')))
      SELECT chain, block_number, tx_index, log_index, address AS emitter,
        CASE WHEN topic0 = unhex('{v2s}') THEN 'v2' WHEN topic0 = unhex('{v4s}') THEN 'v4' ELSE 'v3' END AS kind,
        CASE WHEN topic0 = unhex('{v4s}') THEN topic1 END AS v4_id,
        CASE WHEN topic0 = unhex('{v2s}') THEN w_uint(substr(h,1,64)) - w_uint(substr(h,129,64))
             WHEN topic0 = unhex('{v4s}') THEN -w_int(substr(h,1,64)) ELSE w_int(substr(h,1,64)) END AS a0,
        CASE WHEN topic0 = unhex('{v2s}') THEN w_uint(substr(h,65,64)) - w_uint(substr(h,193,64))
             WHEN topic0 = unhex('{v4s}') THEN -w_int(substr(h,65,64)) ELSE w_int(substr(h,65,64)) END AS a1,
        CASE WHEN topic0 = unhex('{v2s}') THEN NULL ELSE w_dbl(substr(h,129,64)) END AS sqrtp,
        CASE WHEN topic0 = unhex('{v4s}') THEN NULL ELSE topic2 END AS recipient,
        CASE WHEN topic0 = unhex('{v4s}') THEN topic2 ELSE topic1 END AS sender,
        (block_number // 5000000)::INTEGER AS p
      FROM s
    ) TO '{E3}/stage/swaps' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (p), APPEND, FILENAME_PATTERN '{tag}_{{uuid}}', ROW_GROUP_SIZE 1000000)""")
    log(f"stage swaps [{lo},{hi}] {time.time() - t:.0f}s")
    head = f"{E3}/stage/v2_reserves_head.parquet"
    con.execute(f"""COPY (
      WITH n AS (
        SELECT address AS pair, max(block_number) AS block_number,
               arg_max(w_uint(substr(hex(data),1,64)), block_number * 1000000 + log_index) AS reserve0,
               arg_max(w_uint(substr(hex(data),65,64)), block_number * 1000000 + log_index) AS reserve1
        FROM logs WHERE topic0 = unhex('1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1') AND topic_count = 1
        GROUP BY address)
      SELECT pair, max(block_number) AS block_number, arg_max(reserve0, block_number) AS reserve0, arg_max(reserve1, block_number) AS reserve1
      FROM (SELECT * FROM read_parquet('{head}') UNION ALL BY NAME SELECT * FROM n) GROUP BY pair
    ) TO '{head}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
    os.remove(head)           # may be a hard link to the v1 file: replace the link, never write through it
    os.replace(head + ".tmp", head)
    # contract_topic_hist = v1 base + one histogram file per refreshed range (idempotent per range)
    hdir = f"{E3}/stage/topic_hist"
    os.makedirs(hdir, exist_ok=True)
    con.execute(f"""COPY (SELECT address, topic0, topic_count, count(*) AS n, min(block_number) AS first_block, max(block_number) AS last_block
                          FROM logs GROUP BY ALL) TO '{hdir}/{tag}.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    base = f"{e1(chain)}/contract_topic_hist.parquet"
    hist = f"{E3}/contract_topic_hist.parquet"
    con.execute(f"""COPY (
      SELECT address, topic0, topic_count, sum(n) AS n, min(first_block) AS first_block, max(last_block) AS last_block FROM (
        SELECT * FROM read_parquet('{base}') UNION ALL BY NAME SELECT * FROM read_parquet('{hdir}/*.parquet')) GROUP BY ALL
    ) TO '{hist}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
    if os.path.exists(hist):
        os.remove(hist)       # may be a hard link to the v1 file: never write through it
    os.replace(hist + ".tmp", hist)


def gmx2_inc(con, lo, hi):
    """GMX v2 EventEmitter events for [lo, hi] appended to gmx_v2_events (+ new GM markets appended)."""
    import gmx_v2
    con.execute("""CREATE TABLE IF NOT EXISTS gmx_v2_markets (market VARCHAR, index_token VARCHAR, long_token VARCHAR, short_token VARCHAR)""")
    rows = con.execute("""SELECT lower(hex(data)) FROM logs WHERE address = unhex('c8ee91a54287db53897056e12d9819156d3822fb')
        AND topic1 = unhex('ad5d762f1fc581b3e684cf095d93d3a2c10754f60124b09bec8bf3d76473baaf')""").fetchall()
    have = {r[0] for r in con.execute("SELECT market FROM gmx_v2_markets").fetchall()}
    new = []
    for (h,) in rows:
        try:
            a = gmx_v2.decode(bytes.fromhex(h))["address"]
            if a.get("marketToken") not in have:
                new.append((a.get("marketToken"), a.get("indexToken"), a.get("longToken"), a.get("shortToken")))
        except Exception:
            pass
    if new:
        con.executemany("INSERT INTO gmx_v2_markets VALUES (?, ?, ?, ?)", new)
    if con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'gmx_v2_events'").fetchone()[0] == 0:
        log("gmx2: no gmx_v2_events table (chain without GMX v2)")
        return
    con.execute(f"DELETE FROM gmx_v2_events WHERE block_number BETWEEN {lo} AND {hi}")
    before = con.execute("SELECT count(*) FROM gmx_v2_events").fetchone()[0]
    hashes = ",".join(f"unhex('{h}')" for h in gmx_v2.NAMES)
    q = f"""SELECT block_number, tx_index, log_index, '0x' || lower(hex(address)), lower(hex(topic1)), h FROM logs_sel
            WHERE topic0 IN (unhex('137a44067c8961cd7e1d876f4754a5a3a75989b4552f1843fc69c3b372def160'),
                             unhex('468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5'))
              AND topic1 IN ({hashes}) AND block_number BETWEEN {lo} AND {hi}"""
    _decode_into(con, q)
    log(f"gmx2: +{con.execute('SELECT count(*) FROM gmx_v2_events').fetchone()[0] - before} events, +{len(new)} markets")


def _decode_into(con, q):
    """Decode GMX v2 EventLog rows with the econ decoder; one INSERT at the end (inserting batch by batch while a
    cursor on the same database is open stalled for 20+ minutes on DuckDB 1.4.5)."""
    import pyarrow as pa
    import gmx_v2
    # TEMP views are per connection: materialize into a plain table the cursor can read
    con.execute(f"CREATE OR REPLACE TABLE _gmx_inc_raw AS {q}")
    raw = con.execute("SELECT * FROM _gmx_inc_raw").fetchall()
    con.execute("DROP TABLE IF EXISTS _gmx_inc_raw")
    names = ["block_number", "tx_index", "log_index", "contract", "event_name", "account", "market", "collateral_token",
             "is_long", "size_in_usd", "size_delta_usd", "collateral_amount", "collateral_delta_amount", "order_type",
             "position_key", "execution_price"]
    batch = []
    c = gmx_v2.clamp
    for bn, ti, li, addr, t1, h in raw:
        try:
            d = gmx_v2.decode(bytes.fromhex(h))
        except Exception:
            continue
        a, ui, ii, bo, b32 = d["address"], d["uint"], d["int"], d["bool"], d["bytes32"]
        cda = ui.get("collateralDeltaAmount", ii.get("collateralDeltaAmount"))
        batch.append((bn, ti, li, addr, gmx_v2.NAMES[t1], a.get("account"), a.get("market"), a.get("collateralToken"),
                      bo.get("isLong"), c(ui.get("sizeInUsd")), c(ui.get("sizeDeltaUsd")), c(ui.get("collateralAmount")), c(cda),
                      ui.get("orderType"), b32.get("positionKey"), c(ui.get("executionPrice"))))
    del raw
    if not batch:
        return
    cols = list(zip(*batch))
    arrs = []
    for nm, col in zip(names, cols):
        if nm in ("size_in_usd", "size_delta_usd", "collateral_amount", "collateral_delta_amount", "execution_price"):
            arrs.append(pa.array([None if v is None else str(v) for v in col], pa.string()))
        elif nm in ("block_number", "tx_index", "log_index", "order_type"):
            arrs.append(pa.array(col, pa.int64()))
        elif nm == "is_long":
            arrs.append(pa.array(col, pa.bool_()))
        else:
            arrs.append(pa.array(col, pa.string()))
    con.register("gmx_batch", pa.Table.from_arrays(arrs, names=names))
    con.execute("""INSERT INTO gmx_v2_events SELECT block_number, tx_index, log_index, contract, event_name, account, market,
        collateral_token, is_long, size_in_usd::HUGEINT, size_delta_usd::HUGEINT, collateral_amount::HUGEINT,
        collateral_delta_amount::HUGEINT, order_type, position_key, execution_price::HUGEINT FROM gmx_batch""")
    con.unregister("gmx_batch")


def actions_inc(con, chain, lo, hi):
    E3 = e3(chain)
    out = f"{E3}/econ_actions/part_{lo}.parquet"
    if os.path.exists(out):
        os.remove(out)          # a re-run of the same range replaces its part
    bind_stage(con, chain, lo, hi, materialize=True)
    params = {"OUT": E3, "SRC": SRC, "CHAIN": CHAINS[chain]["id"], "LO": lo, "HI": hi, "PLO": lo, "PHI": hi}
    run_sql(con, f"{SRC}/30_actions.sql", params, label=f"{chain}:actions[{lo}-{hi}]")
    n = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    log(f"actions [{lo},{hi}]: {n} rows")
    return n


def tokens_inc(con, chain, los):
    """symbol/name/decimals for new tokens seen in the new actions (cached in meta_calls forever).
    los: first blocks of the action parts to scan (a part whose tokens step failed is retried by the next run)."""
    import meta
    E3 = e3(chain)
    parts = [f"{E3}/econ_actions/part_{lo}.parquet" for lo in ([los] if isinstance(los, int) else los)]
    parts = "[" + ", ".join(f"'{p}'" for p in parts if os.path.exists(p)) + "]"
    if parts == "[]":
        return
    con.execute(f"""CREATE OR REPLACE TABLE token_candidates AS
        SELECT token, sum(score) AS score FROM (
          SELECT asset AS token, count(*) * 1.0 AS score FROM read_parquet({parts}, union_by_name=true)
          WHERE asset LIKE '0x%' AND length(asset) = 42 GROUP BY 1
          UNION ALL SELECT json_extract_string(extra_json, '$.token_out'), count(*) FROM read_parquet({parts}, union_by_name=true)
          WHERE action = 'swap' GROUP BY 1
          UNION ALL SELECT token, 1e12 FROM issuer_tokens
          UNION ALL SELECT loan_token, 1e9 FROM dim_morpho_markets UNION ALL SELECT collateral_token, 1e9 FROM dim_morpho_markets
          UNION ALL SELECT underlying, 1e9 FROM dim_aave_tokens)
        WHERE token IS NOT NULL AND token <> '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
          AND token NOT IN (SELECT address FROM meta_calls WHERE getter = 'decimals()') GROUP BY token""")
    meta.run_tokens(con, 1500)


def prices(con, chain, lo):
    """50_prices.sql with an incremental prices_daily_dex: days before the day of block `lo` are kept, later days recomputed."""
    E3 = e3(chain)
    # recompute from one UTC day before the first new block (also replaces any partial last day of the previous run)
    day_lo = con.execute(f"SELECT (date_trunc('day', to_timestamp(min(timestamp)::BIGINT)) - INTERVAL 1 DAY)::TIMESTAMP FROM blocks WHERE number = {lo}").fetchone()[0]
    if day_lo is None:
        day_lo = con.execute(f"SELECT (date_trunc('day', to_timestamp(min(timestamp)::BIGINT)) - INTERVAL 1 DAY)::TIMESTAMP FROM blocks").fetchone()[0]
    from common import BLOCK_MINUTES
    blk_lo = con.execute(f"""SELECT min(first_block) FROM read_parquet('{BLOCK_MINUTES.format(chain=chain)}')
                             WHERE minute >= epoch(TIMESTAMP '{day_lo}')""").fetchone()[0]
    has_dex = con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'prices_daily_dex'").fetchone()[0]

    def replace(stmt):
        return stmt.startswith("CREATE OR REPLACE TABLE prices_daily_dex AS")

    def run_dex(stmt):
        body = stmt[len("CREATE OR REPLACE TABLE prices_daily_dex AS"):]
        body = body.replace("FROM econ_actions WHERE action = 'swap'",
                            f"FROM econ_actions WHERE block_number >= {blk_lo} AND action = 'swap'")
        if has_dex:
            con.execute(f"DELETE FROM prices_daily_dex WHERE day >= TIMESTAMPTZ '{day_lo}+00' - INTERVAL 1 HOUR")
            con.execute("INSERT INTO prices_daily_dex " + body)
        else:
            con.execute(stmt)
        log(f"prices_daily_dex: recomputed from {day_lo} (block {blk_lo})")
    con.execute(f"CREATE OR REPLACE VIEW econ_actions AS SELECT * FROM read_parquet('{E3}/econ_actions/*.parquet', union_by_name=true)")
    params = {"OUT": E3, "SRC": SRC, "CHAIN": CHAINS[chain]["id"], "LO": 0, "HI": 1 << 62}
    _run_with_hook(con, f"{SRC}/50_prices.sql", params, replace, run_dex, label=f"{chain}:prices")


def _run_with_hook(con, path, params, match, hook, label):
    pending = []

    def skip(s):
        if match(s):
            pending.append(s)
            return True
        if pending:
            hook(pending.pop())
        return False
    run_sql(con, path, params, label=label, skip=skip)
    while pending:
        hook(pending.pop())


SHEETS_A_V3 = """
CREATE OR REPLACE VIEW econ_actions AS SELECT * FROM read_parquet('${OUT}/econ_actions/*.parquet', union_by_name=true);
CREATE OR REPLACE TABLE focus_tokens AS
SELECT token, kind FROM issuer_tokens UNION SELECT token, 'weth' FROM dim_weth
UNION SELECT token, 'stable_anchor' FROM stable_anchor;

-- token balances now = wallet map (exact sign / DOUBLE net up to the snapshot) + all transfers since the snapshot
CREATE OR REPLACE TEMP VIEW bal_src AS
  SELECT address, token, net_approx AS net FROM read_parquet('${MAP}/address_tokens/**/*.parquet') WHERE kind = 'erc20'
  UNION ALL
  SELECT address, token, net FROM read_parquet('${DELTA}/address_tokens.parquet') WHERE kind = 'erc20';

CREATE OR REPLACE TABLE focus_balances AS
WITH ft AS (SELECT unhex(substr(token, 3)) AS tb FROM focus_tokens)
SELECT a2s(address) AS wallet, a2s(token) AS token, sum(net) AS balance_raw, NULL::BIGINT AS n_transfers
FROM bal_src SEMI JOIN ft ON ft.tb = bal_src.token
WHERE address <> unhex('0000000000000000000000000000000000000000') GROUP BY ALL HAVING sum(net) > 0.5;

CREATE OR REPLACE TABLE sheet_wallets AS
SELECT wallet, max(why) AS why FROM (
  SELECT wallet, 'position' AS why FROM positions_head WHERE abs(amount) > 0 AND kind <> 'lp'
  UNION ALL SELECT wallet, 'lp' FROM (SELECT wallet, sum(liquidity) l FROM lp_positions_valued GROUP BY 1 ORDER BY l DESC LIMIT 20000)
  UNION ALL SELECT wallet, 'active' FROM (SELECT wallet, count(*) c FROM econ_actions WHERE action NOT IN ('swap', 'corporate_action')
                                          GROUP BY 1 ORDER BY c DESC LIMIT 3000)
  UNION ALL SELECT wallet, 'focus_holder' FROM (SELECT wallet, row_number() OVER (PARTITION BY token ORDER BY balance_raw DESC) rk FROM focus_balances) WHERE rk <= 50
) WHERE wallet IS NOT NULL GROUP BY wallet;

CREATE OR REPLACE TABLE liquid_balances AS
WITH sw AS (SELECT unhex(substr(wallet, 3)) AS wb FROM sheet_wallets)
SELECT a2s(address) AS wallet, a2s(token) AS token, sum(net) AS balance_raw, NULL::BIGINT AS n_transfers
FROM bal_src SEMI JOIN sw ON sw.wb = bal_src.address
WHERE abs(net) < 1e30 GROUP BY ALL HAVING sum(net) > 0.5;
COPY focus_balances TO '${OUT}/focus_balances.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY liquid_balances TO '${OUT}/liquid_balances_sheet_wallets.parquet' (FORMAT parquet, COMPRESSION zstd);

CREATE OR REPLACE TABLE stock_multiplier AS
SELECT asset AS token, arg_max(TRY_CAST(json_extract_string(extra_json, '$.new_multiplier_e18') AS DOUBLE) / 1e18, block_number) AS multiplier
FROM econ_actions WHERE action = 'corporate_action' AND json_extract_string(extra_json, '$.new_multiplier_e18') IS NOT NULL GROUP BY 1;

CREATE OR REPLACE TABLE position_tokens AS
SELECT token, 'aave' AS why FROM dim_aave_tokens UNION SELECT comet, 'comet' FROM dim_comets UNION SELECT ctoken, 'ctoken' FROM dim_ctokens;

CREATE OR REPLACE TABLE liquid_usd AS
SELECT l.wallet, l.token, l.balance_raw, tm.symbol, tm.decimals,
       l.balance_raw::DOUBLE / pow(10, coalesce(tm.decimals, 18)) AS amount,
       CASE WHEN ph.source = 'dex_vwap' AND l.balance_raw::DOUBLE / pow(10, coalesce(tm.decimals, 18)) * ph.price_usd > 5 * ph.vol_30d_usd THEN NULL
            ELSE l.balance_raw::DOUBLE / pow(10, coalesce(tm.decimals, 18)) * ph.price_usd END AS usd,
       CASE WHEN dp.pool IS NOT NULL THEN 'lp' WHEN pt.token IS NOT NULL THEN 'position_token' ELSE 'liquid' END AS category
FROM liquid_balances l LEFT JOIN token_meta tm ON tm.token = l.token LEFT JOIN prices_head ph ON ph.token = l.token
LEFT JOIN (SELECT pool FROM dim_pools WHERE kind = 'v2') dp ON dp.pool = l.token LEFT JOIN position_tokens pt ON pt.token = l.token;
"""


def sheets(con, chain):
    """60_sheets_a (balances from map + delta, v2 LP totalSupply via eth_call) + ETH balances + 61_sheets_b."""
    import meta
    import rpc
    E3 = e3(chain)
    path = f"{E3}/_sheets_a_v3.sql"
    open(path, "w").write(SHEETS_A_V3)
    params = {"OUT": E3, "SRC": SRC, "CHAIN": CHAINS[chain]["id"], "MAP": f"{DATA}/map/{chain}", "DELTA": os.path.realpath(f"{DATA}/map/{chain}/_delta")}
    run_sql(con, path, params, label=f"{chain}:sheets_a_v3")
    pairs = [r[0] for r in con.execute("SELECT DISTINCT token FROM liquid_usd WHERE category = 'lp'").fetchall()]
    # RPC-derived inputs keep the previous run's values when the endpoint pool gives up (step is then 'degraded')
    try:
        res = rpc.aggregate3([(p, rpc.SEL["totalSupply()"]) for p in pairs], chunk=200) if pairs else []
        rows = [(p, str(int.from_bytes(b[:32], "big"))) for p, (ok, b) in zip(pairs, res) if ok and len(b) >= 32]
        con.execute("CREATE OR REPLACE TABLE v2_lp_supply (pair VARCHAR, total_supply HUGEINT)")
        if rows:
            con.executemany("INSERT INTO v2_lp_supply VALUES (?, ?::HUGEINT)", rows)
        log(f"v2 LP totalSupply: {len(rows)}/{len(pairs)} pairs")
    except rpc.RpcError as e:
        con.execute("CREATE TABLE IF NOT EXISTS v2_lp_supply (pair VARCHAR, total_supply HUGEINT)")
        warn(f"v2 LP totalSupply: RPC failed ({e}); using the previous run's {con.execute('SELECT count(*) FROM v2_lp_supply').fetchone()[0]} pairs")
    con.execute("DROP TABLE IF EXISTS meta_eth_balance_prev")
    if con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'meta_eth_balance'").fetchone()[0]:
        con.execute("ALTER TABLE meta_eth_balance RENAME TO meta_eth_balance_prev")
    try:
        meta.eth_balances(con, 1000)
        con.execute("DROP TABLE IF EXISTS meta_eth_balance_prev")
    except rpc.RpcError as e:
        con.execute("DROP TABLE IF EXISTS meta_eth_balance")
        if con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'meta_eth_balance_prev'").fetchone()[0]:
            con.execute("ALTER TABLE meta_eth_balance_prev RENAME TO meta_eth_balance")
        warn(f"ETH balances: RPC failed ({e}); using the previous run's balances")
    run_sql(con, f"{SRC}/61_sheets_b.sql", {"OUT": E3, "SRC": SRC, "CHAIN": CHAINS[chain]["id"]}, label=f"{chain}:sheets_b")


def run(chain, lo, hi, steps=None, token_los=None):
    """Process blocks [lo, hi]. Returns dict of counters."""
    os.environ.setdefault("RPC_BUDGET", "1500")
    import rpc
    if [u for u in rpc.POOL["urls"]] != CHAINS[chain]["rpcs"]:
        rpc.configure(CHAINS[chain]["rpcs"])
    import meta  # noqa: F401
    init(chain)
    E3 = e3(chain)
    steps = steps or ["stage", "dims", "meta", "gmx2", "actions", "tokens", "positions", "prices", "sheets", "focus"]
    con = connect(f"{E3}/econ.duckdb", threads=3, mem="8GB")
    con.execute(open(f"{SRC}/gen_macros.sql").read())
    nfiles = bind_inc_views(con, chain, lo, hi)
    log(f"{chain}: [{lo},{hi}] from {nfiles} source files")
    params = {"OUT": E3, "SRC": SRC, "CHAIN": CHAINS[chain]["id"], "LO": 0, "HI": 1 << 62}
    stats = {}
    if "stage" in steps:
        stage_inc(con, chain, lo, hi)
    if "dims" in steps:
        bind_stage(con, chain)
        run_sql(con, f"{SRC}/20_dims.sql", params, label=f"{chain}:dims")
    if "meta" in steps:
        meta.run_meta(con, CHAINS[chain]["id"])
        # description()/decimals() of every verified Chainlink aggregator (prices use the official decimals)
        import csv
        vagg = [r["aggregator"] for r in csv.DictReader(open(f"{SRC}/chainlink_verified.csv")) if r["chain"] == str(CHAINS[chain]["id"])]
        n = meta.call_many(con, [(x, g) for x in vagg for g in ("description()", "decimals()")])
        log(f"meta: {n} verified-aggregator calls")
        run_sql(con, f"{SRC}/25_metaload.sql", params, label=f"{chain}:metaload")
    if "gmx2" in steps:
        bind_stage(con, chain, lo, hi)
        gmx2_inc(con, lo, hi)
    if "actions" in steps:
        stats["actions"] = actions_inc(con, chain, lo, hi)
    if "tokens" in steps:
        tokens_inc(con, chain, token_los or lo)
        run_sql(con, f"{SRC}/25_metaload.sql", params, label=f"{chain}:metaload2")
    if "positions" in steps:
        bind_stage(con, chain)
        con.execute(f"CREATE OR REPLACE VIEW econ_actions AS SELECT * FROM read_parquet('{E3}/econ_actions/*.parquet', union_by_name=true)")
        path = f"{E3}/_40_positions.sql"
        # pos_stream / pos_running are per-run intermediates: persistent tables (TEMP ones spilled and made the step ~3x
        # slower under memory pressure), dropped after the step so their blocks are reused instead of growing the file
        open(path, "w").write(open(f"{SRC}/40_positions.sql").read().replace(
            "read_parquet('${OUT}/econ_actions/*.parquet')", "read_parquet('${OUT}/econ_actions/*.parquet', union_by_name=true)"))
        con.execute("SET memory_limit='%s'" % cap_mem("9GB"))   # window functions cannot spill: the two big ones run per wallet bucket
        nb = 16 if cap_mem("9GB") == "9GB" else 32                    # a capped limit gets smaller buckets

        def is_bucketed(st):
            return st.startswith("CREATE OR REPLACE TABLE pos_running AS") or "CREATE OR REPLACE TABLE positions_monthly AS" in st

        def bucketed(stmt, n=nb):
            """pos_running / positions_monthly: same query per wallet bucket (all partitions and groups include the wallet,
            so the union of buckets is exact)."""
            if stmt.startswith("CREATE OR REPLACE TABLE pos_running AS"):
                name, body = "pos_running", stmt[len("CREATE OR REPLACE TABLE pos_running AS"):]
                src, filt = "FROM pos_stream", "FROM (SELECT * FROM pos_stream WHERE hash(wallet) % {n} = {i})"
            else:
                name, body = "positions_monthly", stmt[stmt.index("CREATE OR REPLACE TABLE positions_monthly AS") + len("CREATE OR REPLACE TABLE positions_monthly AS"):]
                src, filt = "FROM pos_running WHERE block_time IS NOT NULL", "FROM pos_running WHERE block_time IS NOT NULL AND hash(wallet) % {n} = {i}"
            for i in range(n):
                b = body.replace(src, filt.format(n=n, i=i), 1)
                con.execute((f"CREATE OR REPLACE TABLE {name} AS " if i == 0 else f"INSERT INTO {name} ") + b)
            log(f"{name}: {con.execute(f'SELECT count(*) FROM {name}').fetchone()[0]} rows in {n} wallet buckets")
        _run_with_hook(con, path, params, is_bucketed, bucketed, label=f"{chain}:positions")
        for t in ("pos_stream", "pos_running"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.execute("SET memory_limit='%s'" % cap_mem("8GB"))
    if "prices" in steps:
        bind_stage(con, chain)
        prices(con, chain, lo)
    if "sheets" in steps:
        sheets(con, chain)
    if "focus" in steps:
        path = f"{E3}/_80_focus.sql"
        open(path, "w").write(open(f"{SRC}/80_focus.sql").read().replace(
            "read_parquet('${OUT}/econ_actions/*.parquet')", "read_parquet('${OUT}/econ_actions/*.parquet', union_by_name=true)"))
        run_sql(con, path, params, label=f"{chain}:focus")
    con.execute("CHECKPOINT")
    con.close()
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("chain")
    ap.add_argument("lo", type=int)
    ap.add_argument("hi", type=int)
    ap.add_argument("--steps", default=None)
    a = ap.parse_args()
    print(json.dumps(run(a.chain, a.lo, a.hi, a.steps.split(",") if a.steps else None)))
