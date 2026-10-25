-- weth9_raw.sql: WETH9 (arc 0xe6b0...8c5a) balances from the frozen snapshot, independent of EMV2: ERC20 Transfer rows from token_transfers
-- plus Deposit(dst, wad) as a mint to dst and Withdrawal(src, wad) as a burn from src, read from logs (topic1 = the account, data = wad).
SET threads=4; SET memory_limit='8GB'; SET temp_directory='/data/emv2-dev/verify/tmp/weth9';
CREATE TEMP TABLE lg AS SELECT topic0, '0x' || right(lower(hex(topic1)), 40) acct, lpad(hex(data), 64, '0') h, block_number b
  FROM read_parquet('/data/emv2-in/arc/snap-20261020T1152Z/logs/*.parquet')
  WHERE address = from_hex('e6b0a06c87a1422f966e8a31b5d1f3b3fa348c5a') AND block_number <= 26859999
    AND topic0 IN (from_hex('e1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c'), from_hex('7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65'));
SELECT count(*) FILTER (WHERE topic0 = from_hex('e1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c')) deposits,
       count(*) FILTER (WHERE topic0 <> from_hex('e1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c')) withdrawals, count(*) extra_rows FROM lg;
CREATE TEMP TABLE legs AS
  SELECT acct a, CASE WHEN topic0 = from_hex('e1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c') THEN 1 ELSE -1 END * (('0x' || substr(h, 33, 16))::UBIGINT::HUGEINT * 18446744073709551616::HUGEINT + ('0x' || substr(h, 49, 16))::UBIGINT::HUGEINT) x FROM lg
  UNION ALL SELECT '0x' || lower(hex(tt)), v FROM (SELECT "to" tt, ('0x' || substr(lpad(hex(amount), 64, '0'), 33, 16))::UBIGINT::HUGEINT * 18446744073709551616::HUGEINT + ('0x' || substr(lpad(hex(amount), 64, '0'), 49, 16))::UBIGINT::HUGEINT v
     FROM read_parquet('/data/emv2-in/arc/snap-20261020T1152Z/token_transfers/*.parquet') WHERE token = from_hex('e6b0a06c87a1422f966e8a31b5d1f3b3fa348c5a') AND block_number <= 26859999)
  UNION ALL SELECT '0x' || lower(hex(f)), -v FROM (SELECT "from" f, ('0x' || substr(lpad(hex(amount), 64, '0'), 33, 16))::UBIGINT::HUGEINT * 18446744073709551616::HUGEINT + ('0x' || substr(lpad(hex(amount), 64, '0'), 49, 16))::UBIGINT::HUGEINT v
     FROM read_parquet('/data/emv2-in/arc/snap-20261020T1152Z/token_transfers/*.parquet') WHERE token = from_hex('e6b0a06c87a1422f966e8a31b5d1f3b3fa348c5a') AND block_number <= 26859999);
CREATE TEMP TABLE rb AS SELECT a, sum(x) bal FROM legs WHERE a <> '0x0000000000000000000000000000000000000000' GROUP BY a;
CREATE TEMP TABLE vb AS SELECT address a, balance::HUGEINT bal FROM read_csv('/data/emv2-out/arc/8af0335ecc25880f/dump/bal.tsv.zst', header=true, all_varchar=true)
  WHERE token = '0xe6b0a06c87a1422f966e8a31b5d1f3b3fa348c5a' AND address <> '0x0000000000000000000000000000000000000000';
SELECT (SELECT count(*) FROM rb) raw_addrs, (SELECT count(*) FROM vb) v2_addrs, (SELECT sum(bal)::VARCHAR FROM rb) raw_supply,
  (SELECT count(*) FROM rb FULL JOIN vb USING (a) WHERE rb.bal IS DISTINCT FROM vb.bal) differing;
