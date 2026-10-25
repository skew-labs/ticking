#!/usr/bin/env python3
"""Economic Machine econ v3 fixes: the v2 fixes (fix_v2.py) applied to the incrementally refreshed v3 layer.

usage: fix_v3.py <robinhood|arbitrum> --as-of-block N [--rpc URL[,URL...]] [--skip-rpc]

Inputs  /srv/skew/em-data/econ/<chain>/v3/   (v1-style outputs recomputed to the latest block by econ_v3.py)
        /srv/skew/em-data/econ/<chain>/v2/   (seeds: NFT position owners and receipt tokens as of the snapshot)
        token transfers since the snapshot (gap + realtime chunks) and the wallet map (+ _delta) instead of the raw
        token_transfers of the snapshot range, which are no longer on this host
Outputs /srv/skew/em-data/econ/<chain>/v3/{sheet_rows,sheet_summary,lp_positions_valued}.v3.parquet, receipt_tokens.v3.csv,
        prices_head.v3.csv, prices_oracle_vault.v3.csv, morpho_coll_px.v3.csv, as_of.json
RPC (ERC-4626 convertToAssets, Morpho oracle price()) goes through rpc.py's endpoint pool as Multicall3 eth_calls; if
the pool gives up, the previous run's vault / Morpho collateral prices are reused and the run is flagged degraded.
Differences to v2: NFT owners = v2 owner (snapshot) updated by ERC721 transfers since; receipt tokens = v2 list (markets
created after the snapshot are not discovered); Comet base balances = event principal for the borrow side and the Comet token's mints - burns per account
(wallet-map edges + delta) for the supply side (see stage_comet).
"""
import argparse
import json
import os
import sys
import time

import duckdb

ROOT = "/srv/skew/em-data"
TMP = f"{ROOT}/tmp"
CHAIN_ID = {"robinhood": 4663, "arbitrum": 42161}
ZERO = "0x0000000000000000000000000000000000000000"
T0 = time.time()
PRICES_HEAD = None    # --prices-head: a corrected head-price file for dev runs (default: <E>/prices_head.csv from the prices step)


def log(msg):
    print("[%6.0fs] %s" % (time.time() - T0, msg), file=sys.stderr, flush=True)


MACROS = """
CREATE OR REPLACE MACRO w_u128(w) AS ((('0x' || substr(w, 33, 16))::UBIGINT)::UHUGEINT * 18446744073709551616::UHUGEINT + (('0x' || substr(w, 49, 16))::UBIGINT)::UHUGEINT);
CREATE OR REPLACE MACRO w_hi0(w) AS (substr(w, 1, 32) = '00000000000000000000000000000000');
CREATE OR REPLACE MACRO w_uint(w) AS (CASE WHEN w IS NULL OR length(w) < 64 THEN NULL WHEN w_hi0(w) THEN TRY_CAST(w_u128(w) AS HUGEINT) ELSE NULL END);
CREATE OR REPLACE MACRO w_dbl(w) AS (('0x' || substr(w, 1, 16))::UBIGINT::DOUBLE * 6.277101735386681e57 + ('0x' || substr(w, 17, 16))::UBIGINT::DOUBLE * 3.402823669209385e38 + ('0x' || substr(w, 33, 16))::UBIGINT::DOUBLE * 1.8446744073709552e19 + ('0x' || substr(w, 49, 16))::UBIGINT::DOUBLE);
CREATE OR REPLACE MACRO blob_dbl(b) AS w_dbl(hex(b));
CREATE OR REPLACE MACRO a2s(b) AS ('0x' || lower(hex(b)));
CREATE OR REPLACE MACRO s2b(s) AS unhex(substr(s, 3));
"""

# ---------------------------------------------------------------------------------------------------------------- RPC


def rpc_batch(calls, chunk=100):
    """calls: list of (to, data_hex). Returns list of result hex strings (None on revert/error). One Multicall3
    aggregate3 eth_call (allowFailure) per `chunk` calls through rpc.py's rotating endpoint pool; raises rpc.RpcError."""
    import rpc
    res = rpc.aggregate3([(t, d[2:] if d.startswith("0x") else d) for t, d in calls], chunk=chunk) if calls else []
    return ["0x" + b.hex() if ok and b else None for ok, b in res]


def u256(h):
    try:
        return int(h[2:66], 16)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------------------- stages


def stage_lp(con, E, TT, resume=False, E2=None, snap=0):
    """Re-derive NFT/V4/V3 LP positions: net liquidity per position key, owner from ERC721 / event, then tick math."""
    if resume and con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name IN ('lp_owned', 'pool_sqrtp_head')").fetchone()[0] == 2:
        log("lp: resume from lp_owned + pool_sqrtp_head")
        return stage_lp_value(con)
    log("lp: events")
    con.execute(f"""CREATE OR REPLACE TABLE lp_ev AS
        SELECT wallet, protocol, family, pos_key, block_number, log_index, pos_liq::DOUBLE AS liq
        FROM read_parquet('{E}/econ_actions/*.parquet', union_by_name=true)
        WHERE pos_kind = 'lp' AND family IN ('univ3', 'univ3_npm', 'univ4') AND pos_liq IS NOT NULL""")
    con.execute("""CREATE OR REPLACE TABLE lp_net AS
        SELECT family, any_value(protocol) AS protocol, pos_key, sum(liq) AS liquidity, count(*) AS n_ev,
               count(DISTINCT wallet) AS n_wallets, arg_max(wallet, block_number * 100000 + log_index) AS last_wallet,
               max(block_number) AS last_block
        FROM lp_ev GROUP BY family, pos_key""")
    # NFT contracts: position managers (V3 NPMs) and V4 senders that are ERC721 contracts (PositionManager)
    con.execute("""CREATE OR REPLACE TABLE nft_cand AS
        SELECT DISTINCT split_part(pos_key, ':', 1) AS c FROM lp_net WHERE family = 'univ3_npm'
        UNION SELECT DISTINCT split_part(pos_key, ':', 3) FROM lp_net WHERE family = 'univ4'""")
    log("lp: nft owners = v2 snapshot owners + ERC721 transfers since the snapshot" if E2 else "lp: nft owners = ERC721 transfers (full history)")
    if not (E2 and os.path.exists(f"{E2}/lp_positions_valued.v2.parquet")):
        con.execute("CREATE OR REPLACE TEMP VIEW _noseed AS SELECT NULL::VARCHAR AS wallet, NULL::VARCHAR AS family, NULL::VARCHAR AS pos_key, NULL::VARCHAR AS owner_method WHERE false")
    con.execute(f"""CREATE OR REPLACE TABLE nft_owner AS
        WITH seed AS (
          SELECT CASE WHEN family = 'univ3_npm' THEN split_part(pos_key, ':', 1) ELSE split_part(pos_key, ':', 3) END AS nft,
                 CASE WHEN family = 'univ3_npm' THEN split_part(pos_key, ':', 2)
                      ELSE CAST(w_uint(upper(substr(split_part(pos_key, ':', 4), 3))) AS VARCHAR) END AS token_id,
                 wallet AS owner, 0::UBIGINT AS ord
          FROM {f"read_parquet('{E2}/lp_positions_valued.v2.parquet')" if E2 and os.path.exists(f"{E2}/lp_positions_valued.v2.parquet") else "_noseed"}
          WHERE owner_method = 'nft_owner' AND family IN ('univ3_npm', 'univ4')),
        inc AS (
          SELECT a2s(token) AS nft, CAST(w_uint(hex(token_id)) AS VARCHAR) AS token_id, a2s("to") AS owner,
                 block_number * 1000000 + log_index AS ord
          FROM {TT} WHERE standard = 'ERC721' AND token IN (SELECT s2b(c) FROM nft_cand WHERE length(c) = 42))
        SELECT nft, token_id, arg_max(owner, ord) AS owner FROM (SELECT * FROM seed UNION ALL SELECT * FROM inc)
        WHERE nft IS NOT NULL AND token_id IS NOT NULL GROUP BY ALL""")
    con.execute("CREATE OR REPLACE TABLE nft_contracts AS SELECT DISTINCT nft FROM nft_owner")
    con.execute("""CREATE OR REPLACE TABLE lp_owned AS
        WITH k AS (
          SELECT n.*,
                 CASE WHEN family = 'univ3_npm' THEN split_part(pos_key, ':', 1)
                      WHEN family = 'univ4' THEN split_part(pos_key, ':', 3) END AS nft,
                 CASE WHEN family = 'univ3_npm' THEN split_part(pos_key, ':', 2)
                      WHEN family = 'univ4' THEN CAST(w_uint(upper(substr(split_part(pos_key, ':', 4), 3))) AS VARCHAR) END AS tid
          FROM lp_net n)
        SELECT k.*, o.owner AS nft_owner, (nc.nft IS NOT NULL) AS sender_is_nft,
               CASE WHEN family = 'univ3' THEN split_part(pos_key, ':', 2)
                    WHEN o.owner IS NOT NULL THEN o.owner
                    WHEN family = 'univ4' AND nc.nft IS NULL THEN split_part(pos_key, ':', 3)
                    ELSE last_wallet END AS owner,
               CASE WHEN family = 'univ3' THEN 'pool_owner' WHEN o.owner IS NOT NULL THEN 'nft_owner'
                    WHEN family = 'univ4' AND nc.nft IS NULL THEN 'v4_sender' ELSE 'inferred_last_sender' END AS owner_method
        FROM k LEFT JOIN nft_owner o ON o.nft = k.nft AND o.token_id = k.tid
        LEFT JOIN nft_contracts nc ON nc.nft = k.nft""")
    log("lp: pool prices")
    # group on the raw BLOB key first; hex-encode once per pool, not once per swap
    con.execute(f"""CREATE OR REPLACE TABLE pool_sqrtp_head AS
        SELECT a2s(k) AS pool, sp, sp_block FROM (
          SELECT CASE WHEN kind = 'v4' THEN v4_id ELSE emitter END AS k,
                 arg_max(sqrtp, block_number * 100000 + log_index) / 79228162514264337593543950336.0 AS sp, max(block_number) AS sp_block
          FROM read_parquet('{E}/stage/swaps/*/*.parquet', hive_partitioning=true, union_by_name=true) WHERE kind <> 'v2' AND sqrtp > 0 GROUP BY 1)""")
    stage_lp_value(con)


