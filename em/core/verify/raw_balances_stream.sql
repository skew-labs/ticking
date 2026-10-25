-- raw_balances_stream.sql: same rules and output as raw_balances.sql, written as one streaming aggregation (no temp tables of hex strings),
-- for chains whose transfer history does not fit the temp-table form (Linea: 298M transfers, 77.6M pairs). @TT@, @OUT@, @MAXB@ as before.
SET threads=8; SET memory_limit='26GB'; SET temp_directory='/data/emv2-dev/verify/tmp/rawstream'; SET max_temp_directory_size='20GB'; SET preserve_insertion_order=false;
COPY (SELECT coalesce(k::VARCHAR, 'unknown') k, count(*) n FROM (SELECT CASE standard WHEN 'ERC20' THEN 1 WHEN 'WETH_DEPOSIT' THEN 1 WHEN 'WETH_WITHDRAWAL' THEN 1
      WHEN 'ERC721' THEN 2 WHEN 'ERC1155_SINGLE' THEN 3 WHEN 'ERC1155_BATCH' THEN 3 END k FROM read_parquet('@TT@') WHERE block_number <= @MAXB@) GROUP BY 1)
  TO '@OUT@/raw_counts.csv' (HEADER);
COPY (
  -- one scan: every transfer row becomes two legs through a cross join (a CTE read twice would be materialised in full)
  WITH legs AS (
    SELECT CASE s.sgn WHEN 1 THEN x."to" ELSE x."from" END holder, x.token, x.k, s.sgn, x.h, x.b
    FROM (SELECT block_number b, CASE standard WHEN 'ERC20' THEN 1 WHEN 'WETH_DEPOSIT' THEN 1 WHEN 'WETH_WITHDRAWAL' THEN 1 WHEN 'ERC721' THEN 2
                 WHEN 'ERC1155_SINGLE' THEN 3 WHEN 'ERC1155_BATCH' THEN 3 END::UTINYINT k, token, "from", "to", lpad(hex(coalesce(amount, ''::BLOB)), 64, '0') h
          FROM read_parquet('@TT@') WHERE block_number <= @MAXB@) x CROSS JOIN (VALUES (1), (-1)) s(sgn)
    WHERE x.k IS NOT NULL)
  SELECT '0x' || lower(hex(holder)) AS address, '0x' || lower(hex(token)) AS token, CASE k WHEN 1 THEN 'erc20' WHEN 2 THEN 'erc721' WHEN 3 THEN 'erc1155' END AS kind,
         count(*) FILTER (WHERE sgn = 1) n_in, count(*) FILTER (WHERE sgn = -1) n_out,
         sum(CASE WHEN k <> 2 THEN sgn * ('0x' || substr(h, 49, 16))::UBIGINT::HUGEINT ELSE 0 END)::VARCHAR d0,
         sum(CASE WHEN k <> 2 THEN sgn * ('0x' || substr(h, 33, 16))::UBIGINT::HUGEINT ELSE 0 END)::VARCHAR d1,
         sum(CASE WHEN k <> 2 THEN sgn * ('0x' || substr(h, 17, 16))::UBIGINT::HUGEINT ELSE 0 END)::VARCHAR d2,
         sum(CASE WHEN k <> 2 THEN sgn * ('0x' || substr(h, 1, 16))::UBIGINT::HUGEINT ELSE 0 END)::VARCHAR d3,
         NULL AS balance, min(b) first_block, max(b) last_block
  FROM legs GROUP BY holder, token, k) TO '@OUT@/raw_bal_limbs.csv' (HEADER);
