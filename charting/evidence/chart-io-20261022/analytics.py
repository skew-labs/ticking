"""Economic Machine token analytics for Ticking (served by em-charts under /api/public/em/*).

Two layers per chain, all under data/<chain>/analytics/:
  static (build_static, after a deep rebuild):  dims (Morpho markets + LLTV, Aave pools/aTokens, vaults, OFT adapters),
      holders at the map snapshot, wallet classes, transfer deltas and lending events for the Blob-gap chunks,
      econ positions (v3 when present, else v2) and Aave liquidation thresholds read on-chain (getConfiguration).
  periodic (refresh, every 5 min in the server): transfer deltas and lending events of new follower chunks, then
      token state, liquidation positions, flows, events and briefs for every served market -> analytics.sqlite3 (WAL).
Endpoints only read analytics.sqlite3 (plus current prices from emread), so they answer in milliseconds.
usage: analytics.py <chain> static | refresh | all
"""
import glob
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

from common import EM, ROOT, connect

RT = "/srv/skew/em-realtime/out"
SNAP_END = {"robinhood": 88098834, "arbitrum": 512450747, "arc": 26859999}   # last block of the map snapshot (dex_trades / address_tokens)
ECON_DB = {"robinhood": f"{EM}/econ/robinhood/econ2.duckdb", "arbitrum": f"{EM}/econ/arbitrum/econ.duckdb", "arc": f"{EM}/econ/arc/econ.duckdb"}
RPC = {"robinhood": "https://robinhood-rpc.publicnode.com", "arbitrum": "https://arbitrum-one-rpc.publicnode.com", "arc": "https://arc-rpc.publicnode.com"}
BURN = ("0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead")
LIVE_DB = "/srv/skew/em-galaxy/live/live.db"
# topic0 (from the econ registry, events.yaml / topic_dict.csv)
T = {
    "m_supply_coll": "a3b9472a1399e17e123f3c2e6586c23e504184d504de59cdaa2b375e880c6184",
    "m_withdraw_coll": "e80ebd7cc9223d7382aab2e0d1d6155c65651f83d53c8b9b06901d167e321142",
    "m_borrow": "570954540bed6b1304a87dfe815a5eda4a648f7097a16240dcd85c9b5fd42a43",
    "m_repay": "52acb05cebbd3cd39715469f22afbf5a17496295ef3bc9bb5944056c63ccaa09",
    "m_liquidate": "a4946ede45d0c6f06a0f5ce92c9ad3b4751452d2fe0e25010783bcab57a67e41",
    "a_supply": "2b627736bca15cd5381dcf80b0bf11fd197d01a037c52b927a881a10fb73ba61",
    "a_withdraw": "3115d1449a7b732c986cba18244e897a450f61e1bb8d589cd2e69e6c8924f9f7",
    "a_borrow": "b3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0",
    "a_repay": "a534c8dbe71f871f9f3530e97a74601fea17b426cae02e1c5aee42c96c784051",
    "a_liquidation": "e413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286",
}
BAD_Q = ("unverified_principal", "morpho_interest_runaway", "reserve_unbacked", "decoded_exceeds_onchain")
LABEL_CLASS = {"cex_hot_wallet": "exchange", "cex_deposit": "exchange", "bridge": "bridge", "mev_bot": "mev",
               "eoa_whale": "whale_label", "lending": "lending", "dex_pool": "dex", "multisig_or_safe": "contract",
               "contract_other": "contract", "token_contract": "contract", "dex_router": "contract", "nft_contract": "contract"}


def d(chain):
    p = f"{ROOT}/data/{chain}/analytics"
    os.makedirs(p, exist_ok=True)
    return p


def econ_source(chain):
    """Prefer econ v3 ("as of now") when its sheet rows exist, else v2. Returns (sheet_rows path, version, as_of dict)."""
    base = f"{EM}/econ/{chain}"
    for name in ("v3/sheet_rows.v3.parquet", "v3/sheet_rows.parquet"):
        p = f"{base}/{name}"
        if os.path.exists(p):
            meta = {}
            for mp in (f"{base}/v3/AS_OF.json", f"{base}/v3/as_of.json"):
                if os.path.exists(mp):
                    meta = json.load(open(mp))
            return p, "v3", meta
    return f"{base}/v2/sheet_rows.v2.parquet", "v2", {"as_of_block": SNAP_END[chain]}


def rpc_call(chain, to, data):
    req = urllib.request.Request(RPC[chain], headers={"content-type": "application/json", "user-agent": "em-charts/1.0"},
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                                  "params": [{"to": to, "data": data}, "latest"]}).encode())
    return json.load(urllib.request.urlopen(req, timeout=15)).get("result")


AMT = ("CASE WHEN left(hex({c}), 32) = '00000000000000000000000000000000' "
       "THEN ('0x' || substr(hex({c}), 33, 16))::UBIGINT::DOUBLE * 18446744073709551616.0 "
       "+ ('0x' || substr(hex({c}), 49, 16))::UBIGINT::DOUBLE END")


def word(i):
    """i-th 32-byte data word of a log as DOUBLE (values < 2^128)."""
    o = 64 * i
    return (f"CASE WHEN substr(hex(data), {o + 1}, 32) = '00000000000000000000000000000000' "
            f"THEN ('0x' || substr(hex(data), {o + 33}, 16))::UBIGINT::DOUBLE * 18446744073709551616.0 "
            f"+ ('0x' || substr(hex(data), {o + 49}, 16))::UBIGINT::DOUBLE END")


def tadr(t):
    return f"'0x' || lower(right(hex({t}), 40))"


LEND_SQL = """
SELECT block_number AS bn, tx_index, log_index, lower(hex(topic0)) AS t0, '0x' || lower(hex(address)) AS contract,
       '0x' || lower(hex(topic1)) AS id32, {a1} AS a1, {a2} AS a2, {a3} AS a3,
       {w0} AS w0, {w1} AS w1, {w2} AS w2, {w3} AS w3, '0x' || lower(right(hex(data), 40)) AS dlast,
       '0x' || lower(substr(hex(data), 25, 40)) AS dw0addr, '0x' || lower(substr(hex(data), 153, 40)) AS dw2addr
FROM read_parquet([{files}]) WHERE block_number > {low} AND lower(hex(topic0)) IN ({topics})
"""


def decode_lending(c, files, low):
    """Raw Morpho Blue / Aave v3 lending logs -> normalized events (protocol, kind, market/reserve, wallet, asset, amounts)."""
    lst = ",".join("'%s'" % f for f in files)
    tl = ",".join("'%s'" % v for v in T.values())
    q = LEND_SQL.format(a1=tadr("topic1"), a2=tadr("topic2"), a3=tadr("topic3"), w0=word(0), w1=word(1), w2=word(2), w3=word(3),
                        files=lst, low=low, topics=tl)
    c.execute(f"CREATE OR REPLACE TEMP TABLE raw_lend AS {q}")
    # Morpho: contract must be a known Morpho Blue (or fork) singleton; id -> market params
    c.execute(f"""CREATE OR REPLACE TEMP TABLE lend_new AS
      SELECT r.bn, r.tx_index, r.log_index, mm.protocol, CASE r.t0
               WHEN '{T['m_supply_coll']}' THEN 'supply_collateral' WHEN '{T['m_withdraw_coll']}' THEN 'withdraw_collateral'
               WHEN '{T['m_borrow']}' THEN 'borrow' WHEN '{T['m_repay']}' THEN 'repay' WHEN '{T['m_liquidate']}' THEN 'liquidate' END AS kind,
             r.id32 AS market, CASE WHEN r.t0 IN ('{T['m_borrow']}', '{T['m_repay']}') THEN mm.loan_token ELSE mm.collateral_token END AS asset,
             mm.collateral_token, mm.loan_token,
             CASE r.t0 WHEN '{T['m_supply_coll']}' THEN r.a3 WHEN '{T['m_withdraw_coll']}' THEN r.a2 WHEN '{T['m_borrow']}' THEN r.a2
                       WHEN '{T['m_repay']}' THEN r.a3 WHEN '{T['m_liquidate']}' THEN r.a3 END AS wallet,
             CASE r.t0 WHEN '{T['m_supply_coll']}' THEN r.w0 WHEN '{T['m_withdraw_coll']}' THEN r.w1 WHEN '{T['m_borrow']}' THEN r.w1
                       WHEN '{T['m_repay']}' THEN r.w0 WHEN '{T['m_liquidate']}' THEN r.w2 END AS amount_raw,
             CASE WHEN r.t0 = '{T['m_liquidate']}' THEN r.w0 END AS repaid_raw,
             CASE WHEN r.t0 = '{T['m_liquidate']}' THEN r.w3 END AS bad_debt_raw,
             CASE WHEN r.t0 = '{T['m_liquidate']}' THEN r.a2 END AS liquidator
      FROM raw_lend r JOIN dim_morpho mm ON mm.morpho = r.contract AND mm.id = r.id32
      WHERE r.t0 IN ('{T['m_supply_coll']}', '{T['m_withdraw_coll']}', '{T['m_borrow']}', '{T['m_repay']}', '{T['m_liquidate']}')
      UNION ALL
      SELECT r.bn, r.tx_index, r.log_index, ap.protocol, CASE r.t0
               WHEN '{T['a_supply']}' THEN 'supply' WHEN '{T['a_withdraw']}' THEN 'withdraw' WHEN '{T['a_borrow']}' THEN 'borrow'
               WHEN '{T['a_repay']}' THEN 'repay' WHEN '{T['a_liquidation']}' THEN 'liquidate' END,
             r.contract, r.a1, CASE WHEN r.t0 = '{T['a_liquidation']}' THEN r.a1 END, CASE WHEN r.t0 = '{T['a_liquidation']}' THEN r.a2 END,
             CASE r.t0 WHEN '{T['a_supply']}' THEN r.a2 WHEN '{T['a_withdraw']}' THEN r.a2 WHEN '{T['a_borrow']}' THEN r.a2
                       WHEN '{T['a_repay']}' THEN r.a2 WHEN '{T['a_liquidation']}' THEN r.a3 END,
             CASE r.t0 WHEN '{T['a_supply']}' THEN r.w1 WHEN '{T['a_withdraw']}' THEN r.w0 WHEN '{T['a_borrow']}' THEN r.w1
                       WHEN '{T['a_repay']}' THEN r.w0 WHEN '{T['a_liquidation']}' THEN r.w1 END,
             CASE WHEN r.t0 = '{T['a_liquidation']}' THEN r.w0 END, NULL,
             CASE WHEN r.t0 = '{T['a_liquidation']}' THEN r.dw2addr END
      FROM raw_lend r JOIN dim_aave_pools ap ON ap.pool = r.contract
      WHERE r.t0 IN ('{T['a_supply']}', '{T['a_withdraw']}', '{T['a_borrow']}', '{T['a_repay']}', '{T['a_liquidation']}')""")
    # Aave v3 owners: Supply/Borrow -> onBehalfOf (topic2), Withdraw -> user (topic2), Repay -> user (topic2),
    # LiquidationCall -> user (topic3); amounts: Supply/Borrow word 1, Withdraw/Repay word 0, LiquidationCall seized word 1.
    return c.execute("SELECT count(*) FROM lend_new").fetchone()[0]