def stage_lp_value(con):
    log("lp: valuation")
    # geometry per family with plain equi-joins (one combined CASE/LEFT JOIN query was planned pathologically slow)
    con.execute("""CREATE OR REPLACE TABLE npm_meta AS
        SELECT npm, CAST(token_id AS VARCHAR) AS tid, pool, tick_lower, tick_upper, token0, token1 FROM d.dim_npm_tokens""")
    con.execute("""CREATE OR REPLACE TABLE lp_open AS
        SELECT family, protocol, owner, owner_method, pos_key, liquidity, n_ev, n_wallets, last_block,
               split_part(pos_key, ':', 1) AS k1, split_part(pos_key, ':', 2) AS k2,
               TRY_CAST(split_part(pos_key, ':', 3) AS INTEGER) AS k3i, TRY_CAST(split_part(pos_key, ':', 4) AS INTEGER) AS k4i,
               TRY_CAST(split_part(pos_key, ':', 5) AS INTEGER) AS k5i, TRY_CAST(split_part(pos_key, ':', 6) AS INTEGER) AS k6i
        FROM lp_owned WHERE liquidity > 0.5 AND owner IS NOT NULL AND owner <> '0x0000000000000000000000000000000000000000'""")
    con.execute("""CREATE OR REPLACE TABLE lp_geo AS
        SELECT o.* EXCLUDE (k1, k2, k3i, k4i, k5i, k6i), n.pool AS pool_ref, n.tick_lower AS tl, n.tick_upper AS tu, n.token0, n.token1
        FROM lp_open o JOIN npm_meta n ON n.npm = o.k1 AND n.tid = o.k2 WHERE o.family = 'univ3_npm'
        UNION ALL BY NAME
        SELECT o.* EXCLUDE (k1, k2, k3i, k4i, k5i, k6i), o.k1 AS pool_ref, o.k3i AS tl, o.k4i AS tu, p.token0, p.token1
        FROM lp_open o LEFT JOIN d.dim_pools p ON p.pool = o.k1 WHERE o.family = 'univ3'
        UNION ALL BY NAME
        SELECT o.* EXCLUDE (k1, k2, k3i, k4i, k5i, k6i), o.k2 AS pool_ref, o.k5i AS tl, o.k6i AS tu, v.token0, v.token1
        FROM lp_open o LEFT JOIN (SELECT pool_id, any_value(token0) token0, any_value(token1) token1 FROM d.dim_v4_pools GROUP BY 1) v
          ON v.pool_id = o.k2 WHERE o.family = 'univ4'""")
    con.execute("""CREATE OR REPLACE TABLE lp_v2 AS
        SELECT g.family, g.protocol, g.owner AS wallet, g.owner_method, g.pos_key, g.pool_ref, g.liquidity, g.tl, g.tu, s.sp, s.sp_block,
               g.n_ev, g.n_wallets, g.last_block, g.token0, g.token1,
               CASE WHEN s.sp IS NULL OR g.tl IS NULL OR g.tu IS NULL THEN NULL
                    WHEN s.sp <= pow(1.0001, g.tl / 2.0) THEN g.liquidity * (1 / pow(1.0001, g.tl / 2.0) - 1 / pow(1.0001, g.tu / 2.0))
                    WHEN s.sp >= pow(1.0001, g.tu / 2.0) THEN 0
                    ELSE g.liquidity * (1 / s.sp - 1 / pow(1.0001, g.tu / 2.0)) END AS amount0_raw,
               CASE WHEN s.sp IS NULL OR g.tl IS NULL OR g.tu IS NULL THEN NULL
                    WHEN s.sp <= pow(1.0001, g.tl / 2.0) THEN 0
                    WHEN s.sp >= pow(1.0001, g.tu / 2.0) THEN g.liquidity * (pow(1.0001, g.tu / 2.0) - pow(1.0001, g.tl / 2.0))
                    ELSE g.liquidity * (s.sp - pow(1.0001, g.tl / 2.0)) END AS amount1_raw
        FROM lp_geo g LEFT JOIN pool_sqrtp_head s ON s.pool = g.pool_ref""")
    for r in con.execute("""SELECT family, owner_method, count(*), round(sum(liquidity)) FROM lp_v2 GROUP BY ALL ORDER BY 1, 2""").fetchall():
        log("lp_v2 %s" % (r,))
    for r in con.execute("""SELECT family, count(*) AS keys, count(*) FILTER (WHERE n_wallets > 1) AS multi_sender_keys,
                                   count(*) FILTER (WHERE liquidity <= 0.5) AS closed_keys FROM lp_net GROUP BY 1""").fetchall():
        log("lp_net %s" % (r,))


