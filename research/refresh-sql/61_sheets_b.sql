-- Stage 7b: balance_sheets (long) + per-wallet summary. Units: amount = token units (decimals applied), usd where priced.
CREATE TABLE IF NOT EXISTS meta_eth_balance (wallet VARCHAR, balance_wei HUGEINT);
CREATE OR REPLACE TABLE balance_sheets AS
WITH w AS (SELECT wallet FROM sheet_wallets),
liquid AS (
  SELECT l.wallet, 'liquid' AS category, 'wallet' AS protocol, l.token AS asset, l.symbol, l.amount,
         l.usd, sm.multiplier AS ui_multiplier, NULL AS pos_key
  FROM liquid_usd l JOIN w USING (wallet) LEFT JOIN stock_multiplier sm ON sm.token = l.token WHERE l.category = 'liquid'
  UNION ALL
  SELECT e.wallet, 'liquid', 'wallet', '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', 'ETH', e.balance_wei::DOUBLE / 1e18,
         NULL, NULL, NULL
  FROM meta_eth_balance e JOIN w USING (wallet) WHERE e.balance_wei > 0
),
v2lp AS (   -- LP token share of pair reserves -> two underlying rows
  SELECT l.wallet, 'lp' AS category, coalesce(p.protocol, 'univ2') AS protocol, CASE s.k WHEN 0 THEN p.token0 ELSE p.token1 END AS asset,
         tm.symbol, l.balance_raw::DOUBLE / nullif(ts.total_supply::DOUBLE, 0) * CASE s.k WHEN 0 THEN r.reserve0::DOUBLE ELSE r.reserve1::DOUBLE END / pow(10, coalesce(tm.decimals, 18)) AS amount,
         NULL::DOUBLE AS usd, NULL::DOUBLE AS ui_multiplier, l.token AS pos_key
  FROM liquid_usd l JOIN w USING (wallet) JOIN dim_pools p ON p.pool = l.token
  JOIN v2_lp_supply ts ON ts.pair = l.token JOIN read_parquet('${OUT}/stage/v2_reserves_head.parquet') r ON a2s(r.pair) = l.token
  CROSS JOIN (VALUES (0), (1)) s(k)
  LEFT JOIN token_meta tm ON tm.token = CASE s.k WHEN 0 THEN p.token0 ELSE p.token1 END
  WHERE l.category = 'lp'
),
v3lp AS (
  SELECT v.wallet, 'lp' AS category, v.protocol, CASE s.k WHEN 0 THEN v.token0 ELSE v.token1 END AS asset, tm.symbol,
         CASE s.k WHEN 0 THEN v.amount0_raw ELSE v.amount1_raw END / pow(10, coalesce(tm.decimals, 18)) AS amount,
         NULL::DOUBLE AS usd, NULL::DOUBLE AS ui_multiplier, v.pos_key
  FROM lp_positions_valued v JOIN w USING (wallet) CROSS JOIN (VALUES (0), (1)) s(k)
  LEFT JOIN token_meta tm ON tm.token = CASE s.k WHEN 0 THEN v.token0 ELSE v.token1 END
),
pos AS (
  SELECT p.wallet, p.kind AS category, p.protocol, p.asset, coalesce(tm.symbol, p.asset) AS symbol,
         CASE WHEN p.unit = 'usd30' OR (p.protocol LIKE 'gmx%' AND p.kind = 'perp_size') THEN p.amount / 1e30
              ELSE p.amount / pow(10, coalesce(tm.decimals, 18)) END AS amount,
         NULL::DOUBLE AS usd, NULL::DOUBLE AS ui_multiplier, p.pos_key
  FROM positions_head p JOIN w USING (wallet) LEFT JOIN token_meta tm ON tm.token = p.asset
  -- event-principal protocols (Aave-v2 forks/Radiant, Silo, Compound-v2 forks w/o abs state) carry no interest index:
  -- repay/withdraw incl. interest leaves negative residuals -> treated as closed (clamped at 0)
  WHERE p.kind NOT IN ('lp') AND (p.amount > 0 OR (p.method NOT IN ('event_principal') AND abs(p.amount) > 0))
)
SELECT ${CHAIN} AS chain, y.wallet, y.category, y.protocol, y.asset, y.symbol, y.amount,
       CASE WHEN y.implausible IS NOT NULL THEN NULL ELSE y.m_usd END AS usd,
       y.m_priced AND y.implausible IS NULL AS priced, coalesce(y.implausible, y.m_source) AS price_source, y.ui_multiplier, y.pos_key
