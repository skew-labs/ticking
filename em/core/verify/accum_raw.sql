-- accum_raw.sql: AUSD (arc) 7-day accumulators straight from the frozen snapshot (blocks + token_transfers), independent of EMV2.
-- Window (from, to] in block timestamps; per holder exact signed delta (ERC20 rows; amounts must fit 128 bits, checked); 0x0 left out.
SET threads=4; SET memory_limit='8GB'; SET temp_directory='/data/emv2-dev/verify/tmp/accum';
CREATE TEMP TABLE w AS SELECT min(number) FILTER (WHERE timestamp > 1791852351) fb, max(number) FILTER (WHERE timestamp <= 1792457151) lb
  FROM read_parquet('/data/emv2-in/arc/snap-20261020T1152Z/blocks/*.parquet');
CREATE TEMP TABLE t AS SELECT "from" f, "to" tt, lpad(hex(amount), 64, '0') h FROM read_parquet('/data/emv2-in/arc/snap-20261020T1152Z/token_transfers/*.parquet')
  WHERE token = from_hex('f5b08979251f398180385b54381ee3d6fa1bbe09') AND standard = 'ERC20' AND block_number BETWEEN (SELECT fb FROM w) AND (SELECT lb FROM w);
SELECT (SELECT fb FROM w) from_block, (SELECT lb FROM w) to_block, count(*) events, count(*) FILTER (WHERE substr(h, 1, 32) <> '00000000000000000000000000000000') over128 FROM t;
CREATE TEMP TABLE d AS SELECT a, sum(v)::HUGEINT delta, count(*) FILTER (WHERE v > 0) n_in, count(*) FILTER (WHERE v < 0) n_out FROM (
  SELECT tt a, ('0x' || substr(h, 33, 32))::UHUGEINT::HUGEINT v FROM t UNION ALL SELECT f, -('0x' || substr(h, 33, 32))::UHUGEINT::HUGEINT FROM t)
  WHERE a <> from_hex('0000000000000000000000000000000000000000') GROUP BY a;
SELECT 'accumulate' side, '0x' || lower(hex(a)) address, delta::VARCHAR delta_raw, n_in, n_out FROM (SELECT * FROM d ORDER BY delta DESC LIMIT 3)
UNION ALL SELECT 'distribute', '0x' || lower(hex(a)), delta::VARCHAR, n_in, n_out FROM (SELECT * FROM d ORDER BY delta ASC LIMIT 3);
SELECT count(*) FILTER (WHERE delta <> 0) wallets_changed, count(*) wallets FROM d;
