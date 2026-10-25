-- Stage 5: event-sourced positions.
CREATE OR REPLACE VIEW econ_actions AS SELECT * FROM read_parquet('${OUT}/econ_actions/*.parquet');

-- ---------- 5a. generic stream: (wallet, protocol, asset, kind, key) with deltas and absolute states ----------
CREATE OR REPLACE TABLE pos_stream AS
SELECT chain, wallet, protocol, asset, pos_kind AS kind, coalesce(pos_key, '') AS pos_key, block_number, block_time, log_index, sub,
       action, tx_hash, family,
       CASE WHEN pos_kind = 'lp' THEN pos_liq::DOUBLE ELSE pos_sign * amount_raw::DOUBLE END AS delta,
       pos_abs::DOUBLE AS abs_v, pos_shares::DOUBLE AS shares_delta
FROM econ_actions WHERE pos_kind IS NOT NULL AND wallet IS NOT NULL;

CREATE OR REPLACE TABLE pos_running AS
WITH o AS (
  SELECT *, count(abs_v) OVER w AS grp FROM pos_stream
  WINDOW w AS (PARTITION BY chain, wallet, protocol, asset, kind, pos_key ORDER BY block_number, log_index, sub ROWS UNBOUNDED PRECEDING)
)
SELECT *, coalesce(max(abs_v) OVER g, 0) + sum(CASE WHEN abs_v IS NULL THEN delta ELSE 0 END) OVER (g ROWS UNBOUNDED PRECEDING) AS value_after
FROM o WINDOW g AS (PARTITION BY chain, wallet, protocol, asset, kind, pos_key, grp ORDER BY block_number, log_index, sub);

-- Comet base leg: signed principal; supply while negative = repay, withdraw into negative = borrow
CREATE OR REPLACE TABLE comet_relabel AS
SELECT tx_hash, log_index, sub, wallet, action AS action_raw,
       CASE WHEN action = 'supply' AND value_after - delta < 0 THEN 'repay'
            WHEN action = 'withdraw' AND value_after < 0 THEN 'borrow' ELSE action END AS action
FROM pos_running WHERE kind = 'comet_base';

-- ---------- 5b. Aave v3 exact balances from scaled aToken / debtToken events ----------
CREATE OR REPLACE TABLE aave_scaled_stream AS
WITH t AS (SELECT * FROM dim_aave_tokens)
SELECT e.contract AS token, e.onBehalfOf AS wallet, e.block_number, e.log_index,
       (e.value::DOUBLE - e.balanceIncrease::DOUBLE) * 1e27 / e."index"::DOUBLE AS scaled_delta, e."index"::DOUBLE AS idx
FROM ev_aave_v3_token__Mint e JOIN t ON t.token = e.contract
UNION ALL
SELECT e.contract, e."from", e.block_number, e.log_index, -(e.value::DOUBLE + e.balanceIncrease::DOUBLE) * 1e27 / e."index"::DOUBLE, e."index"::DOUBLE
FROM ev_aave_v3_token__Burn e JOIN t ON t.token = e.contract
UNION ALL
SELECT e.contract, e."from", e.block_number, e.log_index, -e.value::DOUBLE, e."index"::DOUBLE FROM ev_aave_v3_token__BalanceTransfer e JOIN t ON t.token = e.contract
UNION ALL
SELECT e.contract, e."to", e.block_number, e.log_index, e.value::DOUBLE, e."index"::DOUBLE FROM ev_aave_v3_token__BalanceTransfer e JOIN t ON t.token = e.contract;

CREATE OR REPLACE TABLE aave_index_head AS
SELECT contract AS pool, reserve, arg_max(liquidityIndex::DOUBLE, block_number * 100000 + log_index) AS liq_index,
       arg_max(variableBorrowIndex::DOUBLE, block_number * 100000 + log_index) AS var_index, max(block_number) AS block_number
FROM ev_aave_v3__ReserveDataUpdated GROUP BY ALL;

CREATE OR REPLACE TABLE aave_positions AS
WITH agg AS (
  SELECT s.wallet, s.token, sum(s.scaled_delta) AS scaled, max(s.block_number) AS last_block,
         arg_max(s.idx, s.block_number * 100000 + s.log_index) AS last_idx
  FROM aave_scaled_stream s WHERE s.wallet IS NOT NULL GROUP BY 1, 2
)
SELECT a.wallet, t.underlying AS asset, t.kind, a.token, coalesce(ap.protocol, 'aave_v3_like') AS protocol, a.scaled, a.last_block,
       a.scaled * coalesce(CASE t.kind WHEN 'atoken' THEN ix.liq_index ELSE ix.var_index END, a.last_idx) / 1e27 AS balance_raw
