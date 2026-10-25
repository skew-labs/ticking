-- Stage 10: focus reports — (A) Robinhood stock tokens, (B) stablecoins, (C) Arbitrum incentive claims (DRIP via Merkl).
CREATE OR REPLACE VIEW econ_actions AS SELECT * FROM read_parquet('${OUT}/econ_actions/*.parquet');

-- ---------- (A) stock tokens ----------
CREATE OR REPLACE TABLE stock_flows AS
SELECT asset AS token, action, count(*) AS n, sum(amount_raw::DOUBLE) / 1e18 AS amount, count(DISTINCT wallet) AS wallets,
       count(DISTINCT tx_from) AS distinct_tx_senders, mode(tx_from) AS top_tx_sender
FROM econ_actions WHERE asset IN (SELECT token FROM issuer_tokens WHERE kind = 'robinhood_stock') AND action IN ('issuer_mint', 'issuer_burn', 'bridge_in', 'bridge_out')
GROUP BY ALL;

CREATE OR REPLACE TABLE stock_venues AS
WITH sw AS (
  SELECT asset AS tok, json_extract_string(extra_json, '$.token_out') AS other, amount_raw::DOUBLE AS amt_tok, protocol, json_extract_string(extra_json, '$.pool') AS pool, 'sell' AS side
  FROM econ_actions WHERE action = 'swap' AND asset IN (SELECT token FROM issuer_tokens WHERE kind = 'robinhood_stock')
  UNION ALL
  SELECT json_extract_string(extra_json, '$.token_out'), asset, TRY_CAST(json_extract_string(extra_json, '$.amount_out') AS DOUBLE), protocol, json_extract_string(extra_json, '$.pool'), 'buy'
  FROM econ_actions WHERE action = 'swap' AND json_extract_string(extra_json, '$.token_out') IN (SELECT token FROM issuer_tokens WHERE kind = 'robinhood_stock')
)
SELECT tok AS token, protocol, pool, any_value(other) AS quote_token, count(*) AS n_swaps, sum(amt_tok) / 1e18 AS volume_shares,
       count(*) FILTER (WHERE side = 'buy') AS buys, count(*) FILTER (WHERE side = 'sell') AS sells
FROM sw GROUP BY ALL;

CREATE OR REPLACE TABLE stock_token_summary AS
SELECT i.token, tm.symbol, tm.name,
  (SELECT count(*) FROM focus_balances b WHERE b.token = i.token) AS holders,
  (SELECT sum(balance_raw::DOUBLE) / 1e18 FROM focus_balances b WHERE b.token = i.token) AS supply_raw_shares,
  sm.multiplier AS ui_multiplier,
  (SELECT count(*) FROM econ_actions a WHERE a.action = 'corporate_action' AND a.asset = i.token) AS corporate_actions,
  (SELECT n FROM stock_flows f WHERE f.token = i.token AND f.action = 'issuer_mint') AS mints,
  (SELECT amount FROM stock_flows f WHERE f.token = i.token AND f.action = 'issuer_mint') AS minted_shares,
  (SELECT n FROM stock_flows f WHERE f.token = i.token AND f.action = 'issuer_burn') AS burns,
  (SELECT amount FROM stock_flows f WHERE f.token = i.token AND f.action = 'issuer_burn') AS burned_shares,
  (SELECT top_tx_sender FROM stock_flows f WHERE f.token = i.token AND f.action = 'issuer_mint') AS main_minter_tx_sender,
  (SELECT sum(n_swaps) FROM stock_venues v WHERE v.token = i.token) AS swaps,
  (SELECT sum(volume_shares) FROM stock_venues v WHERE v.token = i.token) AS swap_volume_shares,
  (SELECT arg_max(protocol || ' ' || pool, n_swaps) FROM stock_venues v WHERE v.token = i.token) AS top_venue,
  (SELECT sum(n) FROM stock_flows f WHERE f.token = i.token AND f.action IN ('bridge_in', 'bridge_out')) AS bridge_moves,
  ph.price_usd, ph.source AS price_source
FROM issuer_tokens i LEFT JOIN token_meta tm ON tm.token = i.token LEFT JOIN stock_multiplier sm ON sm.token = i.token
LEFT JOIN prices_head ph ON ph.token = i.token
WHERE i.kind = 'robinhood_stock';

CREATE OR REPLACE TABLE stock_top_holders AS
SELECT b.token, tm.symbol, b.wallet, b.balance_raw::DOUBLE / 1e18 AS shares_raw,
       b.balance_raw::DOUBLE / sum(b.balance_raw::DOUBLE) OVER (PARTITION BY b.token) AS share_of_supply,
       row_number() OVER (PARTITION BY b.token ORDER BY b.balance_raw DESC) AS rank
FROM focus_balances b JOIN issuer_tokens i ON i.token = b.token AND i.kind = 'robinhood_stock' LEFT JOIN token_meta tm ON tm.token = b.token
QUALIFY rank <= 10;

