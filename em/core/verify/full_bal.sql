-- full_bal.sql: every Robinhood map address_tokens row (159.8M) against the EMV2 ledger dump: same (address, token, kind) keys, n_in, n_out,
-- first/last block equal, and the map's exact sign (holds) == EMV2 balance > 0. Balances compared as text (no 128-bit limit).
SET threads=6; SET memory_limit='40GB'; SET temp_directory='/data/emv2-dev/verify/tmp/fullbal'; SET preserve_insertion_order=false;
CREATE TEMP TABLE g AS SELECT '0x' || lower(hex(address)) a, '0x' || lower(hex(token)) t, kind k, n_in::UBIGINT ni, n_out::UBIGINT no_, holds, first_block::UBIGINT fb, last_block::UBIGINT lb
  FROM read_parquet('src/robinhood/full/address_tokens/*.parquet');
CREATE TEMP TABLE v AS SELECT lower(address) a, lower(token) t, kind k, n_in::UBIGINT ni, n_out::UBIGINT no_, (left(balance, 1) <> '-' AND balance <> '0') pos, first_block::UBIGINT fb, last_block::UBIGINT lb
  FROM read_csv('/data/emv2-out/robinhood/e265263e29d91ce4/dump/bal.tsv.zst', header=true, all_varchar=true);
SELECT (SELECT count(*) FROM g) map_rows, (SELECT count(*) FROM v) v2_rows,
  (SELECT count(*) FROM g ANTI JOIN v USING (a, t, k)) map_not_v2, (SELECT count(*) FROM v ANTI JOIN g USING (a, t, k)) v2_not_map,
  (SELECT count(*) FROM g JOIN v USING (a, t, k) WHERE g.ni <> v.ni OR g.no_ <> v.no_ OR g.fb <> v.fb OR g.lb <> v.lb) counters_differ,
  (SELECT count(*) FROM g JOIN v USING (a, t, k) WHERE g.holds <> v.pos) holds_differ;
