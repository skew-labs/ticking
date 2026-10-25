-- re-mark liquid balances to the current prices_head (reuses liquid_balances from 60_sheets_a)
CREATE OR REPLACE TABLE liquid_usd AS
SELECT l.wallet, l.token, l.balance_raw, tm.symbol, tm.decimals,
       l.balance_raw::DOUBLE / pow(10, coalesce(tm.decimals, 18)) AS amount,
       CASE WHEN ph.source = 'dex_vwap' AND l.balance_raw::DOUBLE / pow(10, coalesce(tm.decimals, 18)) * ph.price_usd > 5 * ph.vol_30d_usd THEN NULL
            ELSE l.balance_raw::DOUBLE / pow(10, coalesce(tm.decimals, 18)) * ph.price_usd END AS usd,
       CASE WHEN dp.pool IS NOT NULL THEN 'lp' WHEN pt.token IS NOT NULL THEN 'position_token' ELSE 'liquid' END AS category
FROM liquid_balances l LEFT JOIN token_meta tm ON tm.token = l.token LEFT JOIN prices_head ph ON ph.token = l.token
LEFT JOIN (SELECT pool FROM dim_pools WHERE kind = 'v2') dp ON dp.pool = l.token LEFT JOIN position_tokens pt ON pt.token = l.token;