-- ---------- (B) stablecoins ----------
CREATE OR REPLACE TABLE stable_summary AS
WITH st AS (SELECT DISTINCT token FROM stable_anchor UNION SELECT token FROM issuer_tokens WHERE kind = 'stablecoin'),
fl AS (SELECT asset AS token, action, protocol, count(*) n, sum(amount_raw::DOUBLE) amt FROM econ_actions WHERE asset IN (SELECT token FROM st) GROUP BY ALL)
SELECT st.token, tm.symbol, tm.decimals,
  (SELECT count(*) FROM focus_balances b WHERE b.token = st.token) AS holders,
  (SELECT sum(balance_raw::DOUBLE) FROM focus_balances b WHERE b.token = st.token) / pow(10, tm.decimals) AS circulating,
  (SELECT sum(amt) FROM fl WHERE fl.token = st.token AND action = 'issuer_mint') / pow(10, tm.decimals) AS issuer_minted,
  (SELECT sum(amt) FROM fl WHERE fl.token = st.token AND action = 'issuer_burn') / pow(10, tm.decimals) AS issuer_burned,
  (SELECT sum(amt) FROM fl WHERE fl.token = st.token AND action = 'bridge_in') / pow(10, tm.decimals) AS bridged_in,
  (SELECT sum(amt) FROM fl WHERE fl.token = st.token AND action = 'bridge_out') / pow(10, tm.decimals) AS bridged_out,
  (SELECT string_agg(protocol || ':' || n, ' ' ORDER BY n DESC) FROM fl WHERE fl.token = st.token AND action IN ('bridge_in', 'bridge_out')) AS bridge_protocols,
  (SELECT sum(amt) FROM fl WHERE fl.token = st.token AND action = 'supply') / pow(10, tm.decimals) AS lending_supplied,
  (SELECT string_agg(protocol || ':' || n, ' ' ORDER BY n DESC) FROM fl WHERE fl.token = st.token AND action IN ('supply', 'borrow')) AS lending_protocols,
  (SELECT sum(amt) FROM fl WHERE fl.token = st.token AND action = 'lp_add') / pow(10, tm.decimals) AS lp_added,
  (SELECT sum(n) FROM fl WHERE fl.token = st.token AND action = 'swap') AS swaps_as_input
FROM st LEFT JOIN token_meta tm ON tm.token = st.token;

-- ---------- (C) incentive claims (Merkl distributor; DRIP pays ARB through it) + what claimants did next (7 days) ----------
CREATE OR REPLACE TABLE incentive_claims AS
SELECT a.*, tm.symbol FROM econ_actions a LEFT JOIN token_meta tm ON tm.token = a.asset
WHERE a.action = 'incentive_claim' AND a.counterparty_contract IN (SELECT address FROM reg_contracts WHERE protocol = 'merkl');

CREATE OR REPLACE TABLE claim_followups AS
WITH c AS (SELECT wallet, asset, block_number, block_time, amount_raw, tx_hash FROM incentive_claims),
nx AS (
  SELECT c.wallet, c.asset, c.block_number AS claim_block, c.block_time AS claim_time, c.amount_raw AS claim_amount, a.action, a.protocol, a.block_time AS next_time,
         row_number() OVER (PARTITION BY c.wallet, c.block_number, c.asset ORDER BY a.block_number, a.log_index) AS rn
  FROM c JOIN econ_actions a ON a.wallet = c.wallet AND a.block_number > c.block_number AND a.block_time <= c.block_time + INTERVAL 7 DAY
   AND (a.asset = c.asset OR json_extract_string(a.extra_json, '$.token_out') = c.asset)
   AND a.action <> 'incentive_claim'
)
SELECT c.wallet, c.asset, c.block_number, c.block_time, c.amount_raw, coalesce(nx.action, 'held_or_transfer') AS next_action, nx.protocol AS next_protocol, nx.next_time
FROM c LEFT JOIN nx ON nx.wallet = c.wallet AND nx.claim_block = c.block_number AND nx.asset = c.asset AND nx.rn = 1;

CREATE OR REPLACE TABLE claim_followup_summary AS
SELECT tm.symbol, date_trunc('month', f.block_time) AS month, f.next_action, count(*) AS claims, count(DISTINCT f.wallet) AS wallets,
       sum(f.amount_raw::DOUBLE / pow(10, coalesce(tm.decimals, 18))) AS amount
FROM claim_followups f LEFT JOIN token_meta tm ON tm.token = f.asset GROUP BY ALL ORDER BY month, claims DESC;

COPY stock_token_summary TO '${OUT}/focus_stock_tokens.csv' (HEADER);
COPY stock_top_holders TO '${OUT}/focus_stock_top_holders.csv' (HEADER);
COPY stock_venues TO '${OUT}/focus_stock_venues.csv' (HEADER);
COPY stable_summary TO '${OUT}/focus_stablecoins.csv' (HEADER);
COPY claim_followup_summary TO '${OUT}/focus_incentive_claims.csv' (HEADER);