def stage_prices(con, E, chain, skip_rpc):
    """Head prices v2 = v1 prices_head + ERC-4626 share prices (global) + Morpho oracle prices (market-scoped).

    Permissionless markets and vaults can report anything (robinhood has fake "WETH"/"ETH" tokens priced at $999,915 by
    their own Morpho oracle), so: tokens whose symbol imitates a Chainlink/peg-priced token are never priced this way;
    vault rates must be within [0.5, 3] of a trusted-priced asset; Morpho oracle prices are used only for collateral rows
    of that same market and only when the market has >= $100k of trusted-priced debt (real lenders exposed)."""
    con.execute(f"""CREATE OR REPLACE TABLE ph1 AS SELECT token, price_usd, source, vol_30d_usd FROM read_csv('{PRICES_HEAD or E + "/prices_head.csv"}', header=true)""")
    con.execute("""CREATE OR REPLACE TABLE trusted_px AS SELECT token, price_usd FROM ph1
                   WHERE source LIKE 'chainlink%' OR source = 'stable_peg' OR (source = 'dex_vwap' AND vol_30d_usd >= 1e6)""")
    con.execute("""CREATE OR REPLACE TABLE spoof AS
        WITH ts AS (SELECT DISTINCT upper(tm.symbol) AS sym FROM trusted_px t JOIN tm USING (token) WHERE tm.symbol IS NOT NULL)
        SELECT tm.token FROM tm JOIN ts ON upper(tm.symbol) IN (ts.sym, 'W' || ts.sym) OR 'W' || upper(tm.symbol) = ts.sym
        WHERE tm.token NOT IN (SELECT token FROM trusted_px)""")
    con.execute("CREATE OR REPLACE TABLE px_extra (token VARCHAR, price_usd DOUBLE, source VARCHAR, basis VARCHAR, rate DOUBLE)")
    con.execute("CREATE OR REPLACE TABLE morpho_coll_px (market VARCHAR, collateral VARCHAR, price_usd DOUBLE, debt_usd DOUBLE)")
    import rpc
    from common import warn
    try:
        if skip_rpc:
            raise rpc.RpcError("--skip-rpc")
        vaults = con.execute("""SELECT v.vault, v.asset, coalesce(tv.decimals, 18), coalesce(ta.decimals, 18)
                                FROM d.meta_vault v LEFT JOIN tm tv ON tv.token = v.vault LEFT JOIN tm ta ON ta.token = v.asset
                                WHERE v.vault IS NOT NULL AND v.asset IS NOT NULL""").fetchall()
        res = rpc_batch([(v[0], "0x07a2d13a" + "%064x" % (10 ** int(v[2]))) for v in vaults])
        rates = [(v[0], v[1], u256(r) / 10 ** int(v[3])) for v, r in zip(vaults, res) if u256(r)]
        log("prices: %d ERC-4626 vaults, %d answered convertToAssets" % (len(vaults), len(rates)))
        con.execute("CREATE OR REPLACE TABLE vault_rate (vault VARCHAR, asset VARCHAR, rate DOUBLE)")
        if rates:
            con.executemany("INSERT INTO vault_rate VALUES (?, ?, ?)", rates)
        for _ in range(2):   # vault-of-vault
            con.execute("""INSERT INTO px_extra
                SELECT r.vault, r.rate * coalesce(t.price_usd, x.price_usd), 'erc4626:convertToAssets', r.asset, r.rate
                FROM vault_rate r LEFT JOIN trusted_px t ON t.token = r.asset LEFT JOIN px_extra x ON x.token = r.asset
                WHERE coalesce(t.price_usd, x.price_usd) IS NOT NULL AND r.rate BETWEEN 0.5 AND 3
                  AND r.vault NOT IN (SELECT token FROM px_extra) AND r.vault NOT IN (SELECT token FROM spoof)
                  AND r.vault NOT IN (SELECT token FROM trusted_px)""")
        mk = con.execute("""SELECT m.id, m.oracle, m.collateral_token, m.loan_token, coalesce(tc.decimals, 18), coalesce(tl.decimals, 18)
                            FROM d.dim_morpho_markets m LEFT JOIN tm tc ON tc.token = m.collateral_token LEFT JOIN tm tl ON tl.token = m.loan_token
                            WHERE m.oracle IS NOT NULL AND m.oracle <> '0x0000000000000000000000000000000000000000'
                              AND m.collateral_token <> '0x0000000000000000000000000000000000000000'""").fetchall()
        res = rpc_batch([(m[1], "0xa035b1fe") for m in mk])
        orows = [(m[0], m[2], m[3], u256(r) / 1e36 * 10 ** (int(m[4]) - int(m[5]))) for m, r in zip(mk, res) if u256(r)]
        log("prices: %d Morpho market oracles, %d answered price()" % (len(mk), len(orows)))
        con.execute("CREATE OR REPLACE TABLE morpho_oracle_px (market VARCHAR, collateral VARCHAR, loan VARCHAR, px_in_loan DOUBLE)")
        if orows:
            con.executemany("INSERT INTO morpho_oracle_px VALUES (?, ?, ?, ?)", orows)
        con.execute(f"""INSERT INTO morpho_coll_px
            WITH debt AS (SELECT pos_key AS market, sum(amount) AS debt_raw FROM read_parquet('{E}/positions_head.parquet')
                          WHERE method = 'morpho_shares' AND kind = 'debt' AND amount > 0 GROUP BY 1)
            SELECT o.market, o.collateral, o.px_in_loan * t.price_usd,
                   d.debt_raw / pow(10, coalesce(tl.decimals, 18)) * t.price_usd AS debt_usd
            FROM morpho_oracle_px o JOIN trusted_px t ON t.token = o.loan JOIN debt d ON d.market = o.market
            LEFT JOIN tm tl ON tl.token = o.loan
            WHERE o.px_in_loan > 0 AND o.collateral NOT IN (SELECT token FROM spoof)
              AND d.debt_raw / pow(10, coalesce(tl.decimals, 18)) * t.price_usd >= 100000""")
        # keep only markets that are not running away and whose oracle agrees (within 1.5x) with an independent price:
        # the token's latest DEX VWAP (any volume, <= 120 days) or its ERC-4626 share price
        con.execute(f"""CREATE OR REPLACE TABLE indep_px AS
            SELECT token, arg_max(price_usd, day) AS price_usd FROM read_parquet('{E}/prices_daily.parquet')
            WHERE source = 'dex_vwap' AND day >= (SELECT max(day) FROM read_parquet('{E}/prices_daily.parquet')) - INTERVAL 120 DAY GROUP BY 1
            UNION ALL SELECT token, price_usd FROM px_extra""")
        con.execute("""DELETE FROM morpho_coll_px m WHERE m.market IN (SELECT market FROM morpho_mkt WHERE factor IS NOT NULL)
            OR NOT EXISTS (SELECT 1 FROM indep_px i WHERE i.token = m.collateral AND i.price_usd > 0
                           AND m.price_usd / i.price_usd BETWEEN 0.667 AND 1.5)""")
    except rpc.RpcError as e:
        con.execute("DELETE FROM px_extra")
        con.execute("DELETE FROM morpho_coll_px")
        prev = [f for f in (f"{E}/prices_oracle_vault.v3.csv", f"{E}/morpho_coll_px.v3.csv") if os.path.exists(f)]
        if prev[:1] == [f"{E}/prices_oracle_vault.v3.csv"]:
            cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_csv('{prev[0]}', header=true, all_varchar=true)").fetchall()}
            rate = "TRY_CAST(p.rate AS DOUBLE)" if "rate" in cols else "NULL::DOUBLE"
            # a vault's price is its rate times today's price of its asset (not the old product: arbitrum LINK vaults kept $1.2e11)
            con.execute(f"""INSERT INTO px_extra
                SELECT p.token, CASE WHEN {rate} IS NOT NULL AND t.price_usd IS NOT NULL THEN {rate} * t.price_usd
                                     ELSE TRY_CAST(p.price_usd AS DOUBLE) END, p.source, p.basis, {rate}
                FROM read_csv('{prev[0]}', header=true, all_varchar=true) p LEFT JOIN trusted_px t ON t.token = p.basis""")
        if f"{E}/morpho_coll_px.v3.csv" in prev:
            con.execute(f"""INSERT INTO morpho_coll_px SELECT market, collateral, TRY_CAST(price_usd AS DOUBLE), TRY_CAST(debt_usd AS DOUBLE)
                            FROM read_csv('{E}/morpho_coll_px.v3.csv', header=true, all_varchar=true)""")
        msg = (f"prices: RPC unavailable ({e}); vault share prices / Morpho collateral prices from the previous run "
               f"({con.execute('SELECT count(*) FROM px_extra').fetchone()[0]} / {con.execute('SELECT count(*) FROM morpho_coll_px').fetchone()[0]})")
        if skip_rpc:
            log(msg)
        else:
            warn(msg)
    con.execute("""CREATE OR REPLACE TABLE ph2 AS
        WITH c AS (
          SELECT token, price_usd, source, vol_30d_usd, CASE WHEN source LIKE 'chainlink%' OR source = 'stable_peg' THEN 0 ELSE 2 END AS rk FROM ph1
          UNION ALL SELECT token, price_usd, source, NULL, 1 FROM px_extra WHERE price_usd > 0)
        SELECT token, arg_min(price_usd, rk) AS price_usd, arg_min(source, rk) AS source, arg_min(vol_30d_usd, rk) AS vol_30d_usd
        FROM c GROUP BY token""")
    log("prices: %d vault share prices, %d market-scoped Morpho collateral prices, %d spoofed-symbol tokens excluded" % (
        con.execute("SELECT count(*) FROM px_extra").fetchone()[0], con.execute("SELECT count(*) FROM morpho_coll_px").fetchone()[0],
        con.execute("SELECT count(*) FROM spoof").fetchone()[0]))


