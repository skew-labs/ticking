-- token candidates for symbol/name/decimals eth_calls (ranked; tokens step takes top N not yet cached)
CREATE OR REPLACE VIEW econ_actions AS SELECT * FROM read_parquet('${OUT}/econ_actions/*.parquet');
CREATE OR REPLACE TABLE token_candidates AS
SELECT token, sum(score) AS score FROM (
  SELECT asset AS token, count(*) * 1.0 AS score FROM econ_actions WHERE asset LIKE '0x%' AND length(asset) = 42 GROUP BY 1
  UNION ALL SELECT json_extract_string(extra_json, '$.token_out'), count(*) FROM econ_actions WHERE action = 'swap' GROUP BY 1
  UNION ALL SELECT token, 1e12 FROM issuer_tokens
  UNION ALL SELECT address, 1e12 FROM reg_contracts WHERE role LIKE 'token:%'
  UNION ALL SELECT loan_token, 1e9 FROM dim_morpho_markets UNION ALL SELECT collateral_token, 1e9 FROM dim_morpho_markets
  UNION ALL SELECT underlying, 1e9 FROM dim_aave_tokens
  UNION ALL SELECT * FROM (SELECT '0x' || lower(hex(address)) AS token, sum(n) / 10.0 AS score FROM read_parquet('${OUT}/contract_topic_hist.parquet')
     WHERE topic0 = unhex('ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef') AND topic_count = 3 GROUP BY 1 ORDER BY 2 DESC LIMIT 3000)
) WHERE token IS NOT NULL AND token <> '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' GROUP BY token;
