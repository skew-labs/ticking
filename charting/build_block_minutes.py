"""Block -> UTC minute map per chain, read remotely from the Blob block Parquet (2 columns only) via DuckDB httpfs."""
import duckdb, sys, time, os
sas = open("/root/.em-blob.sas").read().strip().lstrip("?")
B = "https://arbitrum.blob.core.windows.net/em-mapping"
chain = sys.argv[1]
files = [l.split("\t")[0] for l in open(f"data/blocks_files_{chain}.tsv") if l.strip()]
c = duckdb.connect(); c.execute("SET threads=4"); c.execute("SET memory_limit=\x272GB\x27")
c.execute("INSTALL httpfs; LOAD httpfs")
c.execute("CREATE TABLE bm(minute UBIGINT, first_block UBIGINT, last_block UBIGINT, n UINTEGER)")
t0 = time.time(); step = 40
for i in range(0, len(files), step):
    urls = ",".join("\x27%s/%s/blocks/%s?%s\x27" % (B, chain, f, sas) for f in files[i:i+step])
    for attempt in range(5):
        try:
            c.execute(f"INSERT INTO bm SELECT timestamp//60*60, min(number), max(number), count(*)::UINTEGER FROM read_parquet([{urls}]) GROUP BY 1")
            break
        except Exception as e:
            print("retry", i, attempt, str(e).replace(sas, "SAS")[:200], flush=True); time.sleep(5 * (attempt + 1))
    else:
        raise SystemExit("failed batch %d" % i)
    if i % 400 == 0:
        print(chain, i, len(files), round(time.time() - t0), "s", flush=True)
# a minute can straddle two files: merge
c.execute(f"COPY (SELECT minute, min(first_block) first_block, max(last_block) last_block, sum(n)::UINTEGER n FROM bm GROUP BY 1 ORDER BY 1) TO \x27data/block_minutes_{chain}.parquet\x27 (FORMAT parquet, COMPRESSION zstd)")
print("DONE", chain, c.execute(f"SELECT count(*), min(minute), max(minute), sum(n) FROM \x27data/block_minutes_{chain}.parquet\x27").fetchall(), round(time.time() - t0), "s", flush=True)