def stage_lending(con, E2):
    """Receipt tokens of event-principal lending forks.
    v2 discovered them by matching events at the snapshot, which missed most Aave-v2-style reserves (arbitrum radiant_v2:
    6 found, none of WETH / USDC / USDT / ARB / WBTC), so those markets fell back to event principal: 554k "WETH supplied"
    against 3 WETH of rWETH outstanding. v4: every Aave-v2-family pool's aTokens (supplied) and debt tokens (stable and
    variable: debt) come from the reserve dimension (d.dim_aave_tokens, from the pool's own events), at rate 1 (they are
    denominated in the underlying). The discovered list is kept for the other families (Compound-v2 cTokens with rates).
    Aave-v3-family pools are not listed: their positions are exact scaled balances (aave_scaled), not event principal."""
    disc = (f"""SELECT protocol, asset, kind, token, TRY_CAST(n_matched AS BIGINT) AS n_matched, TRY_CAST(n_open AS BIGINT) AS n_open,
                       TRY_CAST(match_share AS DOUBLE) AS match_share, TRY_CAST(rate AS DOUBLE) AS rate
                FROM read_csv('{E2}/receipt_tokens.v2.csv', header=true, all_varchar=true)""" if E2 and os.path.exists(f"{E2}/receipt_tokens.v2.csv")
            else """SELECT NULL::VARCHAR AS protocol, NULL::VARCHAR AS asset, NULL::VARCHAR AS kind, NULL::VARCHAR AS token, NULL::BIGINT AS n_matched,
                    NULL::BIGINT AS n_open, NULL::DOUBLE AS match_share, NULL::DOUBLE AS rate WHERE false""")   # no v2 discovery outside the refresh
    con.execute(f"""CREATE OR REPLACE TABLE receipt_token AS
        WITH disc AS ({disc}),
        v2pools AS (SELECT pool, protocol FROM d.dim_aave_pools WHERE fam = 'aave_v2')
        SELECT * FROM disc WHERE protocol NOT IN (SELECT protocol FROM v2pools)
        UNION ALL
        SELECT DISTINCT p.protocol, lower(t.underlying) AS asset, CASE t.kind WHEN 'atoken' THEN 'supplied' ELSE 'debt' END AS kind,
               lower(t.token) AS token, NULL::BIGINT, NULL::BIGINT, NULL::DOUBLE, 1.0::DOUBLE
        FROM d.dim_aave_tokens t JOIN v2pools p ON p.pool = t.pool WHERE t.underlying IS NOT NULL""")
    log("lending: %d receipt tokens (%d from the Aave-v2-family reserve dimension)" % con.execute(
        "SELECT count(*), count(*) FILTER (WHERE n_matched IS NULL) FROM receipt_token").fetchone())


def stage_backing(con, E, OUT, skip_rpc):
    """Reserve backing of Aave-family pools, read on chain: per (protocol, underlying) the aTokens' totalSupply (claims), the
    underlying held by the aTokens (cash), the debt tokens' totalSupply (debt) and, for Aave-v3-family pools, the v3.3
    reserve deficit. Event and transfer histories cannot see a reserve whose contracts were upgraded or drained (arbitrum
    radiant_v2 after the Oct 2024 exploit: its rTokens now revert on balanceOf / totalSupply, and $60k of cash sits against
    $28M of decoded claims). status: unreadable (an aToken's totalSupply reverts), unbacked ((cash + debt) / claims < 0.5),
    deficit (an Aave v3.3 deficit is recorded; informational) or ok. Rows of unreadable / unbacked reserves get quality
    reserve_unbacked and leave the verified net worth. Without RPC the previous file is reused."""
    con.execute("""CREATE OR REPLACE TABLE rb_tok AS
        SELECT p.protocol, p.fam, lower(p.pool) AS pool, lower(t.underlying) AS asset, t.kind, lower(t.token) AS token
        FROM d.dim_aave_tokens t JOIN d.dim_aave_pools p ON p.pool = t.pool WHERE t.underlying IS NOT NULL""")
    toks = con.execute("SELECT protocol, fam, pool, asset, kind, token FROM rb_tok").fetchall()
    out = f"{OUT}/reserve_backing.v3.csv"
    try:
        if skip_rpc:
            raise rpc_error("--skip-rpc")
        calls = []
        for proto, fam, pool, asset, kind, tok in toks:
            calls.append((tok, "0x18160ddd"))                                   # totalSupply()
            if kind == "atoken":
                calls.append((asset, "0x70a08231" + tok[2:].rjust(64, "0")))   # underlying.balanceOf(aToken)
        res = iter(rpc_batch(calls))
        agg, bad_tok = {}, []
        for proto, fam, pool, asset, kind, tok in toks:
            a = agg.setdefault((proto, asset), {"fam": fam, "pool": pool, "claims": 0, "cash": 0, "debt": 0, "unreadable": 0, "atokens": 0})
            ts = next(res)
            if ts is None:
                bad_tok.append((tok,))
            if kind == "atoken":
                cash = next(res)
                a["atokens"] += 1
                if ts is None:
                    a["unreadable"] += 1
                else:
                    a["claims"] += u256(ts)
                a["cash"] += u256(cash) if cash else 0
            elif ts is not None:
                a["debt"] += u256(ts)
        v3 = sorted({(pool, asset) for (proto, asset), a in agg.items() for pool in [a["pool"]] if a["fam"] == "aave_v3"})
        dres = rpc_batch([(pool, "0xc952485d" + asset[2:].rjust(64, "0")) for pool, asset in v3])   # getReserveDeficit(asset)
        dmap = {k: (u256(r) if r else None) for k, r in zip(v3, dres)}
        rows = []
        for (proto, asset), a in agg.items():
            cov = (a["cash"] + a["debt"]) / a["claims"] if a["claims"] else None
            dfc = dmap.get((a["pool"], asset))
            st = ("unreadable" if a["unreadable"] else "unbacked" if cov is not None and cov < 0.5 and a["claims"] > 0
                  else "deficit" if dfc else "ok")
            rows.append((proto, asset, a["fam"], a["pool"], str(a["claims"]), str(a["cash"]), str(a["debt"]),
                         str(dfc) if dfc is not None else None, cov, a["unreadable"], st))
        con.execute("""CREATE OR REPLACE TABLE reserve_backing (protocol VARCHAR, asset VARCHAR, fam VARCHAR, pool VARCHAR, claims_raw VARCHAR,
                       cash_raw VARCHAR, debt_raw VARCHAR, deficit_raw VARCHAR, coverage DOUBLE, unreadable_atokens INTEGER, status VARCHAR)""")
        con.executemany("INSERT INTO reserve_backing VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY reserve_backing TO '{out}.tmp' (FORMAT csv, HEADER)")
        os.replace(f"{out}.tmp", out)
        con.execute("CREATE OR REPLACE TABLE rb_bad_tok (token VARCHAR)")
        if bad_tok:
            con.executemany("INSERT INTO rb_bad_tok VALUES (?)", bad_tok)
        con.execute(f"COPY rb_bad_tok TO '{out[:-4]}.tokens_unreadable.csv' (FORMAT csv, HEADER)")
    except Exception as e:                     # rpc.RpcError / budget: keep the previous run's backing
        prev = next((f for f in (out, f"{E}/reserve_backing.v3.csv") if os.path.exists(f)), None)
        log("backing: RPC unavailable (%s); %s" % (str(e)[:80], "previous file " + prev if prev else "no previous file, no reserve flags"))
        if prev:
            con.execute(f"CREATE OR REPLACE TABLE reserve_backing AS SELECT * FROM read_csv('{prev}', header=true, all_varchar=true)")
        else:
            con.execute("CREATE OR REPLACE TABLE reserve_backing (protocol VARCHAR, asset VARCHAR, status VARCHAR)")
        pt = (prev or "")[:-4] + ".tokens_unreadable.csv"
        con.execute(f"CREATE OR REPLACE TABLE rb_bad_tok AS SELECT * FROM read_csv('{pt}', header=true, all_varchar=true)" if prev and os.path.exists(pt)
                    else "CREATE OR REPLACE TABLE rb_bad_tok (token VARCHAR)")
    # rows that name their receipt token are judged by that token (a reserve may have an old, reverting token generation and
    # a new, readable one); event-principal rows by the reserve; unbacked reserves (coverage) flag every row of the reserve
    con.execute("CREATE OR REPLACE TABLE reserve_bad AS SELECT DISTINCT protocol, asset, status FROM reserve_backing WHERE status IN ('unreadable', 'unbacked')")
    log("backing: %s" % con.execute("SELECT list(status || '=' || n) FROM (SELECT status, count(*) AS n FROM reserve_backing GROUP BY 1)").fetchone()[0])


