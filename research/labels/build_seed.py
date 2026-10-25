"""build_seed.py <okx_por.parquet> <seed.parquet>: graded seed rows (verified signature -> official, else candidate)."""
import sys
import duckdb
src, out = sys.argv[1], sys.argv[2]
c = duckdb.connect()
c.execute(f"""COPY (SELECT chain, address, 'okx' AS entity_id,
  CASE WHEN sig_verified THEN 'official' ELSE 'candidate' END AS grade,
  CASE WHEN sig_verified THEN 'okx_por' ELSE 'okx_por_unverified' END AS source,
  network, snapshot_height, message, signature, coins FROM read_parquet('{src}')) TO '{out}' (FORMAT parquet)""")
print(c.execute(f"SELECT grade, count(*) FROM read_parquet('{out}') GROUP BY 1").fetchall())
