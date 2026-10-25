-- Stage 6: token metadata + price history.
CREATE OR REPLACE VIEW econ_actions AS SELECT * FROM read_parquet('${OUT}/econ_actions/*.parquet');
CREATE OR REPLACE TABLE token_meta AS
SELECT s.address AS token, raw_str(s.raw) AS symbol, raw_str(n.raw) AS name, TRY_CAST(raw_uint(d.raw) AS INTEGER) AS decimals
FROM meta_calls s LEFT JOIN meta_calls n ON n.address = s.address AND n.getter = 'name()' AND n.ok
LEFT JOIN meta_calls d ON d.address = s.address AND d.getter = 'decimals()' AND d.ok
WHERE s.getter = 'symbol()' AND s.ok
UNION ALL SELECT '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', CASE WHEN ${CHAIN} = 5042 THEN 'USDC' ELSE 'ETH' END,
  CASE WHEN ${CHAIN} = 5042 THEN 'USDC (native, Arc)' ELSE 'Ether (native)' END, 18;   -- native_is_usdc (Arc)

-- ---- anchors: USD stables at $1, WETH/native via Chainlink ETH/USD ----
-- official stablecoin addresses only (registry, cited); symbol matches are NOT trusted (spoofed USDC/USDT/DAI exist)
CREATE OR REPLACE TABLE stable_anchor AS
SELECT DISTINCT lower(address) AS token FROM read_csv('${SRC}/contracts.csv', header=true, all_varchar=true)
WHERE chain = '${CHAIN}' AND family = 'stable' AND role LIKE 'token:%' AND role NOT IN ('token:sUSDe')
UNION SELECT token FROM dim_weth WHERE ${CHAIN} = 5042                       -- Arc: wrapped native = WUSDC ($1)
UNION SELECT '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' WHERE ${CHAIN} = 5042;