def rpc_error(msg):
    import rpc
    return rpc.RpcError(msg)


def stage_morpho(con, E):
    """Morpho Blue markets whose share->asset value ran away from the money ever supplied (interest accruing on bad debt
    at 100% utilisation, e.g. arbitrum market 0xfdb8...: $3.2M principal valued at $3.06B). Positions there are re-valued
    at their share of the net principal and flagged."""
    con.execute(f"""CREATE OR REPLACE TABLE morpho_mkt AS
        WITH ev AS (
          SELECT pos_key AS market, pos_kind AS kind,
                 sum(CASE WHEN action IN ('supply', 'borrow') THEN amount_raw::DOUBLE ELSE 0 END) AS gross_in,
                 sum(CASE WHEN action IN ('supply', 'borrow') THEN amount_raw::DOUBLE
                          WHEN action IN ('withdraw', 'repay', 'liquidated') THEN -amount_raw::DOUBLE ELSE 0 END) AS net_principal
          FROM read_parquet('{E}/econ_actions/*.parquet', union_by_name=true) WHERE family = 'morpho_blue' AND pos_kind IN ('supplied', 'debt') GROUP BY ALL),
        ph AS (SELECT pos_key AS market, kind, sum(amount) AS assets, count(*) AS n FROM read_parquet('{E}/positions_head.parquet')
               WHERE method = 'morpho_shares' AND kind IN ('supplied', 'debt') AND amount > 0 GROUP BY ALL)
        SELECT ph.market, ph.kind, ph.assets, ph.n, ev.gross_in, ev.net_principal,
               CASE WHEN ph.assets > 3 * greatest(ev.gross_in, 1) THEN greatest(ev.net_principal, 0) / ph.assets END AS factor
        FROM ph JOIN ev USING (market, kind)""")
    for r in con.execute("""SELECT market, kind, n, assets, gross_in, net_principal, factor FROM morpho_mkt
                            WHERE factor IS NOT NULL ORDER BY assets DESC LIMIT 6""").fetchall():
        log("morpho runaway %s" % (r,))
    log("morpho: %d runaway market legs" % con.execute("SELECT count(*) FROM morpho_mkt WHERE factor IS NOT NULL").fetchone()[0])


def stage_comet(con, E, MAPD, cache=None):
    """Compound v3 (Comet) base balances.
    v1 used signed event principal per wallet (Supply - Withdraw + AbsorbDebt). Base balances also move between accounts
    (transfer / transferFrom of the Comet token, used by routers, bulkers and vaults) and those moves emit no Supply or
    Withdraw, only the Comet's ERC-20 Transfer events, and always through address 0: Transfer(src, 0, pv) and
    Transfer(0, dst, pv). v2 netted direct src -> dst transfers, which a Comet never emits, so it found none: arbitrum
    0xd9bd... kept $506M of supply it had moved on, and 0x94dd... ran $654M "into debt" withdrawing balance it had received.
    v4: the supply side is the Comet token's own ledger, mints - burns per account (wallet-map edges + delta). It covers
    supply, withdraw, transfers and absorbs, in present value at each event (accrued interest left out: a lower bound).
    The borrow side is event principal minus that supply side (debt = SS - P), kept only for accounts that ever posted
    collateral to that Comet (a Comet cannot lend without collateral). Event principal is cut at the last block of the
    edge delta so both sides cover the same blocks."""
    cache = cache or E
    delta = os.path.realpath(f"{MAPD}/_delta")
    try:
        dto = int(json.load(open(f"{delta}/meta.json"))["to"])
    except (OSError, ValueError, KeyError):
        dto = None
    cut = f"AND block_number <= {dto}" if dto else ""
    con.execute(f"""CREATE OR REPLACE TABLE comet_p AS
        SELECT wallet, any_value(protocol) AS protocol, emitter AS comet, any_value(asset) AS asset, sum(pos_sign * amount_raw::DOUBLE) AS p
        FROM read_parquet('{E}/econ_actions/*.parquet', union_by_name=true)
        WHERE family = 'comet' AND pos_kind = 'comet_base' AND wallet IS NOT NULL {cut}
        GROUP BY wallet, emitter""")
    con.execute(f"""CREATE OR REPLACE TABLE comet_coll AS
        SELECT DISTINCT wallet, emitter AS comet FROM read_parquet('{E}/econ_actions/*.parquet', union_by_name=true)
        WHERE family = 'comet' AND pos_kind = 'collateral' AND pos_sign = 1 {cut}""")
    con.execute("CREATE OR REPLACE TABLE comet_map AS SELECT comet, any_value(protocol) AS protocol, any_value(asset) AS asset FROM comet_p GROUP BY 1")
    Z = ZERO[2:].upper()
    base = f"{cache}/comet_ss_base.parquet"
    if not os.path.exists(base):
        log("comet: Comet-token mints and burns per account from the wallet map (one-time)")
        con.execute(f"""COPY (
            SELECT a2s(w) AS wallet, a2s(asset) AS comet, sum(a) AS ss FROM (
              SELECT asset, "to" AS w, CAST(COALESCE(CAST(total_amount AS DOUBLE), TRY_CAST(total_amount_big AS DOUBLE)) AS DOUBLE) AS a
              FROM read_parquet('{MAPD}/edges/**/*.parquet') WHERE kind = 'erc20' AND asset IN (SELECT s2b(comet) FROM comet_map) AND hex("from") = '{Z}'
              UNION ALL
              SELECT asset, "from", -CAST(COALESCE(CAST(total_amount AS DOUBLE), TRY_CAST(total_amount_big AS DOUBLE)) AS DOUBLE)
              FROM read_parquet('{MAPD}/edges/**/*.parquet') WHERE kind = 'erc20' AND asset IN (SELECT s2b(comet) FROM comet_map) AND hex("to") = '{Z}')
            GROUP BY ALL) TO '{base}' (FORMAT parquet)""")
    dlt = f"{delta}/edges.parquet"
    dsql = f"""UNION ALL
          SELECT a2s(w), a2s(asset), sum(a) FROM (
            SELECT asset, "to" AS w, total_amount::DOUBLE AS a FROM read_parquet('{dlt}')
            WHERE kind = 'erc20' AND asset IN (SELECT s2b(comet) FROM comet_map) AND hex("from") = '{Z}'
            UNION ALL
            SELECT asset, "from", -total_amount::DOUBLE FROM read_parquet('{dlt}')
            WHERE kind = 'erc20' AND asset IN (SELECT s2b(comet) FROM comet_map) AND hex("to") = '{Z}')
          GROUP BY ALL""" if os.path.exists(dlt) else ""
    con.execute(f"CREATE OR REPLACE TABLE comet_ss AS SELECT wallet, comet, sum(ss) AS ss FROM (SELECT * FROM read_parquet('{base}') {dsql}) GROUP BY ALL")
    con.execute("""CREATE OR REPLACE TABLE comet_v2 AS
        WITH k AS (
          SELECT k.wallet, k.comet, m.protocol, m.asset, coalesce(p.p, 0) AS p, coalesce(s.ss, 0) AS ss, cc.wallet IS NOT NULL AS has_coll,
                 pow(10, coalesce(tm.decimals, 18) - 2) AS dust
          FROM (SELECT wallet, comet FROM comet_p UNION SELECT wallet, comet FROM comet_ss) k
          JOIN comet_map m ON m.comet = k.comet
          LEFT JOIN comet_p p ON p.wallet = k.wallet AND p.comet = k.comet
          LEFT JOIN comet_ss s ON s.wallet = k.wallet AND s.comet = k.comet
          LEFT JOIN comet_coll cc ON cc.wallet = k.wallet AND cc.comet = k.comet
          LEFT JOIN tm ON tm.token = m.asset)
        SELECT wallet, comet, protocol, asset, p, ss AS x, has_coll,
               CASE WHEN ss > dust THEN ss WHEN has_coll AND ss - p > dust THEN -(ss - p) ELSE 0 END AS bal
        FROM k""")
    r = con.execute("""SELECT count(*), count(*) FILTER (WHERE bal > 0), count(*) FILTER (WHERE bal < 0),
                              count(*) FILTER (WHERE p - x > 0 AND bal <= 0 AND NOT has_coll) FROM comet_v2""").fetchone()
    log("comet: %d wallet-comets (edges to block %s), %d suppliers, %d borrowers, %d with event supply moved on by transfer" % (r[0], dto, *r[1:]))


