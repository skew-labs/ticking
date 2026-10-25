-- raw_balances.sql: exact (holder, token, kind) flows recomputed straight from extraction token_transfers, independent of the map and
-- of EMV2, with the map's rules (build_map.sql stg_flows/merge_flows): kind 1 = ERC20 + WETH deposit/withdrawal, 2 = ERC721 (count only),
-- 3 = ERC1155 single/batch (amount, token ids summed); both legs of every transfer (self transfers count in and out); 0x0 kept.
-- Amount limbs are summed per leg as HUGEINT; balances that fit (high limbs zero after carry) are exact HUGEINT, others are finished in
-- Python (raw_big.py). @TT@ = token_transfers parquet glob, @OUT@ = output directory.
SET threads=4; SET memory_limit='16GB'; SET temp_directory='/data/emv2-dev/verify/tmp'; SET preserve_insertion_order=false;
CREATE TEMP TABLE t AS
SELECT block_number AS b, CASE standard WHEN 'ERC20' THEN 1 WHEN 'WETH_DEPOSIT' THEN 1 WHEN 'WETH_WITHDRAWAL' THEN 1 WHEN 'ERC721' THEN 2
       WHEN 'ERC1155_SINGLE' THEN 3 WHEN 'ERC1155_BATCH' THEN 3 END::UTINYINT AS k, token, "from" AS f, "to" AS tt,
       lpad(hex(coalesce(amount, ''::BLOB)), 64, '0') AS h
FROM read_parquet('@TT@') WHERE block_number <= @MAXB@;
COPY (SELECT standard_k AS k, count(*) AS n FROM (SELECT coalesce(k::VARCHAR, 'unknown') AS standard_k FROM t) GROUP BY 1) TO '@OUT@/raw_counts.csv' (HEADER);
CREATE TEMP TABLE legs AS
SELECT tt AS holder, token, k, 1::UBIGINT AS ni, 0::UBIGINT AS no, h, 1 AS sgn, b FROM t WHERE k IS NOT NULL
UNION ALL SELECT f, token, k, 0, 1, h, -1, b FROM t WHERE k IS NOT NULL;
CREATE TEMP TABLE fl AS
SELECT holder, token, k, sum(ni)::UBIGINT AS n_in, sum(no)::UBIGINT AS n_out,
       sum(CASE WHEN k <> 2 THEN sgn * ('0x' || substr(h, 49, 16))::UBIGINT::HUGEINT ELSE 0 END) AS d0,
       sum(CASE WHEN k <> 2 THEN sgn * ('0x' || substr(h, 33, 16))::UBIGINT::HUGEINT ELSE 0 END) AS d1,
       sum(CASE WHEN k <> 2 THEN sgn * ('0x' || substr(h, 17, 16))::UBIGINT::HUGEINT ELSE 0 END) AS d2,
       sum(CASE WHEN k <> 2 THEN sgn * ('0x' || substr(h, 1, 16))::UBIGINT::HUGEINT ELSE 0 END) AS d3,
       min(b) AS fb, max(b) AS lb
FROM legs GROUP BY holder, token, k;
COPY (SELECT '0x' || lower(hex(holder)) AS address, '0x' || lower(hex(token)) AS token,
             CASE k WHEN 1 THEN 'erc20' WHEN 2 THEN 'erc721' WHEN 3 THEN 'erc1155' END AS kind, n_in, n_out,
             CASE WHEN k = 2 THEN (n_in::HUGEINT - n_out::HUGEINT)::VARCHAR
                  WHEN d2 = 0 AND d3 = 0 AND abs(d1) < 4611686018427387904 THEN (d0 + d1 * 18446744073709551616::HUGEINT)::VARCHAR END AS balance,
             d0::VARCHAR AS d0, d1::VARCHAR AS d1, d2::VARCHAR AS d2, d3::VARCHAR AS d3, fb AS first_block, lb AS last_block
      FROM fl) TO '@OUT@/raw_bal_limbs.csv' (HEADER);
SELECT count(*) AS pairs, count(*) FILTER (WHERE balance IS NULL) AS need_python FROM read_csv('@OUT@/raw_bal_limbs.csv', all_varchar=true);