-- ---- Chainlink aggregator answers (AnswerUpdated emitted by OCR1/OCR2 aggregators) ----
-- only aggregators that an official Chainlink proxy points to (any phase), from ${SRC}/chainlink_verified.csv
-- (built by py/chainlink_verify.py from Chainlink's reference data directory + on-chain phaseAggregators). Anyone can emit
-- AnswerUpdated with a "BASE / USD" description: robinhood had "MSTR / USD" at $95.50 and "SNDK / USD" at $1,792 next to
-- Chainlink's "Robinhood MSTR / USD" ($153.88) and "Robinhood SNDK / USD" ($1,468); base/quote come from the official name.
CREATE OR REPLACE TABLE cl_verified AS
SELECT lower(aggregator) AS aggregator, any_value(name) AS name, any_value(base) AS base, any_value(quote) AS quote
FROM read_csv('${SRC}/chainlink_verified.csv', header=true, all_varchar=true) WHERE chain = '${CHAIN}' GROUP BY 1;
CREATE OR REPLACE TABLE prices_chainlink AS
SELECT e.contract AS aggregator, v.name AS description, v.base, v.quote,
       e.block_number, to_timestamp(e.updatedAt::BIGINT) AS ts, e."current"::DOUBLE / pow(10, coalesce(m.decimals, 8)) AS price
FROM ev_chainlink__AnswerUpdated e JOIN cl_verified v ON v.aggregator = e.contract LEFT JOIN meta_aggregator m ON m.aggregator = e.contract;
-- a base with >= 3 verified feeds (arbitrum LINK / USD has 4): an answer more than 5x off that day's median of the base's feeds is
-- a scaling error (unknown decimals), not a price. With 1-2 feeds a median cannot tell which one is wrong (decimals decide).
CREATE OR REPLACE TABLE prices_chainlink AS
WITH d AS (SELECT *, date_trunc('day', ts) AS day FROM prices_chainlink),
m AS (SELECT base, quote, day, median(price) AS med, count(DISTINCT aggregator) AS feeds FROM d WHERE price > 0 GROUP BY ALL)
SELECT d.* EXCLUDE (day) FROM d JOIN m USING (base, quote, day) WHERE m.feeds < 3 OR d.price BETWEEN m.med / 5 AND m.med * 5;

-- map feed base symbol -> on-chain token (WETH for ETH, WBTC/cbBTC for BTC, direct symbol match otherwise)
-- feeds attach ONLY to verified token addresses (never by symbol alone): ${SRC}/feed_tokens.csv (canonical bridged tokens
-- + registry) and, for Robinhood stock feeds, tokens from the issuer factory whose symbol equals the feed base.
-- Symbol matching priced an unrelated Robinhood "LINK" at Chainlink LINK/USD (-99.99% vs its DEX) and took memecoin
-- AnswerUpdated emitters (SHERWOOD / USD, STONKBROKER / USD) as feeds (fixed 2026-10-18, econ v2 feed fix).
CREATE OR REPLACE TABLE feed_token AS
WITH f AS (SELECT DISTINCT base FROM prices_chainlink WHERE upper(quote) = 'USD'),
allow AS (
  SELECT upper(base) AS base, lower(token) AS token FROM read_csv('${SRC}/feed_tokens.csv', header=true, all_varchar=true) WHERE chain = '${CHAIN}'
  UNION SELECT upper(tm.symbol), i.token FROM issuer_tokens i JOIN token_meta tm ON tm.token = i.token
  WHERE i.kind = 'robinhood_stock' AND tm.symbol IS NOT NULL)
SELECT f.base, a.token FROM f JOIN allow a ON a.base = upper(f.base);

CREATE OR REPLACE TABLE prices_daily_chainlink AS
SELECT ft.token, date_trunc('day', p.ts) AS day, arg_max(p.price, p.ts) AS price_usd, 'chainlink:' || any_value(p.description) AS source
FROM prices_chainlink p JOIN feed_token ft ON ft.base = p.base WHERE upper(p.quote) = 'USD' AND p.price > 0
GROUP BY ALL;

-- ---- DEX-derived prices: swaps vs stable anchors and vs WETH (priced by chainlink ETH/USD) ----
CREATE OR REPLACE TABLE eth_usd_daily AS
SELECT day, price_usd FROM prices_daily_chainlink WHERE token IN (SELECT token FROM feed_token WHERE upper(base) = 'ETH') QUALIFY row_number() OVER (PARTITION BY day ORDER BY source) = 1;
CREATE OR REPLACE TABLE weth_tokens AS SELECT token FROM (SELECT token FROM dim_weth UNION SELECT '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee') WHERE ${CHAIN} <> 5042;

-- streamed: only swaps with an anchor on one side, aggregated per (day, token) without materializing legs
CREATE OR REPLACE TABLE anchor AS SELECT token, 'usd' AS kind FROM stable_anchor UNION SELECT token, 'eth' FROM weth_tokens;
CREATE OR REPLACE TABLE prices_daily_dex AS
WITH s AS (
  SELECT date_trunc('day', block_time) AS day, asset AS t_in, amount_raw::DOUBLE AS a_in,
         json_extract_string(extra_json, '$.token_out') AS t_out, TRY_CAST(json_extract_string(extra_json, '$.amount_out') AS DOUBLE) AS a_out
  FROM econ_actions WHERE action = 'swap' AND family LIKE 'dex_%' AND amount_raw > 0
), legs AS (
  SELECT s.day, s.t_out AS token, s.a_out / pow(10, mo.decimals) AS amt,
         s.a_in / pow(10, mi.decimals) * CASE an.kind WHEN 'usd' THEN 1 ELSE e.price_usd END AS usd
  FROM s JOIN anchor an ON an.token = s.t_in JOIN token_meta mi ON mi.token = s.t_in JOIN token_meta mo ON mo.token = s.t_out
  LEFT JOIN eth_usd_daily e ON e.day = s.day
  WHERE s.t_out NOT IN (SELECT token FROM anchor)
  UNION ALL
  SELECT s.day, s.t_in, s.a_in / pow(10, mi.decimals), s.a_out / pow(10, mo.decimals) * CASE an.kind WHEN 'usd' THEN 1 ELSE e.price_usd END
  FROM s JOIN anchor an ON an.token = s.t_out JOIN token_meta mi ON mi.token = s.t_in JOIN token_meta mo ON mo.token = s.t_out
  LEFT JOIN eth_usd_daily e ON e.day = s.day
  WHERE s.t_in NOT IN (SELECT token FROM anchor)
)
SELECT token, day, sum(usd) / nullif(sum(amt), 0) AS price_usd, sum(usd) AS volume_usd, count(*) AS n_swaps, 'dex_vwap' AS source
FROM legs WHERE amt > 0 AND usd > 0 GROUP BY ALL HAVING sum(usd) >= 1000 AND count(*) >= 3;

CREATE OR REPLACE TABLE prices_daily AS
SELECT token, day, price_usd, source FROM prices_daily_chainlink
UNION ALL SELECT d.token, d.day, d.price_usd, d.source FROM prices_daily_dex d
  WHERE NOT EXISTS (SELECT 1 FROM prices_daily_chainlink c WHERE c.token = d.token AND c.day = d.day)
UNION ALL SELECT token, NULL, 1.0, 'stable_peg' FROM stable_anchor;

-- head price: chainlink last value, else DEX VWAP of the most recent day (<= 30 days old) for tokens with >= $10k 30-day DEX volume, else peg
CREATE OR REPLACE TABLE dex_vol_30d AS
SELECT token, sum(volume_usd) AS vol_30d_usd FROM prices_daily_dex WHERE day >= (SELECT max(day) FROM prices_daily_dex) - INTERVAL 30 DAY GROUP BY 1;
CREATE OR REPLACE TABLE prices_head AS
SELECT p.token, arg_max(p.price_usd, coalesce(p.day, TIMESTAMP '1970-01-01') + INTERVAL (CASE WHEN p.source LIKE 'chainlink%' THEN 1 ELSE 0 END) HOUR) AS price_usd,
       arg_max(p.source, coalesce(p.day, TIMESTAMP '1970-01-01') + INTERVAL (CASE WHEN p.source LIKE 'chainlink%' THEN 1 ELSE 0 END) HOUR) AS source,
       max(p.day) AS asof_day, any_value(v.vol_30d_usd) AS vol_30d_usd
FROM prices_daily p LEFT JOIN dex_vol_30d v ON v.token = p.token
WHERE (p.day IS NULL OR p.day >= (SELECT max(day) FROM prices_daily) - INTERVAL 30 DAY)
  AND (p.source <> 'dex_vwap' OR coalesce(v.vol_30d_usd, 0) >= 10000)
GROUP BY p.token;

COPY token_meta TO '${OUT}/token_meta.parquet' (FORMAT parquet);
COPY prices_daily TO '${OUT}/prices_daily.parquet' (FORMAT parquet);
COPY prices_head TO '${OUT}/prices_head.csv' (HEADER);
