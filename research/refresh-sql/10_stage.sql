-- Stage 1: one pass over logs.
--  (a) logs_sel: every registry event except the very high-volume ones handled elsewhere
--      (ERC20 Transfer -> token_transfers table; swaps -> (b); Sync -> (c); ScaledUI transfer -> sampled)
--  (b) swaps: compact decoded swaps (pool-perspective deltas a0/a1: + = pool received)
--  (c) v2 reserves at head (last Sync per pair) for LP valuation
CREATE OR REPLACE TABLE event_defs AS SELECT * FROM read_csv('${SRC}/events.csv', header=true, all_varchar=true);
CREATE OR REPLACE TABLE gmx2_names AS SELECT event_name, unhex(substr(name_hash,3)) h FROM read_csv('${SRC}/gmx_v2_events.csv', header=true, all_varchar=true);
CREATE OR REPLACE TABLE stage_t0 AS
  SELECT DISTINCT unhex(substr(topic0,3)) AS t0 FROM event_defs
  WHERE key NOT IN ('erc20.Transfer','robinhood_stock.TransferWithScaledUI','univ2.Sync','univ2.Swap','univ3.Swap',
                    'pancake_v3.Swap','univ4.Swap','gmx_v2.EventLog','gmx_v2.EventLog1','gmx_v2.EventLog2','relay.SolverCallExecuted');

COPY (
  SELECT chain, block_number, tx_index, log_index, address, topic0, topic1, topic2, topic3, topic_count, hex(data) AS h,
         (block_number // 5000000)::INTEGER AS p
  FROM logs
  WHERE topic0 IN (SELECT t0 FROM stage_t0)
     OR (topic0 IN (unhex('137a44067c8961cd7e1d876f4754a5a3a75989b4552f1843fc69c3b372def160'),
                    unhex('468a25a7ba624ceea6e540ad6f49171b52495b648417ae91bca21676d8a24dc5'))
         AND topic1 IN (SELECT h FROM gmx2_names))
) TO '${OUT}/stage/logs_sel' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (p), OVERWRITE_OR_IGNORE, ROW_GROUP_SIZE 500000);

COPY (
  WITH s AS (
    SELECT chain, block_number, tx_index, log_index, address, topic0, topic1, topic2, hex(data) AS h FROM logs
    WHERE topic_count = 3 AND topic0 IN (
      unhex('d78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822'),   -- univ2 Swap
      unhex('c42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'),   -- univ3/algebra Swap
      unhex('19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83'),   -- pancake v3 Swap
      unhex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'))   -- univ4 Swap
      AND (block_number BETWEEN ${SLO} AND ${SHI} OR block_number BETWEEN ${SLO2} AND ${SHI2})
  )
  SELECT chain, block_number, tx_index, log_index, address AS emitter,
    CASE WHEN topic0 = unhex('d78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822') THEN 'v2'
         WHEN topic0 = unhex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f') THEN 'v4' ELSE 'v3' END AS kind,
    CASE WHEN topic0 = unhex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f') THEN topic1 END AS v4_id,
    CASE WHEN topic0 = unhex('d78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822') THEN w_uint(substr(h,1,64)) - w_uint(substr(h,129,64))
         WHEN topic0 = unhex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f') THEN -w_int(substr(h,1,64))
         ELSE w_int(substr(h,1,64)) END AS a0,
    CASE WHEN topic0 = unhex('d78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822') THEN w_uint(substr(h,65,64)) - w_uint(substr(h,193,64))
         WHEN topic0 = unhex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f') THEN -w_int(substr(h,65,64))
         ELSE w_int(substr(h,65,64)) END AS a1,
    CASE WHEN topic0 = unhex('d78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822') THEN NULL ELSE w_dbl(substr(h,129,64)) END AS sqrtp,
    CASE WHEN topic0 = unhex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f') THEN NULL ELSE topic2 END AS recipient,
    CASE WHEN topic0 = unhex('40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f') THEN topic2 ELSE topic1 END AS sender,
    (block_number // 5000000)::INTEGER AS p
  FROM s
) TO '${OUT}/stage/swaps' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (p), OVERWRITE_OR_IGNORE, ROW_GROUP_SIZE 1000000);

COPY (
  SELECT address AS pair, max(block_number) AS block_number,
         arg_max(w_uint(substr(hex(data),1,64)), block_number * 1000000 + log_index) AS reserve0,
         arg_max(w_uint(substr(hex(data),65,64)), block_number * 1000000 + log_index) AS reserve1
  FROM logs WHERE topic0 = unhex('1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1') AND topic_count = 1
  GROUP BY address
) TO '${OUT}/stage/v2_reserves_head.parquet' (FORMAT parquet, COMPRESSION zstd);