# ---------------------------------------------------------------- static layer
def export_dims(chain):
    out = f"{d(chain)}/dims"
    os.makedirs(out, exist_ok=True)
    import duckdb
    src = duckdb.connect(ECON_DB[chain], read_only=True)
    src.execute(f"SET threads=2; SET memory_limit='2GB'; SET temp_directory='{ROOT}/tmp/dims-{os.getpid()}'; SET max_temp_directory_size='2GB'")
    q = {
        "morpho": "SELECT morpho, id, loan_token, collateral_token, oracle, lltv::DOUBLE / 1e18 AS lltv, protocol FROM dim_morpho_markets",
        "aave_pools": "SELECT pool, fam, protocol FROM dim_aave_pools",
        "aave_tokens": "SELECT token, underlying, pool, kind FROM dim_aave_tokens",
        "vaults": "SELECT vault FROM dim_vaults",
        "ctokens": "SELECT ctoken, protocol, underlying FROM dim_ctokens",
        "comets": "SELECT comet, protocol, base_token FROM dim_comets",
        "oft": "SELECT oft, protocol FROM dim_oft",
        "pools": "SELECT pool, protocol, kind FROM dim_pools",
        "v4_managers": "SELECT manager, any_value(protocol) AS protocol, count(*) AS pools FROM dim_v4_pools GROUP BY manager",
    }
    for name, sql in q.items():
        src.execute(f"COPY ({sql}) TO '{out}/{name}.parquet' (FORMAT parquet)")
    src.close()
    lite = f"/srv/skew/em-galaxy/live/dims_{chain}.sqlite"
    c = duckdb.connect()
    c.execute(f"SET threads=2; SET memory_limit='1GB'; SET temp_directory='{ROOT}/tmp/dims-{os.getpid()}'; SET max_temp_directory_size='2GB'")
    c.execute(f"SET home_directory='{os.path.expanduser('~')}'")   # the worker has no HOME (same dir as common.connect: ~/.duckdb extensions)
    c.execute("INSTALL sqlite; LOAD sqlite")
    if os.path.exists(lite):
        c.execute(f"ATTACH '{lite}' AS dl (TYPE sqlite, READ_ONLY)")
        c.execute(f"COPY (SELECT lower(address) AS address, family, protocol, role FROM dl.contracts) TO '{out}/registry.parquet' (FORMAT parquet)")
        c.execute(f"COPY (SELECT lower(market) AS market, index_symbol FROM dl.gmx_markets) TO '{out}/gmx_markets.parquet' (FORMAT parquet)")
    else:
        c.execute(f"COPY (SELECT NULL::VARCHAR address, NULL::VARCHAR family, NULL::VARCHAR protocol, NULL::VARCHAR role WHERE false) TO '{out}/registry.parquet' (FORMAT parquet)")
        c.execute(f"COPY (SELECT NULL::VARCHAR market, NULL::VARCHAR index_symbol WHERE false) TO '{out}/gmx_markets.parquet' (FORMAT parquet)")
    c.close()


def load_dims(c, chain):
    dd = f"{d(chain)}/dims"
    if not os.path.exists(f"{dd}/morpho.parquet"):
        export_dims(chain)
    c.execute(f"CREATE OR REPLACE TABLE dim_morpho AS SELECT * FROM '{dd}/morpho.parquet'")
    c.execute(f"CREATE OR REPLACE TABLE dim_aave_pools AS SELECT * FROM '{dd}/aave_pools.parquet'")
    for n in ("aave_tokens", "vaults", "ctokens", "comets", "oft", "pools", "v4_managers", "registry", "gmx_markets"):
        c.execute(f"CREATE OR REPLACE TABLE dim_{n} AS SELECT * FROM '{dd}/{n}.parquet'")


def served(c, chain):
    c.execute(f"""CREATE OR REPLACE TABLE mk AS
      SELECT m.token, m.symbol, m.kind, coalesce(t.decimals, 18) AS decimals, from_hex(substr(m.token, 3)) AS tb
      FROM read_parquet('{ROOT}/data/{chain}/markets_export.parquet') m
      LEFT JOIN (SELECT lower(token) AS token, any_value(decimals) AS decimals FROM read_parquet('{EM}/econ/{chain}/token_meta.parquet')
                 GROUP BY 1) t ON t.token = m.token""")
    c.execute(f"UPDATE mk SET decimals = 18 WHERE token = '{BURN[0]}'")


def tail_files(chain, kind, low):
    out = []
    for f in glob.glob(f"{ROOT}/tail/{chain}/{kind}/*.parquet"):
        a, b = map(int, re.search(r"_(\d+)-(\d+)\.parquet$", f).groups())
        if b > low:
            out.append(f)
    return sorted(out)


def rt_files(chain, kind, low, high=None):
    out = []
    for f in glob.glob(f"{RT}/{chain}/{kind}/*.parquet"):
        a, b = map(int, re.search(r"_(\d+)-(\d+)\.parquet$", f).groups())
        m = f"{RT}/{chain}/manifests/{a:010d}-{b:010d}.json"
        if b > low and (high is None or b <= high) and os.path.exists(m):
            out.append(f)
    return sorted(out)


XFER_SQL = """
WITH t AS (SELECT block_number AS bn, token, "from", "to", {amt} AS a FROM read_parquet([{files}])
           WHERE block_number > {low} AND standard = 'ERC20')
SELECT t.bn, mk.token, '0x' || lower(hex(t."to")) AS holder, t.a / 10 ^ mk.decimals AS delta FROM t JOIN mk ON mk.tb = t.token WHERE t.a IS NOT NULL
UNION ALL
SELECT t.bn, mk.token, '0x' || lower(hex(t."from")), -t.a / 10 ^ mk.decimals FROM t JOIN mk ON mk.tb = t.token WHERE t.a IS NOT NULL
"""