FROM (
SELECT x.* EXCLUDE (usd), mtm.m_usd, mtm.m_priced, mtm.m_source,
       -- guards: V3/V4 LP tick-math valuation is unverified for exotic pools; never let one leg dominate a sheet
       CASE WHEN x.category = 'lp' AND abs(mtm.m_usd) > 1e8 THEN 'lp_value_implausible'
            WHEN abs(mtm.m_usd) > 1e10 THEN 'value_implausible' END AS implausible
FROM (SELECT * FROM liquid UNION ALL BY NAME SELECT * FROM v2lp UNION ALL BY NAME SELECT * FROM v3lp UNION ALL BY NAME SELECT * FROM pos) x
LEFT JOIN prices_head ph ON ph.token = x.asset
CROSS JOIN LATERAL (SELECT
       CASE WHEN ph.source = 'dex_vwap' AND abs(x.amount * ph.price_usd) > 5 * ph.vol_30d_usd THEN NULL
            ELSE coalesce(x.usd, CASE WHEN x.category = 'perp_size' THEN x.amount
                                      WHEN x.category = 'perp_collateral' AND x.protocol = 'gmx_v1' THEN x.amount
                                      ELSE x.amount * ph.price_usd END) END AS m_usd,
       (NOT coalesce(ph.source = 'dex_vwap' AND abs(x.amount * ph.price_usd) > 5 * ph.vol_30d_usd, false))
         AND (x.usd IS NOT NULL OR ph.price_usd IS NOT NULL OR x.category = 'perp_size' OR (x.category = 'perp_collateral' AND x.protocol = 'gmx_v1')) AS m_priced,
       CASE WHEN ph.source = 'dex_vwap' AND abs(x.amount * ph.price_usd) > 5 * ph.vol_30d_usd THEN 'dex_vwap_exceeds_liquidity' ELSE ph.source END AS m_source
) mtm
WHERE x.amount IS NOT NULL AND abs(x.amount) > 1e-12
) y;

CREATE OR REPLACE TABLE balance_sheet_summary AS
SELECT chain, wallet,
  sum(CASE WHEN category = 'liquid' THEN usd END) AS liquid_usd,
  sum(CASE WHEN category IN ('supplied', 'collateral') THEN usd END) AS supplied_usd,
  sum(CASE WHEN category = 'debt' THEN usd END) AS debt_usd,
  sum(CASE WHEN category = 'lp' THEN usd END) AS lp_usd,
  sum(CASE WHEN category = 'perp_collateral' THEN usd END) AS perp_collateral_usd,
  sum(CASE WHEN category = 'perp_size' THEN usd END) AS perp_notional_usd,
  coalesce(sum(CASE WHEN category IN ('liquid', 'supplied', 'collateral', 'lp', 'perp_collateral') THEN usd END), 0)
    - coalesce(sum(CASE WHEN category = 'debt' THEN usd END), 0) AS net_worth_usd,
  count(*) FILTER (WHERE NOT priced) AS unpriced_rows, count(*) AS rows
FROM balance_sheets GROUP BY ALL;

COPY balance_sheets TO '${OUT}/balance_sheets.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY balance_sheet_summary TO '${OUT}/balance_sheet_summary.parquet' (FORMAT parquet, COMPRESSION zstd);
