-- per-contract topic0 histogram over all extracted chunks (coverage + discovery input)
COPY (
  SELECT address, topic0, topic_count, count(*) AS n, min(block_number) AS first_block, max(block_number) AS last_block
  FROM logs GROUP BY ALL
) TO '${OUT}/contract_topic_hist.parquet' (FORMAT parquet, COMPRESSION zstd);