def stage_sheets(con, E, chain):
    """balance_sheets v2 for the v1 sheet-wallet universe, with per-row quality and per-wallet flags."""
    cid = CHAIN_ID[chain]
    con.execute(f"CREATE OR REPLACE TABLE bs1 AS SELECT * FROM read_parquet('{E}/balance_sheets.parquet')")
    con.execute("CREATE OR REPLACE TABLE sw AS SELECT DISTINCT wallet FROM bs1")
    con.execute(f"""CREATE OR REPLACE TABLE lb AS SELECT wallet, token, balance_raw::DOUBLE AS bal
                    FROM read_parquet('{E}/liquid_balances_sheet_wallets.parquet')""")
    # method per lending protocol (as in 40_positions.sql)
    con.execute("""CREATE OR REPLACE TABLE rows_keep AS
        SELECT b.*, CASE WHEN b.category IN ('supplied', 'collateral', 'debt') THEN
                      CASE WHEN b.protocol LIKE 'aave_v3%' THEN 'aave_scaled' WHEN b.protocol LIKE 'morpho%' THEN 'morpho_shares'
                           WHEN b.protocol LIKE 'gmx%' THEN 'abs_state' WHEN b.protocol LIKE 'comet%' OR b.protocol LIKE 'compound_v3%' THEN 'comet_principal'
                           ELSE 'event_principal' END END AS method
        FROM bs1 b
        WHERE NOT (b.category = 'lp' AND b.pos_key LIKE '%:%')                                   -- NFT/V3/V4 LP re-derived below
          AND NOT ((b.protocol LIKE 'comet%' OR b.protocol LIKE 'compound_v3%') AND b.category IN ('supplied', 'debt'))   -- Comet base re-derived
          AND NOT (b.category = 'liquid' AND b.asset IN (SELECT token FROM receipt_token))""")     # receipt tokens are positions
    # event-principal rows: replace by receipt-token balance where the market's receipt token is known
    con.execute("""CREATE OR REPLACE TABLE lend_v2 AS
        SELECT r.wallet, r.category, r.protocol, r.asset, r.symbol, r.amount AS amount_v1,
               CASE WHEN rt.token IS NOT NULL THEN coalesce(lb.bal, 0) * rt.rate / pow(10, coalesce(tm.decimals, 18)) END AS amount_tok,
               rt.token AS receipt_token, r.pos_key, r.ui_multiplier, r.method
        FROM rows_keep r
        LEFT JOIN receipt_token rt ON rt.protocol = r.protocol AND rt.asset = r.asset
             AND rt.kind = CASE WHEN r.category = 'debt' THEN 'debt' ELSE r.category END
        LEFT JOIN lb ON lb.wallet = r.wallet AND lb.token = rt.token
        LEFT JOIN tm ON tm.token = r.asset
        WHERE r.method = 'event_principal'""")
    # holders of receipt tokens that had no v1 row (e.g. v1 clamped them away or received the token by transfer)
    con.execute("""CREATE OR REPLACE TABLE lend_new AS
        SELECT lb.wallet, CASE WHEN rt.kind = 'debt' THEN 'debt' ELSE rt.kind END AS category, rt.protocol, rt.asset, tm.symbol,
               lb.bal * rt.rate / pow(10, coalesce(tm.decimals, 18)) AS amount, rt.token AS receipt_token
        FROM lb JOIN receipt_token rt ON rt.token = lb.token LEFT JOIN tm ON tm.token = rt.asset
        WHERE NOT EXISTS (SELECT 1 FROM lend_v2 l WHERE l.wallet = lb.wallet AND l.receipt_token = rt.token)""")
    con.execute("""CREATE OR REPLACE TABLE lp_rows AS
        SELECT l.wallet, 'lp' AS category, l.protocol, CASE s.k WHEN 0 THEN l.token0 ELSE l.token1 END AS asset, tm.symbol,
               CASE s.k WHEN 0 THEN l.amount0_raw ELSE l.amount1_raw END / pow(10, coalesce(tm.decimals, 18)) AS amount,
               l.pos_key, l.owner_method, l.sp IS NULL AS no_price_state
        FROM lp_v2 l JOIN sw USING (wallet) CROSS JOIN (VALUES (0), (1)) s(k)
        LEFT JOIN tm ON tm.token = CASE s.k WHEN 0 THEN l.token0 ELSE l.token1 END""")
    con.execute(f"""CREATE OR REPLACE TABLE bs2_raw AS
        SELECT r.wallet, r.category, r.protocol, r.asset, r.symbol, r.amount * coalesce(m.factor, 1) AS amount, r.ui_multiplier, r.pos_key,
               r.method, NULL::VARCHAR AS receipt_token, NULL::VARCHAR AS owner_method, false AS no_price_state, r.usd AS usd_v1,
               m.factor IS NOT NULL AS morpho_runaway
        FROM rows_keep r LEFT JOIN morpho_mkt m ON r.method = 'morpho_shares' AND m.market = r.pos_key
             AND m.kind = CASE WHEN r.category = 'debt' THEN 'debt' ELSE 'supplied' END AND r.category IN ('supplied', 'debt')
        WHERE coalesce(r.method, '') <> 'event_principal'
        UNION ALL BY NAME
        SELECT wallet, category, protocol, asset, symbol, coalesce(amount_tok, amount_v1) AS amount, ui_multiplier, pos_key,
               CASE WHEN amount_tok IS NOT NULL THEN 'receipt_token_balance' ELSE 'event_principal' END AS method, receipt_token
        FROM lend_v2
        UNION ALL BY NAME
        SELECT wallet, category, protocol, asset, symbol, amount, receipt_token AS pos_key, 'receipt_token_balance' AS method, receipt_token
        FROM lend_new JOIN sw USING (wallet)
        UNION ALL BY NAME
        SELECT wallet, category, protocol, asset, symbol, amount, pos_key, 'lp_nft_net' AS method, owner_method, no_price_state
        FROM lp_rows
        UNION ALL BY NAME
        SELECT c.wallet, CASE WHEN c.bal > 0 THEN 'supplied' ELSE 'debt' END AS category, c.protocol, c.asset, tm.symbol,
               abs(c.bal) / pow(10, coalesce(tm.decimals, 18)) AS amount, c.comet AS pos_key, 'comet_base_v2' AS method
        FROM comet_v2 c JOIN sw USING (wallet) LEFT JOIN tm ON tm.token = c.asset
        WHERE c.bal > 0 OR (c.bal < 0 AND c.has_coll)""")
    # decoded receipt-token balances may never exceed what the token itself says exists (arbitrum radiant_v1: 1.23M USDC decoded
    # against 105k of rUSDC totalSupply, debt 2.47M against 27k): such a reserve's history no longer explains its state
    has_claims = "claims_raw" in {r[0] for r in con.execute("DESCRIBE reserve_backing").fetchall()}
    con.execute(f"""CREATE OR REPLACE TABLE reserve_over AS
        WITH dec AS (SELECT protocol, asset, CASE WHEN category = 'debt' THEN 'debt' ELSE 'supply' END AS side, sum(amount) AS amt
                     FROM bs2_raw WHERE method = 'receipt_token_balance' GROUP BY ALL),
        onc AS (SELECT protocol, asset, 'supply' AS side, TRY_CAST(claims_raw AS DOUBLE) AS raw FROM reserve_backing
                UNION ALL SELECT protocol, asset, 'debt', TRY_CAST(debt_raw AS DOUBLE) FROM reserve_backing)
        SELECT d.protocol, d.asset, d.side, d.amt, o.raw / pow(10, coalesce(tm.decimals, 18)) AS onchain
        FROM dec d JOIN onc o USING (protocol, asset, side) LEFT JOIN tm ON tm.token = d.asset
        WHERE d.amt > 0 AND o.raw IS NOT NULL AND d.amt > 1.5 * o.raw / pow(10, coalesce(tm.decimals, 18)) + 1e-6""" if has_claims else
        "CREATE OR REPLACE TABLE reserve_over (protocol VARCHAR, asset VARCHAR, side VARCHAR, amt DOUBLE, onchain DOUBLE)")
    log("backing: %d reserve sides whose decoded receipt balances exceed 1.5x the token's totalSupply" %
        con.execute("SELECT count(*) FROM reserve_over").fetchone()[0])
    con.execute(f"""CREATE OR REPLACE TABLE balance_sheets_v2 AS
        WITH x0 AS (
          -- market-scoped Morpho oracle beats a missing or thin DEX price for that market's collateral (never Chainlink/peg)
          SELECT r.*, CASE WHEN mc.price_usd IS NOT NULL AND (ph.source IS NULL OR ph.source LIKE 'dex_vwap%') THEN mc.price_usd ELSE ph.price_usd END AS price_usd,
                 CASE WHEN mc.price_usd IS NOT NULL AND (ph.source IS NULL OR ph.source LIKE 'dex_vwap%') THEN 'morpho_oracle' ELSE ph.source END AS psrc,
                 ph.vol_30d_usd,
                 CASE WHEN r.category = 'perp_size' OR (r.category = 'perp_collateral' AND r.protocol = 'gmx_v1') THEN r.amount
                      WHEN mc.price_usd IS NOT NULL AND (ph.source IS NULL OR ph.source LIKE 'dex_vwap%') THEN r.amount * mc.price_usd
                      ELSE r.amount * ph.price_usd END AS u
                 , r.category IN ('supplied', 'collateral', 'debt') AND (rbad.status = 'unbacked' OR (rbad.status = 'unreadable' AND
                   (r.receipt_token IS NULL OR r.receipt_token IN (SELECT token FROM rb_bad_tok)))) AS reserve_unbacked,
                 rov.protocol IS NOT NULL AS decoded_over
          FROM bs2_raw r LEFT JOIN ph2 ph ON ph.token = r.asset
          LEFT JOIN reserve_bad rbad ON rbad.protocol = r.protocol AND rbad.asset = r.asset
          LEFT JOIN reserve_over rov ON rov.protocol = r.protocol AND rov.asset = r.asset AND r.method = 'receipt_token_balance'
               AND rov.side = CASE WHEN r.category = 'debt' THEN 'debt' ELSE 'supply' END
          LEFT JOIN morpho_coll_px mc ON r.category = 'collateral' AND r.protocol LIKE 'morpho%' AND mc.market = r.pos_key AND mc.collateral = r.asset
          WHERE r.amount IS NOT NULL AND abs(r.amount) > 1e-12),
        anch AS (SELECT price_usd AS p FROM ph2 WHERE source IN ('chainlink:ETH / USD', 'chainlink:BTC / USD') AND price_usd > 0),
        -- stablecoins and ETH / BTC derivatives (LSTs, LRTs, wrapped BTC) track $1 or an anchor: a thin Arbitrum DEX market does not
        -- make a large holding of them unpriceable (arbitrum 0x2a95...: 13.5k weETH collateral against 13.9k WETH debt read as -$33.6M)
        x AS (SELECT x0.*, coalesce(abs(x0.price_usd - 1) <= 0.1 OR EXISTS (SELECT 1 FROM anch WHERE x0.price_usd / anch.p BETWEEN 0.9 AND 1.5), false) AS pegged
              FROM x0 x0)
        SELECT {cid} AS chain, wallet, category, protocol, asset, symbol, amount,
               CASE WHEN q IS NULL THEN u END AS usd, q IS NULL AND u IS NOT NULL AS priced,
               coalesce(q, psrc) AS price_source, ui_multiplier, pos_key, method, receipt_token, owner_method,
               CASE WHEN q IS NOT NULL THEN q
                    WHEN reserve_unbacked THEN 'reserve_unbacked'
                    WHEN decoded_over THEN 'decoded_exceeds_onchain'
                    WHEN coalesce(morpho_runaway, false) THEN 'morpho_interest_runaway'
                    WHEN u IS NULL AND category IN ('collateral', 'supplied', 'lp') THEN category || '_unpriced'
                    WHEN category = 'lp' AND owner_method = 'inferred_last_sender' THEN 'lp_owner_inferred'
                    WHEN method = 'event_principal' AND abs(u) >= 1000 THEN 'unverified_principal' END AS quality
        FROM (SELECT x.*, CASE WHEN psrc = 'dex_vwap' AND abs(u) > 5 * coalesce(vol_30d_usd, 0) AND NOT pegged THEN 'dex_vwap_exceeds_liquidity'
                               WHEN category = 'lp' AND abs(u) > 1e8 THEN 'lp_value_implausible'
                               WHEN abs(u) > 1e10 THEN 'value_implausible' END AS q FROM x)""")
    # vault wallets: ERC-4626 issuers hold depositors' assets (gross shown, net worth pass-through)
    con.execute("""CREATE OR REPLACE TABLE vault_wallets AS SELECT DISTINCT vault AS wallet FROM d.meta_vault
                   UNION SELECT DISTINCT vault FROM d.dim_vaults""")
    # venue contracts hold their users' money too (robinhood: the Morpho Blue singleton's 131.9M syrupUSDG collateral, the Uniswap V4
    # PoolManager's 88.4M SUSD): gross shown, net worth pass-through, like vaults. Their tokens are already in the depositors' rows.
    con.execute("""CREATE OR REPLACE TABLE venue_wallets AS
        SELECT DISTINCT lower(morpho) AS wallet FROM d.dim_morpho_markets UNION SELECT lower(comet) FROM d.dim_comets
        UNION SELECT lower(token) FROM d.dim_aave_tokens WHERE kind = 'atoken' UNION SELECT lower(ctoken) FROM d.dim_ctokens
        UNION SELECT lower(silo) FROM d.dim_silos UNION SELECT lower(manager) FROM d.dim_v4_pools WHERE manager IS NOT NULL
        UNION SELECT lower(pool) FROM d.dim_pools""")
    con.execute("""CREATE OR REPLACE TABLE balance_sheet_summary_v2 AS
        WITH s AS (
          SELECT chain, wallet,
            sum(CASE WHEN category = 'liquid' THEN usd END) AS liquid_usd,
            sum(CASE WHEN category IN ('supplied', 'collateral') THEN usd END) AS supplied_usd,
            sum(CASE WHEN category = 'debt' THEN usd END) AS debt_usd,
            sum(CASE WHEN category = 'lp' THEN usd END) AS lp_usd,
            sum(CASE WHEN category = 'perp_collateral' THEN usd END) AS perp_collateral_usd,
            sum(CASE WHEN category = 'perp_size' THEN usd END) AS perp_notional_usd,
            coalesce(sum(CASE WHEN category IN ('liquid', 'supplied', 'collateral', 'lp', 'perp_collateral') THEN usd END), 0)
              - coalesce(sum(CASE WHEN category = 'debt' THEN usd END), 0) AS gross_net_usd,
            coalesce(sum(CASE WHEN category IN ('liquid', 'supplied', 'collateral', 'lp', 'perp_collateral') AND NOT coalesce(quality IN ('unverified_principal', 'morpho_interest_runaway', 'reserve_unbacked', 'decoded_exceeds_onchain'), false) THEN usd END), 0)
              - coalesce(sum(CASE WHEN category = 'debt' AND NOT coalesce(quality IN ('unverified_principal', 'morpho_interest_runaway', 'reserve_unbacked', 'decoded_exceeds_onchain'), false) THEN usd END), 0) AS verified_net_usd,
            count(*) FILTER (WHERE NOT priced) AS unpriced_rows, count(*) AS rows,
            list(DISTINCT quality) FILTER (WHERE quality IS NOT NULL) AS row_flags,
            sum(CASE WHEN category = 'debt' AND NOT priced THEN 1 ELSE 0 END) AS unpriced_debt_rows,
            sum(CASE WHEN category IN ('collateral', 'supplied') AND NOT priced THEN 1 ELSE 0 END) AS unpriced_coll_rows
          FROM balance_sheets_v2 GROUP BY ALL)
        SELECT s.* EXCLUDE (gross_net_usd, verified_net_usd, row_flags),
               CASE WHEN v.wallet IS NOT NULL OR vw.wallet IS NOT NULL THEN 0 ELSE s.verified_net_usd END AS net_worth_usd, s.gross_net_usd,
               list_distinct(list_concat(coalesce(s.row_flags, []),
                 CASE WHEN v.wallet IS NOT NULL THEN ['vault_pass_through'] ELSE [] END,
                 CASE WHEN vw.wallet IS NOT NULL AND v.wallet IS NULL THEN ['venue_pass_through'] ELSE [] END,
                 CASE WHEN coalesce(s.debt_usd, 0) > 1000 AND coalesce(s.debt_usd, 0) > coalesce(s.supplied_usd, 0) + coalesce(s.liquid_usd, 0) + coalesce(s.lp_usd, 0) + coalesce(s.perp_collateral_usd, 0)
                      THEN ['debt_exceeds_assets'] ELSE [] END,
                 CASE WHEN s.unpriced_coll_rows > 0 AND coalesce(s.debt_usd, 0) > 0 THEN ['collateral_unpriced'] ELSE [] END)) AS flags
        FROM s LEFT JOIN vault_wallets v ON v.wallet = s.wallet LEFT JOIN venue_wallets vw ON vw.wallet = s.wallet""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chain")
    ap.add_argument("--full-history", action="store_true",
                    help="post-pass over a full-history v1 econ run (the 6-VM layout): no snapshot or delta; needs --econ-dir, --map-dir, "
                         "--extraction (raw token_transfers + blocks), --chain-id, --rpc and --out")
    ap.add_argument("--econ-dir", default=None)
    ap.add_argument("--map-dir", default=None)
    ap.add_argument("--extraction", default=None)
    ap.add_argument("--chain-id", type=int, default=None)
    ap.add_argument("--as-of-block", type=int, required=True)
    ap.add_argument("--rpc", default=None)
    ap.add_argument("--skip-rpc", action="store_true")
    ap.add_argument("--stages", default="lp,morpho,prices,lending,comet,sheets")
    ap.add_argument("--prices-head", default=None, help="head prices CSV instead of <v3>/prices_head.csv (dev runs)")
    ap.add_argument("--backing-rpc", action="store_true", help="read reserve backing on chain even with --skip-rpc (dev runs)")
    ap.add_argument("--temp-max", default=os.environ.get("EM_DUCK_TEMP_MAX", "60GB"), help="DuckDB max_temp_directory_size")
    ap.add_argument("--out", default=None, help="write outputs, caches and the work DuckDB here instead of em-data/econ/<chain>/v3 (dev runs)")
    a = ap.parse_args()
    global PRICES_HEAD
    PRICES_HEAD = a.prices_head
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import CHAINS, bind_inc_views, duck_tmp
    import rpc
    full = a.full_history
    if full:
        need = [k for k in ("econ_dir", "map_dir", "extraction", "chain_id", "rpc", "out") if not getattr(a, k)]
        if need:
            raise SystemExit("--full-history needs " + ", ".join("--" + k.replace("_", "-") for k in need))
        if duckdb.__version__.startswith("1.5"):
            raise SystemExit("duckdb %s drops BLOB-equality Parquet filters (empty decoder views): use 1.4.x" % duckdb.__version__)
        CHAIN_ID[a.chain] = a.chain_id
    elif a.chain not in CHAINS:
        raise SystemExit("unknown chain for the skew refresh layout: %s (use --full-history on a VM)" % a.chain)
    rpc.configure(a.rpc.split(",") if a.rpc else CHAINS[a.chain]["rpcs"])
    E2 = None if full else f"{ROOT}/econ/{a.chain}/v2"
    E = OUT = a.econ_dir if full else f"{ROOT}/econ/{a.chain}/v3"
    if a.out:
        OUT = a.out
        os.makedirs(OUT, exist_ok=True)
    MAPD = a.map_dir if full else f"{ROOT}/map/{a.chain}"
    tmpdir = f"{OUT}/tmp" if full else duck_tmp()
    os.makedirs(tmpdir if full else f"{TMP}/duck", exist_ok=True)
    fdb = f"{OUT}/fix3_{a.chain}.duckdb" if a.out else f"{TMP}/fix3_{a.chain}.duckdb"
    for f in (fdb, fdb + ".wal"):
        if os.path.exists(f):
            os.remove(f)
    con = duckdb.connect(fdb)
    con.execute(f"SET threads={os.environ.get('FIX_THREADS', '3')}; SET memory_limit='{os.environ.get('FIX_MEM', '8GB')}'; "
                f"SET temp_directory='{tmpdir}'; SET preserve_insertion_order=false;"
                f"SET max_temp_directory_size='{a.temp_max}'")
    con.execute(MACROS)
    con.execute(f"ATTACH '{E}/econ.duckdb' AS d (READ_ONLY)")
    if full:      # the whole history is the "increment": ERC721 owners and block times straight from the VM's extraction
        snap = 0
        X = a.extraction
        con.execute(f"""CREATE OR REPLACE VIEW inc_token_transfers AS SELECT * FROM read_parquet('{X}/token_transfers/**/*.parquet', union_by_name=true)
                        WHERE block_number <= {a.as_of_block}""")
        con.execute(f"""CREATE OR REPLACE VIEW inc_blocks AS SELECT * FROM read_parquet('{X}/blocks/**/*.parquet', union_by_name=true)
                        WHERE number <= {a.as_of_block}""")
    else:
        snap = CHAINS[a.chain]["snap_end"]
        bind_inc_views(con, a.chain, snap + 1, a.as_of_block, prefix="inc_")
    TT = "inc_token_transfers"
    con.execute(f"CREATE OR REPLACE TABLE tm AS SELECT token, symbol, decimals FROM read_parquet('{E}/token_meta.parquet')")
    st = a.stages.split(",")
    if "lp" in st:
        stage_lp(con, E, TT, E2=E2, snap=snap)
    if "morpho" in st:
        stage_morpho(con, E)
    if "prices" in st:
        stage_prices(con, E, a.chain, a.skip_rpc)
    if "lending" in st:
        stage_lending(con, E2)
        stage_backing(con, E, OUT, a.skip_rpc and not a.backing_rpc)
    if "comet" in st:
        stage_comet(con, E, MAPD, cache=OUT)
    if "sheets" in st:
        stage_sheets(con, E, a.chain)
        for t, f in (("balance_sheets_v2", "sheet_rows.v3.parquet"), ("balance_sheet_summary_v2", "sheet_summary.v3.parquet"),
                     ("lp_v2", "lp_positions_valued.v3.parquet")):
            con.execute(f"COPY {t} TO '{OUT}/{f}.tmp' (FORMAT parquet, COMPRESSION zstd)")
            os.replace(f"{OUT}/{f}.tmp", f"{OUT}/{f}")
        for q, f in (("receipt_token", "receipt_tokens.v3.csv"), ("ph2", "prices_head.v3.csv"),
                     ("(SELECT * FROM px_extra)", "prices_oracle_vault.v3.csv"), ("morpho_coll_px", "morpho_coll_px.v3.csv")):
            con.execute(f"COPY {q} TO '{OUT}/{f}.tmp' (FORMAT csv, HEADER)")
            os.replace(f"{OUT}/{f}.tmp", f"{OUT}/{f}")
        t = con.execute(f"SELECT max(timestamp) FROM inc_blocks WHERE number = {a.as_of_block}").fetchone()[0]
        json.dump({"chain": a.chain, "as_of_block": a.as_of_block, "as_of_time": int(t) if t else None,
                   "as_of_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(t))) if t else None,
                   "snapshot_block": snap, "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                  open(f"{OUT}/as_of.json.tmp", "w"), indent=1)
        os.replace(f"{OUT}/as_of.json.tmp", f"{OUT}/as_of.json")
    con.close()
    for f in (fdb, fdb + ".wal"):
        if os.path.exists(f):
            os.remove(f)
    log("done")


if __name__ == "__main__":
    main()