FROM agg a JOIN dim_aave_tokens t ON t.token = a.token
LEFT JOIN aave_index_head ix ON ix.reserve = t.underlying AND ix.pool = coalesce(t.pool, ix.pool)
LEFT JOIN dim_aave_pools ap ON ap.pool = coalesce(t.pool, ix.pool)
QUALIFY row_number() OVER (PARTITION BY a.wallet, a.token ORDER BY (ix.pool = t.pool) DESC NULLS LAST, ix.block_number DESC NULLS LAST) = 1;

-- collateral flag per (wallet, reserve): last of Enabled/Disabled
CREATE OR REPLACE TABLE aave_collateral_flag AS
SELECT wallet, reserve, arg_max(enabled, ord) AS enabled FROM (
  SELECT "user" AS wallet, reserve, true AS enabled, block_number * 100000 + log_index AS ord FROM ev_aave_v3__ReserveUsedAsCollateralEnabled
  UNION ALL SELECT "user", reserve, false, block_number * 100000 + log_index FROM ev_aave_v3__ReserveUsedAsCollateralDisabled) GROUP BY ALL;

-- ---------- 5c. Morpho Blue: shares -> assets using event-reconstructed market totals ----------
CREATE OR REPLACE TABLE morpho_totals AS
WITH ev AS (
  SELECT id, assets::DOUBLE sa, shares::DOUBLE ss, 0.0 ba, 0.0 bs FROM ev_morpho_blue__Supply
  UNION ALL SELECT id, -assets::DOUBLE, -shares::DOUBLE, 0, 0 FROM ev_morpho_blue__Withdraw
  UNION ALL SELECT id, 0, 0, assets::DOUBLE, shares::DOUBLE FROM ev_morpho_blue__Borrow
  UNION ALL SELECT id, 0, 0, -assets::DOUBLE, -shares::DOUBLE FROM ev_morpho_blue__Repay
  UNION ALL SELECT id, -badDebtAssets::DOUBLE, 0, -(repaidAssets::DOUBLE + badDebtAssets::DOUBLE), -(repaidShares::DOUBLE + badDebtShares::DOUBLE) FROM ev_morpho_blue__Liquidate
  UNION ALL SELECT id, interest::DOUBLE, feeShares::DOUBLE, interest::DOUBLE, 0 FROM ev_morpho_blue__AccrueInterest
)
SELECT id, sum(sa) total_supply_assets, sum(ss) total_supply_shares, sum(ba) total_borrow_assets, sum(bs) total_borrow_shares FROM ev GROUP BY id;

-- ---------- 5c2. Aave v4: share -> amount (hub supply rate from Add/Remove, drawnIndex from UpdateAsset); collateral flags ----------
CREATE TABLE IF NOT EXISTS aave_v4_reserves (spoke VARCHAR, reserve_id HUGEINT, asset_id HUGEINT, hub VARCHAR, block_number UBIGINT);
CREATE OR REPLACE TABLE aave_v4_rates AS
WITH sp AS (
  SELECT contract AS hub, assetId AS asset_id, arg_max(amount::DOUBLE / shares::DOUBLE, block_number * 100000 + log_index) AS supply_rate
  FROM (SELECT contract, assetId, shares, amount, block_number, log_index FROM ev_aave_v4_hub__Add
        UNION ALL SELECT contract, assetId, shares, amount, block_number, log_index FROM ev_aave_v4_hub__Remove) WHERE shares > 0 GROUP BY 1, 2
), di AS (
  SELECT contract AS hub, assetId AS asset_id, arg_max(drawnIndex::DOUBLE, block_number * 100000 + log_index) / 1e27 AS drawn_index
  FROM ev_aave_v4_hub__UpdateAsset GROUP BY 1, 2
)
SELECT r.spoke || ':' || CAST(r.reserve_id AS VARCHAR) AS pos_key, r.hub, r.asset_id, sp.supply_rate, di.drawn_index
FROM aave_v4_reserves r LEFT JOIN sp ON sp.hub = r.hub AND sp.asset_id = r.asset_id LEFT JOIN di ON di.hub = r.hub AND di.asset_id = r.asset_id;
CREATE OR REPLACE TABLE aave_v4_coll AS
SELECT contract || ':' || CAST(reserveId AS VARCHAR) AS pos_key, "user" AS wallet, arg_max(usingAsCollateral, block_number * 100000 + log_index) AS enabled
FROM ev_aave_v4__SetUsingAsCollateral GROUP BY 1, 2;

