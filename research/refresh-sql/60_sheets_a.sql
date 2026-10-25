-- Stage 7a: balance-sheet wallet universe + liquid ERC20 balances (net of token_transfers).
-- Liquid balances are computed (1) for ALL holders of focus tokens (Robinhood stock tokens, stablecoins, WETH) and
-- (2) for the balance-sheet wallet universe across all tokens. Amounts >= 2^100 (spam) are excluded so sums cannot overflow.
CREATE OR REPLACE VIEW econ_actions AS SELECT * FROM read_parquet('${OUT}/econ_actions/*.parquet');
CREATE OR REPLACE MACRO blob_u100(b) AS CASE WHEN substr(hex(b), 1, 39) = '000000000000000000000000000000000000000' THEN w_uint(hex(b)) END;

CREATE OR REPLACE TABLE focus_tokens AS
SELECT token, kind FROM issuer_tokens UNION SELECT token, 'weth' FROM dim_weth
UNION SELECT token, 'stable_anchor' FROM stable_anchor;

CREATE OR REPLACE TABLE focus_balances AS
WITH ft AS (SELECT unhex(substr(token, 3)) AS tb FROM focus_tokens),
f AS (
  SELECT token, "to" AS w, blob_u100(amount) AS a FROM token_transfers SEMI JOIN ft ON ft.tb = token_transfers.token WHERE standard = 'ERC20'
  UNION ALL
  SELECT token, "from", -blob_u100(amount) FROM token_transfers SEMI JOIN ft ON ft.tb = token_transfers.token WHERE standard = 'ERC20'
)
SELECT a2s(w) AS wallet, a2s(token) AS token, sum(a) AS balance_raw, count(*) AS n_transfers
FROM f WHERE w <> unhex('0000000000000000000000000000000000000000') AND a IS NOT NULL GROUP BY ALL HAVING sum(a) > 0;

-- wallet universe: open protocol positions, LPs, most active econ wallets, top holders of focus tokens
CREATE OR REPLACE TABLE sheet_wallets AS
SELECT wallet, max(why) AS why FROM (
  SELECT wallet, 'position' AS why FROM positions_head WHERE abs(amount) > 0 AND kind <> 'lp'
  UNION ALL SELECT wallet, 'lp' FROM (SELECT wallet, sum(liquidity) l FROM lp_positions_valued GROUP BY 1 ORDER BY l DESC LIMIT 20000)
  UNION ALL SELECT wallet, 'active' FROM (SELECT wallet, count(*) c FROM econ_actions WHERE action NOT IN ('swap', 'corporate_action')
                                          GROUP BY 1 ORDER BY c DESC LIMIT 3000)
  UNION ALL SELECT wallet, 'focus_holder' FROM (SELECT wallet, row_number() OVER (PARTITION BY token ORDER BY balance_raw DESC) rk FROM focus_balances) WHERE rk <= 50
) WHERE wallet IS NOT NULL GROUP BY wallet;

CREATE OR REPLACE TABLE liquid_balances AS
WITH sw AS (SELECT unhex(substr(wallet, 3)) AS wb FROM sheet_wallets),
f AS (
  SELECT token, "to" AS w, blob_u100(amount) AS a FROM token_transfers SEMI JOIN sw ON sw.wb = token_transfers."to" WHERE standard = 'ERC20'
  UNION ALL
  SELECT token, "from", -blob_u100(amount) FROM token_transfers SEMI JOIN sw ON sw.wb = token_transfers."from" WHERE standard = 'ERC20'
)
SELECT a2s(w) AS wallet, a2s(token) AS token, sum(a) AS balance_raw, count(*) AS n_transfers
FROM f WHERE a IS NOT NULL GROUP BY ALL HAVING sum(a) > 0;
COPY focus_balances TO '${OUT}/focus_balances.parquet' (FORMAT parquet, COMPRESSION zstd);
COPY liquid_balances TO '${OUT}/liquid_balances_sheet_wallets.parquet' (FORMAT parquet, COMPRESSION zstd);

-- latest UI multiplier per Robinhood stock token (ERC-8056)
CREATE OR REPLACE TABLE stock_multiplier AS
SELECT asset AS token, arg_max(TRY_CAST(json_extract_string(extra_json, '$.new_multiplier_e18') AS DOUBLE) / 1e18, block_number) AS multiplier
FROM econ_actions WHERE action = 'corporate_action' AND json_extract_string(extra_json, '$.new_multiplier_e18') IS NOT NULL GROUP BY 1;

-- tokens that are claims on protocol positions (valued from positions, not as liquid): aTokens/debt tokens, comets, cTokens
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

-- v2 LP tokens -> underlying via reserves share (LP totalSupply from mint/burn flows), only pairs held by sheet wallets
CREATE OR REPLACE TABLE v2_lp_supply AS
WITH held AS (SELECT DISTINCT unhex(substr(token, 3)) tb FROM liquid_usd WHERE category = 'lp')
SELECT a2s(token) AS pair, sum(CASE WHEN "from" = unhex('0000000000000000000000000000000000000000') THEN blob_u100(amount) ELSE 0 END)
     - sum(CASE WHEN "to" = unhex('0000000000000000000000000000000000000000') THEN blob_u100(amount) ELSE 0 END) AS total_supply
FROM token_transfers SEMI JOIN held ON held.tb = token_transfers.token
WHERE standard = 'ERC20' AND ("from" = unhex('0000000000000000000000000000000000000000') OR "to" = unhex('0000000000000000000000000000000000000000'))
GROUP BY 1;