def build_static(chain):
    t0 = time.time()
    out = d(chain)
    c = connect(threads=4, mem="6GB")
    export_dims(chain)
    load_dims(c, chain)
    served(c, chain)
    snap = SNAP_END[chain]
    # holders at the map snapshot (all holders of served tokens)
    c.execute(f"""COPY (SELECT mk.token, '0x' || lower(hex(a.address)) AS holder, a.net_approx / 10 ^ mk.decimals AS amount
                        FROM read_parquet('{EM}/map/{chain}/address_tokens/*.parquet') a JOIN mk ON mk.tb = a.token
                        WHERE a.kind = 'erc20' AND a.net_approx > 0)
                  TO '{out}/holders_snap.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    print(chain, "holders_snap", c.execute(f"SELECT count(*) FROM '{out}/holders_snap.parquet'").fetchone()[0], round(time.time() - t0), "s", flush=True)
    # Blob-gap chunks: transfer deltas and lending events after the snapshot
    tt = tail_files(chain, "token_transfers", snap)
    if tt:
        c.execute(f"COPY ({XFER_SQL.format(amt=AMT.format(c='amount'), files=','.join(repr(f) for f in tt), low=snap)}) "
                  f"TO '{out}/xfer_gap.parquet' (FORMAT parquet, COMPRESSION zstd)")
    lf = tail_files(chain, "logs", snap)
    if lf:
        decode_lending(c, lf, snap)
        c.execute(f"COPY (SELECT * FROM lend_new) TO '{out}/lend_gap.parquet' (FORMAT parquet)")
    tail_end = max([int(re.search(r"-(\d+)\.parquet$", f).group(1)) for f in tt] or [snap])
    print(chain, "gap deltas/lending", round(time.time() - t0), "s", flush=True)
    # wallet classes for every address that holds or moved a served token, or has a lending position
    rows_src, ver, asof = econ_source(chain)
    c.execute(f"""CREATE OR REPLACE TABLE addrs AS
      SELECT DISTINCT holder AS address FROM '{out}/holders_snap.parquet'
      UNION SELECT DISTINCT holder FROM read_parquet('{out}/xfer_gap.parquet')
      UNION SELECT DISTINCT lower(wallet) FROM read_parquet('{rows_src}') WHERE category IN ('collateral', 'supplied', 'debt')""")
    build_classes(c, chain, "addrs", f"{out}/classes.parquet")
    print(chain, "classes", c.execute(f"SELECT class, count(*) FROM '{out}/classes.parquet' GROUP BY 1 ORDER BY 2 DESC").fetchall(),
          round(time.time() - t0), "s", flush=True)
    # lending positions (econ v3 if present, else v2) on served assets
    q = ",".join("'%s'" % b for b in BAD_Q)
    c.execute(f"""COPY (SELECT lower(wallet) AS wallet, category, protocol, lower(asset) AS asset, symbol, amount, usd, pos_key, quality
                        FROM read_parquet('{rows_src}') WHERE category IN ('collateral', 'supplied', 'debt')
                          AND coalesce(quality, '') NOT IN ({q}))
                  TO '{out}/positions_snap.parquet' (FORMAT parquet)""")
    fetch_aave_lt(c, chain, out)
    json.dump({"snapshot_block": snap, "tail_end_block": tail_end, "econ": ver, "econ_as_of": asof, "built_at": int(time.time())},
              open(f"{out}/static.json", "w"))
    print("STATIC DONE", chain, round(time.time() - t0), "s", flush=True)


def build_classes(c, chain, table, dst):
    stock_minters = ""
    sp = f"{EM}/econ/{chain}/focus_stock_tokens.csv"
    if os.path.exists(sp):
        stock_minters = f"UNION SELECT lower(main_minter_tx_sender) FROM read_csv_auto('{sp}') WHERE main_minter_tx_sender IS NOT NULL"
    c.execute(f"""CREATE OR REPLACE TEMP TABLE lab AS
      SELECT '0x' || lower(hex(l.address)) AS address, l.category, l.entity_name
      FROM read_parquet('{EM.replace('em-data', 'em-graphloop')}/export/{chain}/labels.parquet') l
      SEMI JOIN (SELECT from_hex(substr(address, 3)) AS b FROM {table}) a ON a.b = l.address""")
    c.execute(f"""CREATE OR REPLACE TEMP TABLE isc AS
      SELECT '0x' || lower(hex(x.address)) AS address, x.is_contract FROM read_parquet('{EM}/map/{chain}/addresses/*.parquet') x
      SEMI JOIN (SELECT from_hex(substr(address, 3)) AS b FROM {table}) a ON a.b = x.address""")
    c.execute(f"""COPY (
      WITH dex AS (SELECT pool AS address, any_value(coalesce(protocol, kind)) AS sub FROM dim_pools GROUP BY pool
                   UNION ALL SELECT manager, 'uniswap_v4' FROM dim_v4_managers),
      lend AS (SELECT morpho AS address, any_value(protocol) AS sub FROM dim_morpho GROUP BY morpho
               UNION ALL SELECT token, 'aave:' || pool FROM dim_aave_tokens WHERE kind = 'atoken'
               UNION ALL SELECT ctoken, protocol FROM dim_ctokens UNION ALL SELECT comet, protocol FROM dim_comets),
      issuer AS (SELECT DISTINCT token AS address FROM mk {stock_minters})
      SELECT a.address,
             CASE WHEN a.address IN {BURN} THEN 'burn'
                  WHEN dx.address IS NOT NULL THEN 'dex'
                  WHEN ld.address IS NOT NULL THEN 'lending'
                  WHEN a.address IN (SELECT market FROM dim_gmx_markets)
                       OR a.address IN (SELECT address FROM dim_registry WHERE family LIKE 'gmx%' AND role IN ('vault', 'market')) THEN 'perp'
                  WHEN a.address IN (SELECT vault FROM dim_vaults) THEN 'vault'
                  WHEN l.category = 'bridge' OR a.address IN (SELECT oft FROM dim_oft) THEN 'bridge'
                  WHEN l.category IN ('cex_hot_wallet', 'cex_deposit') THEN 'exchange'
                  WHEN a.address IN (SELECT address FROM issuer) THEN 'issuer'
                  WHEN l.category = 'mev_bot' THEN 'mev'
                  WHEN l.category = 'dex_pool' THEN 'dex'
                  WHEN l.category = 'lending' THEN 'lending'
                  WHEN coalesce(i.is_contract, false) OR l.category IN ('multisig_or_safe', 'contract_other', 'token_contract', 'dex_router',
                                                                           'nft_contract') THEN 'contract'
                  WHEN l.category = 'eoa_whale' THEN 'whale_label'
                  ELSE 'eoa' END AS class,
             coalesce(dx.sub, ld.sub, l.category) AS sub,
             coalesce(nullif(l.entity_name, ''), rg.protocol || CASE WHEN rg.role IS NOT NULL THEN ':' || rg.role ELSE '' END) AS entity_name
      FROM (SELECT DISTINCT address FROM {table}) a
      LEFT JOIN dex dx ON dx.address = a.address LEFT JOIN lend ld ON ld.address = a.address
      LEFT JOIN lab l ON l.address = a.address LEFT JOIN isc i ON i.address = a.address
      LEFT JOIN (SELECT address, any_value(protocol) AS protocol, any_value(role) AS role FROM dim_registry GROUP BY address) rg ON rg.address = a.address
    ) TO '{dst}' (FORMAT parquet)""")


def fetch_aave_lt(c, chain, out):
    """Liquidation thresholds for (Aave-family pool, collateral asset) pairs in the positions, read on-chain."""
    pairs = c.execute(f"""SELECT DISTINCT ap.pool, p.asset FROM '{out}/positions_snap.parquet' p
                          JOIN dim_aave_pools ap ON ap.protocol = p.protocol WHERE p.category IN ('collateral', 'supplied')""").fetchall()
    lt = {}
    for pool, asset in pairs:
        try:
            r = rpc_call(chain, pool, "0xc44b11f7" + asset[2:].rjust(64, "0"))
            v = int(r, 16) if r and r != "0x" else 0
            lt[f"{pool}:{asset}"] = {"ltv": (v & 0xffff) / 1e4, "lt": ((v >> 16) & 0xffff) / 1e4, "bonus": ((v >> 32) & 0xffff) / 1e4}
        except Exception as e:  # unknown fork ABI or RPC failure: leave out (no ladder entry for that pair)
            lt[f"{pool}:{asset}"] = {"error": str(e)[:120]}
    json.dump({"checked_at": int(time.time()), "source": "Pool.getConfiguration(asset) via eth_call", "pairs": lt},
              open(f"{out}/aave_lt.json", "w"))


# ---------------------------------------------------------------- periodic layer
RES_DDL = """
CREATE TABLE IF NOT EXISTS token_state(market TEXT PRIMARY KEY, json TEXT, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS liq_positions(market TEXT, wallet TEXT, protocol TEXT, position TEXT, coll_amount REAL, debt_usd REAL,
  debt_symbol TEXT, threshold REAL, threshold_kind TEXT, liq_price REAL, other_coll_usd REAL);
CREATE INDEX IF NOT EXISTS liq_market ON liq_positions(market);
CREATE TABLE IF NOT EXISTS flows(market TEXT, window TEXT, json TEXT, PRIMARY KEY(market, window));
CREATE TABLE IF NOT EXISTS events(market TEXT, time INTEGER, kind TEXT, usd REAL, amount REAL, wallet TEXT, wallet_class TEXT, tx TEXT,
  block INTEGER, log_index INTEGER, protocol TEXT, text TEXT, PRIMARY KEY(market, block, log_index, kind));
CREATE INDEX IF NOT EXISTS ev_time ON events(market, time);
CREATE TABLE IF NOT EXISTS brief(market TEXT PRIMARY KEY, json TEXT, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def res_path(chain):
    return f"{d(chain)}/analytics.sqlite3"


def res_db(chain):
    db = sqlite3.connect(res_path(chain), timeout=30, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(RES_DDL)
    return db


def rt_cache(c, chain, tail_end):
    """Per follower chunk: transfer deltas and lending events (cached per chunk file, so each run only does new chunks)."""
    cd = f"{d(chain)}/rt"
    os.makedirs(cd, exist_ok=True)
    known = int(c.execute(f"SELECT max(last_block) FROM read_parquet('{ROOT}/data/block_minutes_{chain}.parquet')").fetchone()[0])
    for f in rt_files(chain, "token_transfers", tail_end, known):
        tag = os.path.basename(f).split("_", 2)[-1]
        dst = f"{cd}/xfer_{tag}"
        if not os.path.exists(dst):
            c.execute(f"COPY ({XFER_SQL.format(amt=AMT.format(c='amount'), files=repr(f), low=tail_end)}) TO '{dst}.tmp' (FORMAT parquet)")
            os.replace(dst + ".tmp", dst)
    for f in rt_files(chain, "logs", tail_end, known):
        tag = os.path.basename(f).split("_", 1)[-1]
        dst = f"{cd}/lend_{tag}"
        if not os.path.exists(dst):
            decode_lending(c, [f], tail_end)
            c.execute(f"COPY (SELECT * FROM lend_new) TO '{dst}.tmp' (FORMAT parquet)")
            os.replace(dst + ".tmp", dst)
    return cd


def short(a):
    return a[:6] + "…" + a[-4:] if a else "?"


def fmt_usd(v):
    if v is None:
        return "an unpriced amount"
    a = abs(v)
    s = "$%.1fB" % (a / 1e9) if a >= 1e9 else "$%.1fM" % (a / 1e6) if a >= 1e6 else "$%.0fK" % (a / 1e3) if a >= 1e3 else "$%.0f" % a
    return ("-" if v < 0 else "") + s


def fmt_amt(v):
    a = abs(v or 0)
    return ("%.2fB" % (a / 1e9) if a >= 1e9 else "%.2fM" % (a / 1e6) if a >= 1e6 else "%.1fK" % (a / 1e3) if a >= 1e4 else
            "%.2f" % a if a >= 1 else "%.4g" % a)


def proto_name(p):
    if not p:
        return "?"
    base = p.split(":")[0]
    n = {"morpho_blue": "Morpho Blue", "morpho_blue_fork": "Morpho fork", "aave_v3": "Aave v3", "aave_v3_fork": "Aave v3 fork",
         "aave_v2_fork": "Aave v2 fork", "aave_v2": "Aave v2"}.get(base, base)
    return n + (" " + p.split(":")[1][:8] if ":" in p and base.endswith("fork") else "")


def refresh(chain, reader=None, log=print):
    t0 = time.time()
    st = json.load(open(f"{d(chain)}/static.json"))
    snap, tail_end = st["snapshot_block"], st["tail_end_block"]
    if reader is None:
        from emread import Reader
        reader = Reader(chain)
    # 3 GB made every cycle spill 10-15 GB to disk (2026-10-20 measurement); memory is the cheaper side on skew now
    c = connect(threads=2, mem=os.environ.get("EM_ANALYTICS_MEM", "6GB"))
    try:
        load_dims(c, chain)
        served(c, chain)
        out = d(chain)
        cd = rt_cache(c, chain, tail_end)
        now = reader.data_until_ms() // 1000
        # prices (latest known, USD) and market ids
        px = [(m["token"], m["last_price"], m["id"], m["symbol"], m["kind"], m.get("quality") or "ok") for m in reader.markets()]
        c.execute("CREATE OR REPLACE TABLE px(token VARCHAR, price DOUBLE, market VARCHAR, symbol VARCHAR, kind VARCHAR, quality VARCHAR)")
        c.executemany("INSERT INTO px VALUES (?, ?, ?, ?, ?, ?)", px)
        from common import ANCHORS
        have = {r[0] for r in px}
        c.executemany("INSERT INTO px VALUES (?, 1.0, ?, ?, 'usd_anchor', 'ok')",
                      [(a, f"{chain}:{a}", sym) for a, k, _, sym in ANCHORS[chain] if k == "usd" and a not in have])
        c.execute(f"""CREATE OR REPLACE TABLE bm AS SELECT minute::BIGINT AS minute, first_block::UBIGINT AS first_block
                      FROM read_parquet('{ROOT}/data/block_minutes_{chain}.parquet') WHERE minute > 0 ORDER BY first_block""")
        c.execute(f"CREATE OR REPLACE TABLE cls AS SELECT * FROM '{out}/classes.parquet'")
        xr = glob.glob(f"{cd}/xfer_*.parquet")
        lr = glob.glob(f"{cd}/lend_*.parquet")
        xsrc = f"SELECT * FROM read_parquet('{out}/xfer_gap.parquet')" + (
            " UNION ALL SELECT * FROM read_parquet([%s])" % ",".join("'%s'" % f for f in xr) if xr else "")
        lsrc = f"SELECT * FROM read_parquet('{out}/lend_gap.parquet')" + (
            " UNION ALL SELECT * FROM read_parquet([%s])" % ",".join("'%s'" % f for f in lr) if lr else "")
        c.execute(f"CREATE OR REPLACE TABLE lend AS SELECT l.*, b.minute AS time FROM ({lsrc}) l ASOF JOIN bm b ON l.bn >= b.first_block")
        c.execute("""UPDATE lend SET amount_raw = amount_raw / 10 ^ coalesce((SELECT decimals FROM mk WHERE mk.token = lend.asset), 18),
                     repaid_raw = repaid_raw / 10 ^ coalesce((SELECT decimals FROM mk WHERE mk.token = lend.loan_token), 18)""")
        log(f"{chain} analytics inputs {round(time.time() - t0)}s")
        token_state(c, chain, out, xsrc, now, snap, [f"{out}/xfer_gap.parquet"] + xr, log)
        log(f"{chain} token_state {round(time.time() - t0)}s")
        positions(c, chain, out, snap)
        flows_events(c, chain, out, reader, now, snap)
        log(f"{chain} flows/events {round(time.time() - t0)}s")
        write_results(c, chain, now, st)
        try:   # money-flow map (wallet map): same tables, so its DEX and lending totals reconcile with the flows above
            import flowmap
            res = res_db(chain)
            try:
                flowmap.build(c, chain, now, out, res, log=log, rt_files=rt_files, tail_files=tail_files, amt=AMT, snap=st["snapshot_block"])
            finally:
                res.close()
        except Exception:
            import traceback
            log(f"{chain} flowmap failed:\n" + traceback.format_exc())
        log(f"{chain} analytics refresh done {round(time.time() - t0)}s")
    finally:
        c.close()


def bal_inputs(c, out, xsrc, snap, xfiles, bm_lo):
    """The balance sum's inputs, incremental (2026-10-20): a checkpoint of every holder's summed balance plus the transfer files
    that arrived after it. The per-chunk files never change once written (rt_cache), so the checkpoint stays valid while the
    snapshot and every file it summed are unchanged; anything else (a static rebuild, a rewritten file) sums everything
    again, as before. Returns (sql, mode)."""
    ck_p, ck_j = f"{out}/bal_ckpt.parquet", f"{out}/bal_ckpt.json"
    sig = {}
    for f in xfiles:
        try:
            st = os.stat(f)
            sig[f] = [st.st_size, st.st_mtime_ns]
        except OSError:
            pass
    try:
        hs = os.stat(f"{out}/holders_snap.parquet")
        hsig = [hs.st_size, hs.st_mtime_ns]
    except OSError:
        hsig = None
    sig["__holders_snap__"] = hsig   # a rebuilt snapshot (static layer) invalidates the checkpoint too
    try:
        ck = json.load(open(ck_j))
    except (OSError, ValueError):
        ck = None
    if ck and ck.get("snap") == snap and ck.get("bm_lo") == bm_lo and os.path.exists(ck_p) and \
            all(sig.get(f) == v for f, v in ck.get("files", {}).items()):
        new = [f for f in sig if f not in ck["files"] and f != "__holders_snap__"]
        sql = f"SELECT token, holder, amount FROM read_parquet('{ck_p}')" + (
            " UNION ALL SELECT token, holder, delta FROM read_parquet([%s]) WHERE bn >= %d" % (",".join("'%s'" % f for f in new), bm_lo)
            if new else "")
        return sql, "incremental (+%d files)" % len(new), sig
    sql = (f"SELECT token, holder, amount FROM '{out}/holders_snap.parquet' "
           f"UNION ALL SELECT token, holder, delta FROM ({xsrc}) x WHERE x.bn >= {bm_lo}")
    return sql, "full", sig


def token_state(c, chain, out, xsrc, now, snap, xfiles=None, log=print):
    # balances now = map snapshot + transfers after it; per-holder class; window deltas (24h / 7d) per bucket
    # The window tables (24h, 7d, flows 1h-7d) only read the last 7 days of transfers; the full history only feeds the
    # balances, which are now summed incrementally from a checkpoint (the spill of the full re-sum was ~11 GB a cycle).
    bm_lo = c.execute("SELECT coalesce(min(first_block), 0) FROM bm").fetchone()[0]   # the old ASOF join kept bn >= this only
    w_lo = c.execute(f"SELECT coalesce(max(first_block), 0) FROM bm WHERE minute <= {now - 7 * 86400 - 3600}").fetchone()[0]
    if xfiles is not None:
        from analytics_window import build_window
        build_window(c, xfiles, max(int(w_lo), int(bm_lo)), log=log)
    else:  # callers with an SQL-only source keep the original compatibility path
        c.execute(f"""CREATE OR REPLACE TABLE xf AS SELECT x.token, x.holder, x.delta, b.minute
                      FROM (SELECT * FROM ({xsrc}) WHERE bn >= {max(int(w_lo), int(bm_lo))}) x
                      ASOF JOIN bm b ON x.bn >= b.first_block""")
    if xfiles is None:
        c.execute(f"""CREATE OR REPLACE TABLE balr AS SELECT token, holder, sum(amount) AS amount FROM (
                        SELECT token, holder, amount FROM '{out}/holders_snap.parquet'
                        UNION ALL SELECT token, holder, delta FROM ({xsrc}) x WHERE x.bn >= {bm_lo}) GROUP BY ALL""")
        mode, sig = "full", None
    else:
        sql, mode, sig = bal_inputs(c, out, xsrc, snap, xfiles, int(bm_lo))
        if mode == "incremental (+0 files)":
            # The validated checkpoint already contains one filtered sum per holder.
            c.execute(f"CREATE OR REPLACE VIEW balr AS {sql}")
        else:
            c.execute(f"CREATE OR REPLACE TABLE balr AS SELECT token, holder, sum(amount) AS amount FROM ({sql}) GROUP BY ALL "
                      "HAVING abs(sum(amount)) > 1e-12")
    if sig is not None and mode != "incremental (+0 files)":   # negatives retained for exact later netting
        ck_p = f"{out}/bal_ckpt.parquet"
        c.execute(f"COPY balr TO '{ck_p}.tmp' (FORMAT parquet, COMPRESSION zstd)")
        os.replace(ck_p + ".tmp", ck_p)
        with open(f"{out}/bal_ckpt.json.tmp", "w") as f:
            json.dump({"snap": snap, "bm_lo": int(bm_lo), "files": sig, "at": int(time.time())}, f)
        os.replace(f"{out}/bal_ckpt.json.tmp", f"{out}/bal_ckpt.json")
    log(f"{chain} balances {mode}")
    vf = f"{out}/bal_verify"   # one-shot check (touch the file): the incremental sum against a full re-sum, logged
    if mode.startswith("incremental") and os.path.exists(vf):
        os.remove(vf)
        c.execute(f"""CREATE OR REPLACE TEMP TABLE balf AS SELECT token, holder, sum(amount) AS amount FROM (
                        SELECT token, holder, amount FROM '{out}/holders_snap.parquet'
                        UNION ALL SELECT token, holder, delta FROM ({xsrc}) x WHERE x.bn >= {bm_lo}) GROUP BY ALL HAVING sum(amount) > 1e-12""")
        r = c.execute("""SELECT count(*) FILTER (WHERE f.amount IS NULL OR i.amount IS NULL OR abs(f.amount - i.amount) > 1e-6 * greatest(1, abs(f.amount))),
                                count(f.amount), count(i.amount),
                                count(*) FILTER (WHERE abs(coalesce(f.amount, 0) - coalesce(i.amount, 0)) > 1e-3),
                                count(*) FILTER (WHERE abs(coalesce(f.amount, 0) - coalesce(i.amount, 0)) > 1e-6 * greatest(abs(coalesce(f.amount, 0)), abs(coalesce(i.amount, 0)))
                                                 AND greatest(abs(coalesce(f.amount, 0)), abs(coalesce(i.amount, 0))) > 1e-3),
                                max(abs(coalesce(f.amount, 0) - coalesce(i.amount, 0))),
                                arg_max(token || ' ' || holder || ' f=' || coalesce(f.amount, 0)::VARCHAR
                                        || ' i=' || coalesce(i.amount, 0)::VARCHAR, abs(coalesce(f.amount, 0) - coalesce(i.amount, 0)))
                         FROM balf f FULL JOIN (SELECT * FROM balr WHERE amount > 1e-12) i USING (token, holder)""").fetchone()
        log(f"{chain} balances verify: full {r[1]} rows, incremental {r[2]} rows, {r[0]} differ (relative 1e-6); "
            f"{r[3]} differ by > 0.001 token, {r[4]} by > 1e-6 relative on balances > 0.001; max diff {r[5]} at {r[6]}")
        c.execute("DROP TABLE balf")
    c.execute("CREATE OR REPLACE TABLE bal AS SELECT token, holder, amount FROM balr WHERE amount > 1e-12")
    c.execute("""CREATE OR REPLACE TABLE balc AS
      SELECT b.token, b.holder, b.amount, coalesce(k.class, 'unknown') AS class, k.sub, k.entity_name, p.price,
             b.amount * p.price AS usd FROM bal b LEFT JOIN cls k ON k.address = b.holder LEFT JOIN px p ON p.token = b.token""")
    c.execute("""CREATE OR REPLACE TABLE supply AS SELECT token, sum(amount) FILTER (WHERE class <> 'burn') AS supply,
                 sum(amount) FILTER (WHERE class = 'burn') AS burned, count(*) FILTER (WHERE class <> 'burn') AS holders FROM balc GROUP BY token""")
    # whales: EOAs holding >= $100K and >= 0.1% of supply; labelled whales from 0.01%; unpriced tokens: >= 1% of supply
    c.execute("""CREATE OR REPLACE TABLE balb AS
      SELECT b.*, CASE
        WHEN b.class IN ('eoa', 'whale_label', 'unknown') OR (b.class = 'contract' AND b.sub = 'contract_other') THEN CASE WHEN (
             (b.usd >= 100000 AND b.amount >= 0.001 * s.supply) OR (b.class = 'whale_label' AND b.amount >= 0.0001 * s.supply)
             OR (b.price IS NULL AND b.amount >= 0.01 * s.supply)) THEN 'whales'
             WHEN b.class = 'contract' THEN 'smart_wallets' ELSE 'retail' END
        WHEN b.class = 'dex' THEN 'dex_liquidity'
        WHEN b.class = 'lending' THEN 'lending_contracts'
        WHEN b.class = 'exchange' THEN 'exchanges'
        WHEN b.class = 'perp' THEN 'perp_liquidity'
        WHEN b.class = 'bridge' THEN 'bridges'
        WHEN b.class IN ('vault', 'contract', 'mev') THEN 'vaults_contracts'
        WHEN b.class = 'issuer' THEN 'issuer'
        WHEN b.class = 'burn' THEN 'burned'
        ELSE 'unknown' END AS bucket
      FROM balc b JOIN supply s USING (token)""")
    # bucket changes over windows from transfer deltas (holder class as of now)
    c.execute(f"""CREATE OR REPLACE TABLE bdelta AS
      SELECT x.token, b.bucket,
             sum(x.delta) FILTER (WHERE x.minute >= {now - 86400}) AS d24h, sum(x.delta) FILTER (WHERE x.minute >= {now - 7 * 86400}) AS d7d
      FROM xf x JOIN balb b ON b.token = x.token AND b.holder = x.holder
      WHERE x.minute >= {now - 7 * 86400} GROUP BY ALL""")
    c.execute("""CREATE OR REPLACE TABLE dex_sub AS SELECT token,
                   CASE WHEN sub ILIKE '%v4%' THEN 'v4' WHEN sub ILIKE '%v3%' OR sub ILIKE '%algebra%' OR sub ILIKE '%clmm%' THEN 'v3'
                        WHEN sub ILIKE '%v2%' OR sub ILIKE '%solidly%' THEN 'v2' ELSE 'other' END AS family,
                   sum(amount) AS amount, sum(usd) AS usd, count(*) AS holders FROM balb WHERE bucket = 'dex_liquidity' GROUP BY ALL""")
    c.execute("""CREATE OR REPLACE TABLE lend_phys AS
      SELECT b.token, coalesce(ap.protocol, CASE WHEN b.sub LIKE 'aave:%' THEN 'aave' ELSE b.sub END) AS protocol, sum(b.amount) AS amount
      FROM balb b LEFT JOIN dim_aave_pools ap ON b.sub = 'aave:' || ap.pool WHERE b.bucket = 'lending_contracts' GROUP BY ALL""")
    c.execute("""CREATE OR REPLACE TABLE top_holders AS SELECT * FROM (
                   SELECT token, holder, amount, usd, bucket, class, entity_name,
                          row_number() OVER (PARTITION BY token ORDER BY amount DESC) AS rk FROM balb WHERE bucket <> 'burned') WHERE rk <= 10""")


def positions(c, chain, out, snap):
    """Lending positions now = econ positions (as of the econ snapshot) + lending events after it."""
    rows_src, ver, asof = econ_source(chain)
    base_block = int(asof.get("as_of_block") or snap)
    q = ",".join("'%s'" % b for b in BAD_Q)
    c.execute(f"""CREATE OR REPLACE TABLE flagged AS SELECT DISTINCT lower(wallet) AS wallet, protocol, pos_key FROM read_parquet('{rows_src}')
                  WHERE quality IN ({q})""")
    c.execute(f"CREATE OR REPLACE TABLE pos0 AS SELECT * FROM '{out}/positions_snap.parquet'")
    # Morpho: per (wallet, market) collateral and debt
    c.execute(f"""CREATE OR REPLACE TABLE mpos AS
      WITH e AS (SELECT wallet, market, mm.protocol, mm.collateral_token, mm.loan_token, mm.lltv,
                        sum(CASE kind WHEN 'supply_collateral' THEN amount_raw WHEN 'withdraw_collateral' THEN -amount_raw
                                      WHEN 'liquidate' THEN -amount_raw ELSE 0 END) AS dcoll,
                        sum(CASE kind WHEN 'borrow' THEN amount_raw WHEN 'repay' THEN -amount_raw WHEN 'liquidate' THEN -coalesce(repaid_raw, 0)
                                      ELSE 0 END) AS ddebt
                 FROM lend l JOIN dim_morpho mm ON mm.id = l.market WHERE l.bn > {base_block} GROUP BY ALL),
      s AS (SELECT p.wallet, p.pos_key AS market, mm.protocol, mm.collateral_token, mm.loan_token, mm.lltv,
                   sum(p.amount) FILTER (WHERE p.category = 'collateral' AND p.asset = mm.collateral_token) AS coll,
                   sum(p.amount) FILTER (WHERE p.category = 'debt' AND p.asset = mm.loan_token) AS debt
            FROM pos0 p JOIN dim_morpho mm ON mm.id = p.pos_key WHERE p.protocol LIKE 'morpho%' GROUP BY ALL)
      SELECT coalesce(s.wallet, e.wallet) AS wallet, coalesce(s.market, e.market) AS market, coalesce(s.protocol, e.protocol) AS protocol,
             coalesce(s.collateral_token, e.collateral_token) AS collateral_token, coalesce(s.loan_token, e.loan_token) AS loan_token,
             coalesce(s.lltv, e.lltv) AS lltv, greatest(coalesce(s.coll, 0) + coalesce(e.dcoll, 0), 0) AS coll,
             greatest(coalesce(s.debt, 0) + coalesce(e.ddebt, 0), 0) AS debt
      FROM s FULL JOIN e ON e.wallet = s.wallet AND e.market = s.market""")
    c.execute("DELETE FROM mpos USING flagged f WHERE f.wallet = mpos.wallet AND f.pos_key = mpos.market")
    # Aave-family: per (wallet, pool) collateral by asset and debt by asset
    c.execute(f"""CREATE OR REPLACE TABLE apos AS
      WITH s AS (SELECT p.wallet, ap.pool, p.protocol, p.asset, CASE WHEN p.category = 'debt' THEN 'debt' ELSE 'coll' END AS side, sum(p.amount) AS amt
                 FROM pos0 p JOIN dim_aave_pools ap ON ap.protocol = p.protocol GROUP BY ALL),
      e AS (SELECT l.wallet, l.market AS pool, l.protocol, l.asset,
                   CASE WHEN l.kind IN ('borrow', 'repay') THEN 'debt' ELSE 'coll' END AS side,
                   sum(CASE l.kind WHEN 'supply' THEN l.amount_raw WHEN 'withdraw' THEN -l.amount_raw WHEN 'borrow' THEN l.amount_raw
                                   WHEN 'repay' THEN -l.amount_raw WHEN 'liquidate' THEN -l.amount_raw END) AS amt
            FROM lend l WHERE l.bn > {base_block} AND l.market IN (SELECT pool FROM dim_aave_pools) GROUP BY ALL
            UNION ALL
            SELECT wallet, market, protocol, loan_token, 'debt', -sum(coalesce(repaid_raw, 0)) FROM lend
            WHERE bn > {base_block} AND kind = 'liquidate' AND market IN (SELECT pool FROM dim_aave_pools) GROUP BY ALL)
      SELECT wallet, pool, any_value(protocol) AS protocol, asset, side, greatest(sum(amt), 0) AS amt
      FROM (SELECT * FROM s UNION ALL SELECT * FROM e) GROUP BY wallet, pool, asset, side""")
    c.execute("DELETE FROM apos USING flagged f WHERE f.wallet = apos.wallet AND f.protocol = apos.protocol")
    lt = json.load(open(f"{out}/aave_lt.json")).get("pairs", {})
    c.execute("CREATE OR REPLACE TABLE alt(pool VARCHAR, asset VARCHAR, lt DOUBLE)")
    alt_rows = [(k.split(":")[0], k.split(":")[1], v["lt"]) for k, v in lt.items() if v.get("lt")]
    if alt_rows:   # a chain without Aave liquidation thresholds (Arc) has none to insert
        c.executemany("INSERT INTO alt VALUES (?, ?, ?)", alt_rows)
    # supplied (not collateral) positions now: econ 'supplied' rows (Morpho loan-side, other lenders) adjusted by Aave events is not
    # separable for Aave (a supply is collateral when enabled), so Aave supplies are reported as collateral, like econ v2 does.
    c.execute("""CREATE OR REPLACE TABLE coll_now AS
      SELECT collateral_token AS token, protocol, sum(coll) AS amount, count(DISTINCT wallet) FILTER (WHERE coll > 0) AS wallets FROM mpos GROUP BY ALL
      UNION ALL SELECT asset, protocol, sum(amt), count(DISTINCT wallet) FILTER (WHERE amt > 0) FROM apos WHERE side = 'coll' GROUP BY ALL
      UNION ALL SELECT asset, protocol, sum(amount), count(DISTINCT wallet) FROM pos0
                WHERE category = 'collateral' AND protocol NOT LIKE 'morpho%' AND protocol NOT IN (SELECT protocol FROM dim_aave_pools) GROUP BY ALL""")
    c.execute(f"""CREATE OR REPLACE TABLE debt_now AS
      SELECT loan_token AS token, protocol, sum(debt) AS amount, count(DISTINCT wallet) FILTER (WHERE debt > 0) AS wallets FROM mpos GROUP BY ALL
      UNION ALL SELECT asset, protocol, sum(amt), count(DISTINCT wallet) FILTER (WHERE amt > 0) FROM apos WHERE side = 'debt' GROUP BY ALL
      UNION ALL SELECT asset, protocol, sum(amount), count(DISTINCT wallet) FROM pos0
                WHERE category = 'debt' AND protocol NOT LIKE 'morpho%' AND protocol NOT IN (SELECT protocol FROM dim_aave_pools) GROUP BY ALL""")
    c.execute("""CREATE OR REPLACE TABLE supplied_now AS
      SELECT asset AS token, protocol, sum(amount) AS amount, count(DISTINCT wallet) AS wallets FROM pos0
      WHERE category = 'supplied' AND protocol NOT IN (SELECT protocol FROM dim_aave_pools) GROUP BY ALL""")


WIN = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}


def wclass_sql(alias_wallet):
    return f"""CASE WHEN wc.class = 'mev' THEN 'mev' WHEN wc.class = 'exchange' THEN 'exchange'
                    WHEN wc.class IN ('whale_label') OR wh.holder IS NOT NULL THEN 'whale'
                    WHEN wc.class IN ('eoa', 'unknown') OR wc.class IS NULL THEN 'retail' ELSE 'contract' END"""


def flows_events(c, chain, out, reader, now, snap):
    deep = reader.deep_path
    hot = reader.hot_path
    c.execute(f"ATTACH '{deep}' AS dp (READ_ONLY)")
    c.execute("CREATE OR REPLACE TABLE whales AS SELECT token, holder FROM balb WHERE bucket = 'whales'")
    since = now - 7 * 86400
    # DEX trades (deep tape + hot fills after the deep cut)
    cut = int(reader.deep_until_ms() // 1000) + 60
    c.execute(f"""CREATE OR REPLACE TABLE tr AS SELECT token, time, side, usd, base_volume AS amount, '0x' || lower(hex(trader)) AS wallet,
                         '0x' || lower(hex(tx_hash)) AS tx, bn, log_index, pool FROM dp.tape WHERE time >= {since} AND time < {cut}""")
    if os.path.exists(hot):
        c.execute("INSTALL sqlite; LOAD sqlite")
        c.execute(f"ATTACH '{hot}' AS ht (TYPE sqlite, READ_ONLY)")
        c.execute(f"""INSERT INTO tr SELECT m.token, f.time, m.side, m.usd, f.base_volume, m.trader, m.tx, f.slot, f.event_order, f.pool
                      FROM ht.fill_meta m JOIN ht.fills f ON f.identity = m.identity WHERE f.time >= {max(since, cut)}""")
        c.execute("DETACH ht")
    c.execute(f"""CREATE OR REPLACE TABLE trc AS SELECT tr.*, {wclass_sql('tr.wallet')} AS wclass
                  FROM tr LEFT JOIN cls wc ON wc.address = tr.wallet LEFT JOIN whales wh ON wh.token = tr.token AND wh.holder = tr.wallet""")
    # lending events with USD at the hour (deep hourly closes; current price after the deep cut)
    c.execute(f"""CREATE OR REPLACE TABLE lev AS
      WITH l AS (SELECT * FROM lend WHERE time >= {since - 30 * 86400}),
      h AS (SELECT token, time // 1000 AS t, close FROM dp.token_bars WHERE period = 3600 AND time >= {(since - 31 * 86400) * 1000})
      SELECT l.*, coalesce(h.close, p.price) AS asset_px FROM l ASOF LEFT JOIN h ON h.token = l.asset AND l.time >= h.t
      LEFT JOIN px p ON p.token = l.asset""")
    c.execute("""CREATE OR REPLACE TABLE levc AS
      SELECT l.*, l.amount_raw * l.asset_px AS usd, coalesce(lp.price, 1.0) AS loan_px, l.repaid_raw * coalesce(lp.price, 1.0) AS repaid_usd,
             CASE WHEN wc.class = 'mev' THEN 'mev' WHEN wc.class = 'exchange' THEN 'exchange' WHEN wc.class = 'whale_label' THEN 'whale'
                  WHEN wc.class IN ('eoa', 'unknown') OR wc.class IS NULL THEN 'retail' ELSE 'contract' END AS wclass
      FROM lev l LEFT JOIN cls wc ON wc.address = l.wallet LEFT JOIN px lp ON lp.token = l.loan_token""")
    # live.db (bridges, LP) for its covered window
    live_from = None
    if os.path.exists(LIVE_DB):
        c.execute(f"ATTACH '{LIVE_DB}' AS lv (TYPE sqlite, READ_ONLY)")
        c.execute(f"""CREATE OR REPLACE TABLE lva AS SELECT time, action, protocol, wallet, wallet_category, lower(asset) AS asset,
                             lower(asset2) AS asset2, amount, amount2, usd, tx_hash, block, log_index
                      FROM lv.actions WHERE chain = '{chain}' AND action IN ('bridge_in', 'bridge_out', 'lp_add', 'lp_remove')""")
        live_from = c.execute(f"SELECT min(time) FROM lv.actions WHERE chain = '{chain}'").fetchone()[0]
        c.execute("DETACH lv")
    else:
        c.execute("CREATE OR REPLACE TABLE lva(time BIGINT, action VARCHAR, protocol VARCHAR, wallet VARCHAR, wallet_category VARCHAR, asset VARCHAR, "
                  "asset2 VARCHAR, amount DOUBLE, amount2 DOUBLE, usd DOUBLE, tx_hash VARCHAR, block BIGINT, log_index BIGINT)")
    c.execute("DETACH dp")
    snap_time = c.execute(f"SELECT max(minute) FROM bm WHERE first_block <= {snap}").fetchone()[0]
    c.execute("CREATE OR REPLACE TABLE meta_t(k VARCHAR, v BIGINT)")
    c.executemany("INSERT INTO meta_t VALUES (?, ?)", [("now", now), ("snap_time", snap_time), ("live_from", live_from or 0), ("cut", cut)])


def ladder_rows(c, chain):
    """Per position liquidation price of each collateral token (Morpho LLTV per market; Aave LT read on-chain)."""
    rows = c.execute(f"""
      SELECT '{chain}:' || m.collateral_token, m.wallet, m.protocol, m.market, m.coll, m.debt * coalesce(lp.price, 1.0), coalesce(lp.symbol, '?'),
             m.lltv, 'lltv', m.debt * coalesce(lp.price, 1.0) / (m.coll * m.lltv), 0.0
      FROM mpos m LEFT JOIN px lp ON lp.token = m.loan_token
      LEFT JOIN px cp ON cp.token = m.collateral_token
      WHERE m.coll > 0 AND m.debt > 0 AND m.lltv > 0 AND lp.price IS NOT NULL AND m.debt * lp.price >= 1
        AND (cp.price IS NULL OR (m.coll * cp.price >= 10 AND m.debt * lp.price / (m.coll * m.lltv) <= 50 * cp.price))""").fetchall()
    rows += c.execute(f"""
      WITH a AS (SELECT a.*, p.price, t.lt FROM apos a LEFT JOIN px p ON p.token = a.asset LEFT JOIN alt t ON t.pool = a.pool AND t.asset = a.asset),
      w AS (SELECT wallet, pool, any_value(protocol) AS protocol, sum(amt * price) FILTER (WHERE side = 'debt') AS debt_usd,
                   sum(amt * price * lt) FILTER (WHERE side = 'coll') AS wcoll_usd, count(*) FILTER (WHERE side = 'debt' AND price IS NULL) AS unpriced_debt,
                   count(*) FILTER (WHERE side = 'coll' AND amt > 0 AND (price IS NULL OR lt IS NULL)) AS unpriced_coll,
                   string_agg(DISTINCT CASE WHEN side = 'debt' THEN asset END, ',') AS debt_assets
            FROM a GROUP BY wallet, pool)
      SELECT '{chain}:' || a.asset, a.wallet, w.protocol, a.pool, a.amt, w.debt_usd, coalesce(dp.symbol, 'multi'), a.lt, 'aave_lt',
             (w.debt_usd - (w.wcoll_usd - a.amt * a.price * a.lt)) / (a.amt * a.lt), w.wcoll_usd - a.amt * a.price * a.lt
      FROM a JOIN w ON w.wallet = a.wallet AND w.pool = a.pool LEFT JOIN px dp ON dp.token = w.debt_assets
      WHERE a.side = 'coll' AND a.amt > 0 AND a.lt > 0 AND a.price IS NOT NULL AND w.debt_usd > 0 AND w.unpriced_debt = 0
        AND w.unpriced_coll = 0 AND w.debt_assets <> a.asset
        AND (w.debt_usd - (w.wcoll_usd - a.amt * a.price * a.lt)) > 0 AND a.amt * a.price >= 10 AND w.debt_usd >= 1
        AND (w.debt_usd - (w.wcoll_usd - a.amt * a.price * a.lt)) / (a.amt * a.lt) <= 50 * a.price""").fetchall()
    return rows


def group(rows, key=0):
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


BUCKET_LABEL = {"retail": "Retail wallets (EOAs)", "whales": "Whales", "smart_wallets": "Smart-contract wallets (no protocol role)", "lending_collateral": "Posted as collateral",
                "lending_supplied": "Supplied to lending (not collateral)", "lending_unattributed": "In lending contracts (unattributed)",
                "dex_liquidity": "DEX liquidity", "perp_liquidity": "Perp DEX liquidity (GMX)", "exchanges": "Exchanges", "bridges": "Bridges", "vaults_contracts": "Vaults & contracts",
                "issuer": "Issuer / treasury", "unknown": "Unknown"}
EVENT_KIND = {"large_buy": "whale_buy", "large_sell": "whale_sell", "collateral_post": "collateral_in", "collateral_withdraw": "collateral_out",
              "borrow_against": "borrow", "repay": "repay", "liquidation": "liquidation", "bridge_in": "bridge_in", "bridge_out": "bridge_out",
              "lp_add": "lp_add", "lp_remove": "lp_remove"}


def num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return v


def write_results(c, chain, now, st):
    meta = dict(c.execute("SELECT k, v FROM meta_t").fetchall())
    snap_time, live_from = meta["snap_time"], meta["live_from"] or None
    updated = int(time.time())
    prices = {t: (num(p), m, s, k, q) for t, p, m, s, k, q in c.execute("SELECT token, price, market, symbol, kind, quality FROM px").fetchall()}
    sup = {t: (num(s), num(b), h) for t, s, b, h in c.execute("SELECT token, supply, burned, holders FROM supply").fetchall()}
    bk = group(c.execute("SELECT token, bucket, sum(amount), sum(usd), count(*) FROM balb GROUP BY 1, 2").fetchall())
    dsub = group(c.execute("SELECT token, family, amount, usd, holders FROM dex_sub").fetchall())
    coll = group(c.execute("SELECT token, protocol, amount, wallets FROM coll_now WHERE amount > 0").fetchall())
    supp = group(c.execute("SELECT token, protocol, amount, wallets FROM supplied_now WHERE amount > 0").fetchall())
    debt = group(c.execute("SELECT token, protocol, amount, wallets FROM debt_now WHERE amount > 0").fetchall())
    lphys = group(c.execute("SELECT token, protocol, amount FROM lend_phys").fetchall())
    bdel = group(c.execute("SELECT token, bucket, d24h, d7d FROM bdelta").fetchall())
    ldel = group(c.execute(f"""SELECT asset, protocol,
        sum(CASE WHEN kind IN ('supply_collateral', 'supply') THEN amount_raw WHEN kind IN ('withdraw_collateral', 'withdraw', 'liquidate') THEN -amount_raw END)
          FILTER (WHERE time >= {now - 86400}) AS d24,
        sum(CASE WHEN kind IN ('supply_collateral', 'supply') THEN amount_raw WHEN kind IN ('withdraw_collateral', 'withdraw', 'liquidate') THEN -amount_raw END)
          FILTER (WHERE time >= {now - 7 * 86400}) AS d7
        FROM levc WHERE kind IN ('supply_collateral', 'withdraw_collateral', 'supply', 'withdraw', 'liquidate') GROUP BY 1, 2""").fetchall())
    tops = group(c.execute("SELECT token, holder, amount, usd, bucket, class, entity_name FROM top_holders ORDER BY token, rk").fetchall())
    full7 = snap_time is not None and snap_time <= now - 7 * 86400
    base_notes = [f"Balances: map snapshot + every ERC20 transfer since (block-exact); positions: econ {st['econ']} + Morpho Blue/Aave v3 "
                  f"events after block {st['snapshot_block']}",
                  "supply = sum of positive balances (mints included), burn addresses excluded; no independent totalSupply read",
                  "whales: EOAs (and role-less contract wallets) with >= $100K and >= 0.1% of supply (labelled whales from 0.01%); "
                  "retail: other EOAs; smart_wallets: contracts with no protocol role (mostly smart-contract user accounts)",
                  "buckets are PHYSICAL holdings and sum to supply. Tokens inside lending contracts are split into collateral vs supply: "
                  "Morpho holds collateral first; other lenders by the ratio of position claims. lendingClaims lists the economic claims "
                  "(collateral posted, supplied, borrowed); claims can exceed the physical amount because borrowed tokens are re-lent",
                  "Aave supplies count as collateral claims (Aave does not separate them); interest accrual is not tracked",
                  "d24h/d7d: net transfers into each bucket's current holders; lending buckets from lending events"
                  + ("" if full7 else f"; history after the econ snapshot only (from unix {snap_time}), so d7d may be partial")]
    states = {}
    for token, (price, market, symbol, kind, quality) in prices.items():
        sp = sup.get(token)
        if not sp or not sp[0]:
            continue
        supply, burned, holders = sp
        b = {r[1]: (num(r[2]), num(r[3]), r[4]) for r in bk.get(token, [])}
        lend_phys = b.pop("lending_contracts", (0.0, 0.0, 0))[0] or 0.0
        b.pop("burned", None)
        dl = {r[1]: (num(r[2]) or 0.0, num(r[3]) or 0.0) for r in ldel.get(token, [])}
        dd = {r[1]: (num(r[2]) or 0.0, num(r[3]) or 0.0) for r in bdel.get(token, [])}
        cpos = {r[1]: (num(r[2]), r[3]) for r in coll.get(token, [])}
        spos = {r[1]: (num(r[2]), r[3]) for r in supp.get(token, [])}
        dpos = {r[1]: (num(r[2]), r[3]) for r in debt.get(token, [])}
        phys = {r[1]: num(r[2]) for r in lphys.get(token, [])}
        # physical tokens in each lending protocol, split into collateral vs lendable supply: Morpho keeps collateral
        # in the contract (never lent), so collateral comes first; elsewhere split by the ratio of position claims
        cprot, sprot, unattr = [], [], 0.0
        for proto, ph in phys.items():
            cp_, sp_ = (cpos.get(proto) or (0.0, 0))[0] or 0.0, (spos.get(proto) or (0.0, 0))[0] or 0.0
            if proto.startswith("morpho"):
                ca = min(ph, cp_)
                sa = ph - ca
            elif cp_ + sp_ > 0:
                ca, sa = ph * cp_ / (cp_ + sp_), ph * sp_ / (cp_ + sp_)
            else:
                ca, sa = 0.0, 0.0
                unattr += ph
            if ca > 0:
                cprot.append((proto, ca, (cpos.get(proto) or (0, None))[1]))
            if sa > 0:
                sprot.append((proto, sa, (spos.get(proto) or (0, None))[1]))
        csum, ssum = sum(x[1] for x in cprot), sum(x[1] for x in sprot)
        resid = unattr + max(0.0, lend_phys - sum(phys.values()))
        claims = []
        for proto in sorted(set(cpos) | set(spos) | set(dpos), key=lambda p: -((cpos.get(p) or (0, 0))[0] or 0)):
            claims.append({"name": proto_name(proto), "id": proto, "collateral": (cpos.get(proto) or (None,))[0],
                           "supplied": (spos.get(proto) or (None,))[0], "borrowed": (dpos.get(proto) or (None,))[0],
                           "collateralWallets": (cpos.get(proto) or (0, None))[1], "physical": phys.get(proto)})
        usd = (lambda a: a * price if (price is not None and a is not None) else None)
        buckets = []

        def add(key, amount, wallets, d24, d7, protocols=None, families=None):
            if not amount or amount <= 0:
                return
            e = {"key": key, "label": BUCKET_LABEL[key], "amount": amount, "usd": usd(amount), "share": amount / supply, "wallets": wallets,
                 "d24h": d24, "d7d": d7, "d24hUsd": usd(d24), "d7dUsd": usd(d7), "protocols": protocols or []}
            if families:
                e["families"] = families
            buckets.append(e)
        for key in ("retail", "whales", "smart_wallets"):
            if key in b:
                add(key, b[key][0], b[key][2], *dd.get(key, (0.0, 0.0)))
        add("lending_collateral", csum, sum(x[2] or 0 for x in cprot), sum(dl.get(x[0], (0, 0))[0] for x in cprot) if cprot else None,
            sum(dl.get(x[0], (0, 0))[1] for x in cprot) if cprot else None,
            [{"name": proto_name(x[0]), "id": x[0], "amount": x[1], "usd": usd(x[1]), "share": x[1] / supply, "wallets": x[2],
              "d24h": dl.get(x[0], (0, 0))[0], "d7d": dl.get(x[0], (0, 0))[1]} for x in sorted(cprot, key=lambda x: -x[1])])
        add("lending_supplied", ssum, sum(x[2] or 0 for x in sprot), None, None,
            [{"name": proto_name(x[0]), "id": x[0], "amount": x[1], "usd": usd(x[1]), "share": x[1] / supply, "wallets": x[2]}
             for x in sorted(sprot, key=lambda x: -x[1])])
        if "dex_liquidity" in b:
            fams = [{"name": x[1], "amount": num(x[2]), "usd": num(x[3]), "share": num(x[2]) / supply, "pools": x[4]}
                    for x in sorted(dsub.get(token, []), key=lambda x: -(x[2] or 0))]
            add("dex_liquidity", b["dex_liquidity"][0], b["dex_liquidity"][2], *dd.get("dex_liquidity", (0.0, 0.0)), protocols=fams)
        for key in ("perp_liquidity", "exchanges", "bridges", "vaults_contracts", "issuer"):
            if key in b:
                add(key, b[key][0], b[key][2], *dd.get(key, (0.0, 0.0)))
        if resid > 1e-9 * supply:
            add("lending_unattributed", resid, None, None, None)
        if "unknown" in b:
            add("unknown", b["unknown"][0], b["unknown"][2], *dd.get("unknown", (0.0, 0.0)))
        attributed = sum(e["amount"] for e in buckets if e["key"] != "unknown")
        states[market] = {
            "market": market, "symbol": symbol, "kind": kind, "quality": quality, "asOf": now, "updatedAt": updated, "price": price,
            "supply": supply, "supplyUsd": usd(supply), "holders": holders, "burned": burned, "buckets": buckets,
            "lendingContractsPhysical": lend_phys,
            "lendingClaims": claims,
            "topHolders": [{"address": r[1], "amount": num(r[2]), "usd": num(r[3]), "share": num(r[2]) / supply, "bucket": r[4],
                            "class": r[5], "entity": r[6]} for r in tops.get(token, [])],
            "coverage": {"notes": base_notes, "unpricedShare": 1.0 if price is None else 0.0, "attributedShare": attributed / supply,
                         "changesFrom": snap_time, "change7dComplete": full7, "econ": st["econ"]}}
    lrows = [tuple(num(x) if i >= 4 and i not in (6, 8) else x for i, x in enumerate(r)) for r in ladder_rows(c, chain)]
    # flows
    flows_rows = []
    for wname, wsec in WIN.items():
        lo = now - wsec
        cls_rows = group(c.execute(f"""SELECT token, wclass, sum(usd) FILTER (WHERE side = 'B'), sum(usd) FILTER (WHERE side = 'A'), count(*),
                                              count(DISTINCT wallet) FROM trc WHERE time >= {lo} GROUP BY ALL""").fetchall())
        topw = group(c.execute(f"""SELECT token, wallet, wclass, net FROM (
                                     SELECT token, wallet, any_value(wclass) AS wclass,
                                            sum(CASE WHEN side = 'B' THEN usd ELSE -usd END) AS net,
                                            row_number() OVER (PARTITION BY token ORDER BY abs(sum(CASE WHEN side = 'B' THEN usd ELSE -usd END)) DESC) AS rk
                                     FROM trc WHERE time >= {lo} GROUP BY token, wallet) WHERE rk <= 10 ORDER BY token, rk""").fetchall())
        lnd = group(c.execute(f"""SELECT CASE WHEN kind IN ('borrow', 'repay') THEN collateral_token ELSE asset END AS tok,
                   sum(usd) FILTER (WHERE kind IN ('supply_collateral', 'supply')), sum(usd) FILTER (WHERE kind IN ('withdraw_collateral', 'withdraw')),
                   sum(usd) FILTER (WHERE kind = 'borrow'), sum(usd) FILTER (WHERE kind = 'repay'), sum(usd) FILTER (WHERE kind = 'liquidate'),
                   count(*), sum(amount_raw) FILTER (WHERE kind IN ('supply_collateral', 'supply')),
                   sum(amount_raw) FILTER (WHERE kind IN ('withdraw_collateral', 'withdraw')), sum(amount_raw) FILTER (WHERE kind = 'liquidate')
            FROM levc WHERE time >= {lo} AND (kind NOT IN ('borrow', 'repay') OR collateral_token IS NOT NULL) GROUP BY 1""").fetchall())
        lcls = group(c.execute(f"""SELECT CASE WHEN kind IN ('borrow', 'repay') THEN collateral_token ELSE asset END AS tok, wclass,
                   sum(CASE WHEN kind IN ('supply_collateral', 'supply') THEN usd WHEN kind IN ('withdraw_collateral', 'withdraw') THEN -usd END)
            FROM levc WHERE time >= {lo} AND kind IN ('supply_collateral', 'supply', 'withdraw_collateral', 'withdraw') GROUP BY 1, 2""").fetchall())
        brg = {r[0]: r for r in c.execute(f"""SELECT x.token, sum(x.delta) FILTER (WHERE x.delta > 0), sum(-x.delta) FILTER (WHERE x.delta < 0), count(*)
                   FROM xf x JOIN cls k ON k.address = x.holder WHERE k.class = 'bridge' AND x.minute >= {lo} GROUP BY 1""").fetchall()}
        lp = group(c.execute(f"""SELECT tok, action, sum(amt), count(*) FROM (
                     SELECT asset AS tok, action, amount AS amt, time FROM lva UNION ALL SELECT asset2, action, amount2, time FROM lva)
                   WHERE action IN ('lp_add', 'lp_remove') AND time >= {lo} AND tok IS NOT NULL GROUP BY ALL""").fetchall())
        for token, (price, market, symbol, kind, quality) in prices.items():
            if market not in states:
                continue
            classes = []
            for _, wc, buy, sell, n, w in cls_rows.get(token, []):
                buy, sell = num(buy) or 0.0, num(sell) or 0.0
                classes.append({"class": wc, "buyUsd": buy, "sellUsd": sell, "netUsd": buy - sell, "trades": n, "wallets": w})
            classes.sort(key=lambda x: -(x["buyUsd"] + x["sellUsd"]))
            r = (lnd.get(token) or [None])[0]
            lending = {"supplyUsd": num(r[1]) if r else 0.0, "withdrawUsd": num(r[2]) if r else 0.0, "borrowUsd": num(r[3]) if r else 0.0,
                       "repayUsd": num(r[4]) if r else 0.0, "liquidatedUsd": num(r[5]) if r else 0.0, "events": r[6] if r else 0,
                       "supplyAmount": num(r[7]) if r else 0.0, "withdrawAmount": num(r[8]) if r else 0.0, "liquidatedAmount": num(r[9]) if r else 0.0,
                       "netCollateralUsd": ((num(r[1]) or 0.0) - (num(r[2]) or 0.0)) if r else 0.0,
                       "netCollateralByClass": {x[1]: num(x[2]) for x in lcls.get(token, [])}}
            br = brg.get(token)
            bridges = {"inUsd": num(br[2]) * price if br and br[2] and price else (0.0 if price else None),
                       "outUsd": num(br[1]) * price if br and br[1] and price else (0.0 if price else None),
                       "inAmount": num(br[2]) if br else 0.0, "outAmount": num(br[1]) if br else 0.0}
            lpd = {x[1]: (num(x[2]), x[3]) for x in lp.get(token, [])}
            liq = {"addAmount": lpd.get("lp_add", (0.0, 0))[0], "removeAmount": lpd.get("lp_remove", (0.0, 0))[0],
                   "addUsd": (lpd.get("lp_add", (0.0, 0))[0] or 0) * price if price else None,
                   "removeUsd": (lpd.get("lp_remove", (0.0, 0))[0] or 0) * price if price else None}
            obj = {"market": market, "symbol": symbol, "window": wname, "asOf": now, "updatedAt": updated, "from": lo, "price": price,
                   "classes": classes, "lending": lending, "bridges": bridges, "lp": liq,
                   "topWallets": [{"wallet": x[1], "class": x[2], "netUsd": num(x[3])} for x in topw.get(token, [])],
                   "coverage": {"notes": ["classes: DEX trades from the trade tape (deep + hot); wallet = transaction sender",
                                          "lending: Morpho Blue + Aave v3(-fork) events; borrow/repay attributed to the collateral token via the "
                                          "Morpho market (Aave borrows are cross-collateral and not attributed)",
                                          "bridges: net transfers into/out of bridge contracts; lp: live.db LP actions"],
                                "unpricedShare": 1.0 if price is None else 0.0,
                                "lendingFrom": max(lo, snap_time or lo), "lendingComplete": (snap_time or now) <= lo,
                                "bridgesFrom": max(lo, snap_time or lo), "lpFrom": max(lo, live_from or now),
                                "lpComplete": bool(live_from) and live_from <= lo}}
            flows_rows.append((market, wname, json.dumps(obj)))
    # events
    thr = {}
    for m, so in states.items():
        dex_usd = sum((e.get("usd") or 0) for e in so["buckets"] if e["key"] == "dex_liquidity")
        thr[m] = (max(25000.0, 0.005 * dex_usd), max(10000.0, 0.0025 * dex_usd))
    mk_by_token = {t: v[1] for t, v in prices.items()}
    sym = {t: v[2] for t, v in prices.items()}
    events_rows = []
    for token, time_, usd_, amt, gross, wallet, wc, tx, bn, li in c.execute(
            """SELECT token, min(time), sum(CASE WHEN side = 'B' THEN usd ELSE -usd END) AS net_usd,
                      sum(CASE WHEN side = 'B' THEN amount ELSE -amount END), sum(usd), any_value(wallet), any_value(wclass), tx, min(bn),
                      min(log_index)
               FROM trc GROUP BY token, tx HAVING abs(sum(CASE WHEN side = 'B' THEN usd ELSE -usd END)) >= 25000""").fetchall():
        m = mk_by_token.get(token)
        if m not in thr or abs(usd_) < thr[m][0]:
            continue
        side, usd_, amt = ("B" if usd_ > 0 else "A"), abs(usd_), abs(amt)
        who = {"whale": "Whale", "mev": "MEV bot", "exchange": "Exchange wallet", "contract": "Contract"}.get(wc, "Wallet")
        events_rows.append((m, time_, "whale_buy" if side == "B" else "whale_sell", num(usd_), num(amt), wallet, wc, tx, bn, li, "dex",
                            f"{who} {short(wallet)} {'bought' if side == 'B' else 'sold'} {fmt_amt(amt)} {sym[token]} ({fmt_usd(usd_)}) on a DEX"))
    for (tok, time_, kind, usd_, amt, wallet, wc, bn, li, proto, loan_tok, repaid_usd) in c.execute(
            """SELECT CASE WHEN kind IN ('borrow', 'repay') THEN collateral_token ELSE asset END, time, kind, usd, amount_raw, wallet, wclass, bn,
                      log_index, protocol, loan_token, repaid_usd FROM levc""").fetchall():
        m = mk_by_token.get(tok)
        if m not in thr or usd_ is None:
            continue
        usd_ = num(usd_)
        if kind == "liquidate":
            if usd_ < 1000:
                continue
            k, text = "liquidation", (f"{short(wallet)} was liquidated on {proto_name(proto)}: {fmt_amt(amt)} {sym[tok]} ({fmt_usd(usd_)}) seized"
                                      + (f" against {fmt_usd(num(repaid_usd))} of debt" if repaid_usd else ""))
        elif usd_ < thr[m][1]:
            continue
        elif kind in ("supply_collateral", "supply"):
            k, text = "collateral_in", f"{short(wallet)} posted {fmt_amt(amt)} {sym[tok]} ({fmt_usd(usd_)}) as collateral on {proto_name(proto)}"
        elif kind in ("withdraw_collateral", "withdraw"):
            k, text = "collateral_out", f"{short(wallet)} withdrew {fmt_amt(amt)} {sym[tok]} ({fmt_usd(usd_)}) of collateral from {proto_name(proto)}"
        elif kind == "borrow":
            k, text = "borrow", f"{short(wallet)} borrowed {fmt_usd(usd_)} of {sym.get(loan_tok, 'the loan token')} against {sym[tok]} on {proto_name(proto)}"
        else:
            k, text = "repay", f"{short(wallet)} repaid {fmt_usd(usd_)} of {sym.get(loan_tok, 'the loan token')} borrowed against {sym[tok]} on {proto_name(proto)}"
        events_rows.append((m, time_, k, usd_, num(amt), wallet, wc, None, bn, li, proto, text))
    for time_, action, proto, wallet, wcat, tok, a, txh, blk, li in c.execute(
            """SELECT time, action, protocol, wallet, wallet_category, asset, amount, tx_hash, block, log_index FROM lva
               UNION ALL SELECT time, action, protocol, wallet, wallet_category, asset2, amount2, tx_hash, block, log_index FROM lva
               WHERE asset2 IS NOT NULL""").fetchall():
        m = mk_by_token.get(tok)
        price = prices.get(tok, (None,))[0]
        usd_ = a * price if (a is not None and price is not None) else None
        if m not in thr or usd_ is None or usd_ < thr[m][1] or prices[tok][3] == "usd_anchor":
            continue
        verb = {"bridge_in": "bridged in", "bridge_out": "bridged out", "lp_add": "added liquidity with", "lp_remove": "removed liquidity of"}[action]
        events_rows.append((m, time_, action, num(usd_), num(a), wallet, wcat, txh, blk, li, proto,
                            f"{short(wallet)} {verb} {fmt_amt(a)} {sym[tok]} ({fmt_usd(usd_)}) via {proto}"))
    # briefs (numbers only from the outputs above)
    lq = group(lrows)
    flows24 = {r[0]: json.loads(r[2]) for r in flows_rows if r[1] == "24h"}
    ev24 = group([e for e in events_rows if e[1] >= now - 86400])
    briefs = {m: make_brief(so, lq.get(m, []), flows24.get(m), ev24.get(m, []), now, updated) for m, so in states.items()}
    db = res_db(chain)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM token_state")
        db.executemany("INSERT INTO token_state VALUES (?, ?, ?)", [(m, json.dumps(v), now) for m, v in states.items()])
        db.execute("DELETE FROM liq_positions")
        db.executemany("INSERT INTO liq_positions VALUES (?,?,?,?,?,?,?,?,?,?,?)", lrows)
        db.execute("DELETE FROM flows")
        db.executemany("INSERT INTO flows VALUES (?, ?, ?)", flows_rows)
        db.execute("DELETE FROM events")
        db.executemany("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", events_rows)
        db.execute("DELETE FROM brief")
        db.executemany("INSERT INTO brief VALUES (?, ?, ?)", [(m, json.dumps(v), now) for m, v in briefs.items()])
        for k, v in (("refreshed_at", updated), ("data_time", now), ("snap_time", snap_time), ("live_from", live_from or 0),
                     ("econ", st["econ"]), ("snapshot_block", st["snapshot_block"]), ("events_from", snap_time)):
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))
        db.execute("COMMIT")
    finally:
        db.close()  # rolls back an unfinished transaction on failure


def make_brief(s, lrows, f24, ev, now, updated):
    sym, price = s["symbol"], s["price"]
    facts = []
    by = {e["key"]: e for e in s["buckets"]}

    def fact(key, text, value, unit, source):
        facts.append({"key": key, "text": text, "value": value, "unit": unit, "source": source})
    ranked = sorted([e for e in s["buckets"] if e["key"] != "unknown"], key=lambda e: -(e["amount"] or 0))
    if ranked:
        t = ranked[0]
        fact("largest_bucket_share", f"{t['share'] * 100:.0f}% of {sym} supply is {t['label'].lower()}"
             + (f" ({fmt_usd(t['usd'])})" if t.get("usd") is not None else ""), t["share"], "share", "token-state")
    col = by.get("lending_collateral")
    if col:
        main = col["protocols"][0] if col["protocols"] else None
        fact("collateral_share", f"{col['share'] * 100:.1f}% of {sym} supply is posted as collateral"
             + (f", mostly on {main['name']}" if main else "") + (f" ({fmt_usd(col['usd'])})" if col.get("usd") else ""),
             col["share"], "share", "token-state")
        if col.get("usd") is not None:
            fact("collateral_usd", f"{fmt_usd(col['usd'])} of {sym} is posted as collateral", col["usd"], "usd", "token-state")
    wh = by.get("whales")
    if wh:
        fact("whales_share", f"Whales hold {wh['share'] * 100:.1f}% of {sym} supply", wh["share"], "share", "token-state")
    if lrows and price:
        within10 = sum((r[4] or 0) * price for r in lrows if r[9] is not None and price * 0.9 <= r[9] < price)
        at_risk = sum((r[4] or 0) * price for r in lrows if r[9] is not None and price <= r[9] <= 1.5 * price)
        nearest = max((r for r in lrows if r[9] is not None and r[9] < price), key=lambda r: r[9], default=None)
        fact("liquidatable_within_10pct_usd", f"{fmt_usd(within10)} of {sym} collateral liquidates within 10% below ${price:,.2f}",
             within10, "usd", "liquidations")
        if at_risk:
            fact("liquidatable_now_usd", f"{fmt_usd(at_risk)} of {sym} collateral is at or past its liquidation price", at_risk, "usd",
                 "liquidations")
        if nearest:
            fact("nearest_liq_price", f"Nearest liquidation at ${nearest[9]:,.2f} ({(nearest[9] / price - 1) * 100:.1f}%)", nearest[9],
                 "usd_price", "liquidations")
    if f24:
        cn = f24["lending"]["netCollateralUsd"]
        if cn:
            fact("collateral_net_24h_usd", f"Net {fmt_usd(abs(cn))} of {sym} {'posted as' if cn > 0 else 'withdrawn from'} collateral in 24h",
                 cn, "usd", "flows")
        dn = sum(x["netUsd"] for x in f24["classes"])
        if f24["classes"]:
            fact("dex_net_24h_usd", f"DEX flow 24h: net {fmt_usd(abs(dn))} {'bought' if dn > 0 else 'sold'}", dn, "usd", "flows")
            w = next((x for x in f24["classes"] if x["class"] == "whale"), None)
            if w and w["netUsd"]:
                fact("whale_net_24h_usd", f"Whales net {'bought' if w['netUsd'] > 0 else 'sold'} {fmt_usd(abs(w['netUsd']))} in 24h",
                     w["netUsd"], "usd", "flows")
        if f24["lending"]["liquidatedUsd"]:
            fact("liquidated_24h_usd", f"{fmt_usd(f24['lending']['liquidatedUsd'])} of {sym} collateral was liquidated in 24h",
                 f24["lending"]["liquidatedUsd"], "usd", "flows")
    if ev:
        big = max(ev, key=lambda e: e[3] or 0)
        fact("events_24h", f"{len(ev)} notable events in 24h; largest: {big[11]}", len(ev), "count", "events")
    head = facts[0]["text"] if facts else f"No economic activity data for {sym}"
    if len(facts) > 1:
        head += "; " + facts[1]["text"][0].lower() + facts[1]["text"][1:]
    return {"market": s["market"], "symbol": sym, "asOf": now, "updatedAt": updated, "price": price, "quality": s["quality"],
            "headline": head, "facts": facts,
            "coverage": {"notes": ["every number is taken from token-state, liquidations, flows and events of the same refresh"],
                         "unpricedShare": s["coverage"]["unpricedShare"], "attributedShare": s["coverage"]["attributedShare"]}}


if __name__ == "__main__":
    ch, what = sys.argv[1], sys.argv[2]
    if what in ("static", "all"):
        build_static(ch)
    if what in ("refresh", "all"):
        refresh(ch)