-- ---------- 5d. GMX v1 absolute position state by key ----------
CREATE OR REPLACE TABLE gmx1_state AS
WITH st AS (
  SELECT key, size::DOUBLE size_usd30, collateral::DOUBLE coll_usd30, block_number, log_index FROM ev_gmx_v1__UpdatePosition
  UNION ALL SELECT key, 0, 0, block_number, log_index FROM ev_gmx_v1__ClosePosition
  UNION ALL SELECT key, 0, 0, block_number, log_index FROM ev_gmx_v1__LiquidatePosition
), acct AS (SELECT key, any_value(account) account, any_value(indexToken) index_token, any_value(isLong) is_long, any_value(collateralToken) collateral_token
            FROM ev_gmx_v1__IncreasePosition GROUP BY key)
SELECT a.account AS wallet, a.index_token, a.collateral_token, a.is_long, s.key,
       arg_max(s.size_usd30, s.block_number * 100000 + s.log_index) AS size_usd30,
       arg_max(s.coll_usd30, s.block_number * 100000 + s.log_index) AS coll_usd30, max(s.block_number) last_block
FROM st s JOIN acct a USING (key) GROUP BY ALL;

-- ---------- 5e. current pool prices for LP valuation (last sqrtP per pool / v4 id) ----------
CREATE OR REPLACE TABLE pool_sqrtp_head AS
SELECT CASE WHEN kind = 'v4' THEN a2s(v4_id) ELSE a2s(emitter) END AS pool, a2s(emitter) AS emitter,
       arg_max(sqrtp, block_number * 100000 + log_index) / 79228162514264337593543950336.0 AS sp
FROM read_parquet('${OUT}/stage/swaps/*/*.parquet', hive_partitioning=true) WHERE kind <> 'v2' AND sqrtp > 0 GROUP BY ALL;

-- ---------- 5f. positions at head ----------
CREATE OR REPLACE TABLE positions_head AS
WITH last AS (
  SELECT chain, wallet, protocol, asset, kind, pos_key, arg_max(value_after, block_number * 1000000 + coalesce(log_index, 0) * 4 + sub) AS value,
         max(block_number) AS last_block, count(*) AS n_events, sum(coalesce(shares_delta, 0)) AS shares
  FROM pos_running GROUP BY ALL
), gen AS (
  SELECT chain, wallet, protocol, asset,
         CASE WHEN kind = 'comet_base' THEN CASE WHEN value >= 0 THEN 'supplied' ELSE 'debt' END ELSE kind END AS kind,
         pos_key, CASE WHEN kind = 'comet_base' THEN abs(value) ELSE value END AS amount, shares, last_block, n_events,
         CASE WHEN kind = 'lp' THEN 'liquidity' WHEN kind LIKE 'perp_size' THEN 'usd30_or_raw' ELSE 'token_raw' END AS unit,
         CASE WHEN kind IN ('perp_size', 'perp_collateral') AND protocol LIKE 'gmx_v2%' THEN 'abs_state'
              WHEN protocol IN (SELECT DISTINCT protocol FROM dim_morpho_markets) THEN 'morpho_shares'
              WHEN protocol LIKE 'aave_v4%' THEN 'aave_v4_shares_ex_premium'
              ELSE 'event_principal' END AS method
  FROM last
)
SELECT g.chain, g.wallet, g.protocol, g.asset,
       CASE WHEN g.method = 'aave_v4_shares_ex_premium' AND g.kind = 'supplied' AND coalesce(v4c.enabled, false) THEN 'collateral' ELSE g.kind END AS kind, g.pos_key,
       CASE WHEN g.method = 'aave_v4_shares_ex_premium' AND g.kind = 'supplied' THEN g.shares * v4r.supply_rate
            WHEN g.method = 'aave_v4_shares_ex_premium' AND g.kind = 'debt' THEN g.shares * v4r.drawn_index
            WHEN g.method = 'morpho_shares' AND g.kind = 'supplied' AND mt.total_supply_shares > 0 THEN g.shares * mt.total_supply_assets / mt.total_supply_shares
            WHEN g.method = 'morpho_shares' AND g.kind = 'debt' AND mt.total_borrow_shares > 0 THEN g.shares * mt.total_borrow_assets / mt.total_borrow_shares
            ELSE g.amount END AS amount,
       g.amount AS amount_principal, g.unit, g.method, g.last_block, g.n_events
