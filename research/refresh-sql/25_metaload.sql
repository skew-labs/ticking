-- Stage 3b: turn cached eth_call results (meta_calls) into lookup tables used by the decoders.
CREATE TABLE IF NOT EXISTS meta_calls (address VARCHAR, getter VARCHAR, ok BOOLEAN, raw BLOB);
CREATE OR REPLACE MACRO raw_addr(b) AS CASE WHEN octet_length(b) >= 32 THEN '0x' || lower(substr(hex(b), 25, 40)) END;
CREATE OR REPLACE MACRO raw_uint(b) AS CASE WHEN octet_length(b) >= 32 THEN w_uint(substr(hex(b), 1, 64)) END;
CREATE OR REPLACE MACRO raw_str(b) AS CASE
    WHEN octet_length(b) >= 64 AND w_uint(substr(hex(b), 1, 64)) = 32
      THEN TRY(decode(unhex(substr(hex(b), 129, CAST(w_uint(substr(hex(b), 65, 64)) AS BIGINT) * 2))))
    WHEN octet_length(b) = 32 THEN TRY(decode(unhex(rtrim(hex(b), '0'))))
  END;

-- Comets: base token from eth_call, falling back to the registry note
CREATE OR REPLACE TABLE dim_comets AS
SELECT d.comet, d.protocol, coalesce(raw_addr(m.raw), lower(regexp_extract(r.note, 'base (0x[0-9a-fA-F]{40})', 1))) AS base_token
FROM (SELECT comet, protocol FROM dim_comets) d
LEFT JOIN meta_calls m ON m.address = d.comet AND m.getter = 'baseToken()' AND m.ok
LEFT JOIN (SELECT lower(address) address, note FROM read_csv('${SRC}/contracts.csv', header=true, all_varchar=true)) r ON r.address = d.comet;

-- cTokens: underlying (CEther-style -> native)
CREATE OR REPLACE TABLE dim_ctokens AS
SELECT d.ctoken, d.protocol, d.n, coalesce(CASE WHEN m.ok THEN raw_addr(m.raw) END, '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee') AS underlying
FROM (SELECT ctoken, protocol, n FROM dim_ctokens) d LEFT JOIN meta_calls m ON m.address = d.ctoken AND m.getter = 'underlying()';

CREATE OR REPLACE TABLE meta_vault AS SELECT address AS vault, raw_addr(raw) AS asset FROM meta_calls WHERE getter = 'asset()' AND ok AND octet_length(raw) = 32;
CREATE OR REPLACE TABLE meta_oft AS SELECT address AS oft, raw_addr(raw) AS token FROM meta_calls WHERE getter = 'token()' AND ok AND octet_length(raw) = 32;
-- every aggregator that answered description() OR decimals(): newer Chainlink aggregators have no description() (arbitrum
-- LINK / USD 0x88e2..., 18 decimals), and dropping them made prices fall back to 8 decimals: LINK at $120.7B (1e10 too high)
CREATE OR REPLACE TABLE meta_aggregator AS
SELECT a.address AS aggregator, raw_str(d.raw) AS description, raw_uint(c.raw)::INTEGER AS decimals
FROM (SELECT DISTINCT address FROM meta_calls WHERE getter IN ('description()', 'decimals()') AND ok) a
LEFT JOIN meta_calls d ON d.address = a.address AND d.getter = 'description()' AND d.ok
LEFT JOIN meta_calls c ON c.address = a.address AND c.getter = 'decimals()' AND c.ok;

-- issuer-controlled tokens whose mint/burn = creation/redemption (stock tokens, registry stablecoins)
CREATE OR REPLACE TABLE issuer_tokens AS
SELECT DISTINCT '0x' || lower(hex(address)) AS token, 'robinhood_stock' AS kind, 'scaled_ui_stock' AS label
FROM read_parquet('${OUT}/contract_topic_hist.parquet')
WHERE topic0 = unhex('37e7f0db430edc9dd31bc66f25f8449353aa0818f503b906747dd8f286cd3802')
UNION
SELECT lower(address), 'stablecoin', role FROM read_csv('${SRC}/contracts.csv', header=true, all_varchar=true)
WHERE chain = '${CHAIN}' AND family = 'stable' AND role LIKE 'token:%';

CREATE TABLE IF NOT EXISTS gmx_v2_events (block_number UBIGINT, tx_index UINTEGER, log_index UINTEGER, contract VARCHAR, event_name VARCHAR,
  account VARCHAR, market VARCHAR, collateral_token VARCHAR, is_long BOOLEAN, size_in_usd HUGEINT, size_delta_usd HUGEINT,
  collateral_amount HUGEINT, collateral_delta_amount HUGEINT, order_type INTEGER, position_key VARCHAR, execution_price HUGEINT);
CREATE TABLE IF NOT EXISTS gmx_v2_markets (market VARCHAR, index_token VARCHAR, long_token VARCHAR, short_token VARCHAR);
