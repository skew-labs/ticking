-- CASHCAT facts from the frozen Robinhood map sample (map end block 88,098,834). Emits one JSON row per fact.
SELECT 'creator_cashcat_balance_row' AS fact, '0x'||lower(hex(address)) AS address, '0x'||lower(hex(token)) AS token, kind,
       n_in::VARCHAR AS n_in, n_out::VARCHAR AS n_out, holds, net_approx, first_block, last_block
FROM read_parquet('golden/robinhood/address_tokens.parquet')
WHERE address = from_hex('cdfc08a1c1fbafb355645e5ddc32122e5716ca90') AND token = from_hex('020bfc650a365f8bb26819deaabf3e21291018b4');
SELECT 'cashcat_token_stats' AS fact, '0x'||lower(hex(token)) AS token, standard, n_transfers::VARCHAR AS n_transfers,
       n_holders_approx::VARCHAR AS n_holders_approx, first_block, last_block, first_time::VARCHAR AS first_time, last_time::VARCHAR AS last_time
FROM read_parquet('golden/robinhood/token_stats.parquet') WHERE token = from_hex('020bfc650a365f8bb26819deaabf3e21291018b4');
SELECT 'creator_out_edges' AS fact, count(*) AS rows, sum(n)::VARCHAR AS n_sum,
       count(*) FILTER (WHERE asset = from_hex('020bfc650a365f8bb26819deaabf3e21291018b4')) AS cashcat_rows
FROM read_parquet('golden/robinhood/edges.parquet') WHERE "from" = from_hex('cdfc08a1c1fbafb355645e5ddc32122e5716ca90');
SELECT 'creator_node' AS fact, '0x'||lower(hex(address)) AS address, first_block, last_block, tx_sent, tx_received, token_transfers_in,
       token_transfers_out, out_degree, in_degree, is_contract
FROM read_parquet('golden/robinhood/addresses.parquet') WHERE address = from_hex('cdfc08a1c1fbafb355645e5ddc32122e5716ca90');