FROM gen g LEFT JOIN morpho_totals mt ON g.method = 'morpho_shares' AND mt.id = g.pos_key
LEFT JOIN aave_v4_rates v4r ON g.method = 'aave_v4_shares_ex_premium' AND v4r.pos_key = g.pos_key
LEFT JOIN aave_v4_coll v4c ON g.method = 'aave_v4_shares_ex_premium' AND v4c.pos_key = g.pos_key AND v4c.wallet = g.wallet
WHERE g.protocol NOT LIKE 'aave_v3%' OR g.protocol IS NULL      -- Aave v3 replaced by exact scaled balances below
UNION ALL BY NAME
SELECT ${CHAIN} AS chain, a.wallet, a.protocol, a.asset,
       CASE WHEN a.kind = 'vdebt' THEN 'debt' WHEN coalesce(f.enabled, true) THEN 'collateral' ELSE 'supplied' END AS kind,
       a.token AS pos_key, a.balance_raw AS amount, NULL AS amount_principal, 'token_raw' AS unit, 'aave_scaled' AS method, a.last_block, NULL AS n_events
FROM aave_positions a LEFT JOIN aave_collateral_flag f ON f.wallet = a.wallet AND f.reserve = a.asset
WHERE a.protocol LIKE 'aave_v3%' AND abs(a.balance_raw) > 0.5
UNION ALL BY NAME
SELECT ${CHAIN} AS chain, wallet, 'gmx_v1' AS protocol, index_token AS asset, k.kind, key AS pos_key,
       CASE k.kind WHEN 'perp_size' THEN size_usd30 ELSE coll_usd30 END AS amount, 'usd30' AS unit, 'abs_state' AS method, last_block
FROM gmx1_state CROSS JOIN (VALUES ('perp_size'), ('perp_collateral')) k(kind) WHERE size_usd30 > 0;

-- LP liquidity -> token amounts (Uniswap v3/v4 tick math, double precision)
CREATE OR REPLACE TABLE lp_positions_valued AS
WITH lp AS (
  SELECT p.*, t.pool AS npm_pool, t.tick_lower AS npm_tl, t.tick_upper AS npm_tu, t.token0 AS npm_t0, t.token1 AS npm_t1,
         split_part(p.pos_key, ':', 1) AS k1, split_part(p.pos_key, ':', 2) AS k2
  FROM positions_head p
  LEFT JOIN dim_npm_tokens t ON p.pos_key = t.npm || ':' || t.token_id::VARCHAR
  WHERE p.kind = 'lp' AND p.amount > 0
), g AS (
  SELECT lp.*,
    CASE WHEN k1 = 'v4' THEN k2 WHEN npm_pool IS NOT NULL THEN npm_pool ELSE k1 END AS pool_ref,
    CASE WHEN k1 = 'v4' THEN TRY_CAST(split_part(pos_key, ':', 5) AS INTEGER) WHEN npm_pool IS NOT NULL THEN npm_tl ELSE TRY_CAST(split_part(pos_key, ':', 3) AS INTEGER) END AS tl,
    CASE WHEN k1 = 'v4' THEN TRY_CAST(split_part(pos_key, ':', 6) AS INTEGER) WHEN npm_pool IS NOT NULL THEN npm_tu ELSE TRY_CAST(split_part(pos_key, ':', 4) AS INTEGER) END AS tu
  FROM lp
)
SELECT g.chain, g.wallet, g.protocol, g.pos_key, g.pool_ref, g.amount AS liquidity, g.tl, g.tu, s.sp,
       coalesce(g.npm_t0, dp.token0, v4.token0) AS token0, coalesce(g.npm_t1, dp.token1, v4.token1) AS token1,
       CASE WHEN s.sp IS NULL OR g.tl IS NULL THEN NULL
            WHEN s.sp <= pow(1.0001, g.tl / 2.0) THEN g.amount * (1 / pow(1.0001, g.tl / 2.0) - 1 / pow(1.0001, g.tu / 2.0))
            WHEN s.sp >= pow(1.0001, g.tu / 2.0) THEN 0
            ELSE g.amount * (1 / s.sp - 1 / pow(1.0001, g.tu / 2.0)) END AS amount0_raw,
       CASE WHEN s.sp IS NULL OR g.tl IS NULL THEN NULL
            WHEN s.sp <= pow(1.0001, g.tl / 2.0) THEN 0
            WHEN s.sp >= pow(1.0001, g.tu / 2.0) THEN g.amount * (pow(1.0001, g.tu / 2.0) - pow(1.0001, g.tl / 2.0))
            ELSE g.amount * (s.sp - pow(1.0001, g.tl / 2.0)) END AS amount1_raw
FROM g LEFT JOIN pool_sqrtp_head s ON s.pool = g.pool_ref
LEFT JOIN dim_pools dp ON dp.pool = g.pool_ref
LEFT JOIN (SELECT pool_id, any_value(token0) token0, any_value(token1) token1 FROM dim_v4_pools GROUP BY 1) v4 ON v4.pool_id = g.pool_ref;

-- ---------- 5g. monthly as-of snapshots (sparse: one row per position-month with activity; value = end-of-month state) ----------
CREATE OR REPLACE TABLE positions_monthly AS
SELECT chain, date_trunc('month', block_time) AS month, wallet, protocol, asset,
       CASE WHEN kind = 'comet_base' THEN 'comet_base_signed' ELSE kind END AS kind, count(DISTINCT pos_key) AS n_keys,
       sum(month_end_value) AS amount
FROM (SELECT chain, wallet, protocol, asset, kind, pos_key, block_time,
             arg_max(value_after, block_number * 1000000 + coalesce(log_index, 0) * 4 + sub) OVER (PARTITION BY chain, wallet, protocol, asset, kind, pos_key, date_trunc('month', block_time)) AS month_end_value,
             row_number() OVER (PARTITION BY chain, wallet, protocol, asset, kind, pos_key, date_trunc('month', block_time) ORDER BY block_number DESC, log_index DESC) AS rn
      FROM pos_running WHERE block_time IS NOT NULL)
WHERE rn = 1 GROUP BY ALL;

-- ---------- 5h. sanity checks ----------
CREATE OR REPLACE TABLE positions_checks AS
SELECT 'debt_negative_beyond_rounding' AS check_name, protocol, any_value(method) AS method, count(*) AS n_bad,
       (SELECT count(*) FROM positions_head x WHERE x.kind = 'debt' AND x.protocol = p.protocol) AS n_total, min(amount) AS worst
FROM positions_head p WHERE kind = 'debt' AND amount < -1000 GROUP BY protocol
UNION ALL
SELECT 'supplied_negative_beyond_rounding', protocol, any_value(method), count(*), (SELECT count(*) FROM positions_head x WHERE x.kind IN ('supplied','collateral') AND x.protocol = p.protocol), min(amount)
FROM positions_head p WHERE kind IN ('supplied', 'collateral') AND amount < -1000 GROUP BY protocol;

-- Aave cross-check: scaled*index vs aToken Transfer-derived net vs Pool-event principal (top 25 wallets by aToken balance)
CREATE OR REPLACE TABLE aave_crosscheck AS
WITH top AS (SELECT * FROM aave_positions WHERE kind = 'atoken' ORDER BY balance_raw DESC LIMIT 25),
tr AS (
  SELECT a2s(t.token) token, w.wallet, sum(CASE WHEN a2s(t."to") = w.wallet THEN blob_dbl(t.amount) ELSE 0 END) - sum(CASE WHEN a2s(t."from") = w.wallet THEN blob_dbl(t.amount) ELSE 0 END) AS net
  FROM token_transfers t JOIN top w ON t.token = unhex(substr(w.token, 3)) AND (t."to" = unhex(substr(w.wallet, 3)) OR t."from" = unhex(substr(w.wallet, 3)))
  GROUP BY ALL
),
ev AS (SELECT wallet, asset, sum(pos_sign * amount_raw::DOUBLE) principal FROM econ_actions WHERE protocol LIKE 'aave_v3%' AND pos_kind = 'supplied' GROUP BY ALL)
SELECT top.wallet, top.asset, top.token, top.balance_raw AS scaled_x_index, tr.net AS transfer_net, ev.principal AS pool_event_principal,
       top.balance_raw / nullif(tr.net, 0) - 1 AS rel_diff_vs_transfers
FROM top LEFT JOIN tr ON tr.token = top.token AND tr.wallet = top.wallet LEFT JOIN ev ON ev.wallet = top.wallet AND ev.asset = top.asset;

COPY positions_head TO '${OUT}/positions_head.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY positions_monthly TO '${OUT}/positions_monthly.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY lp_positions_valued TO '${OUT}/lp_positions_valued.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY positions_checks TO '${OUT}/positions_checks.csv' (HEADER);
COPY aave_crosscheck TO '${OUT}/aave_crosscheck.csv' (HEADER);
