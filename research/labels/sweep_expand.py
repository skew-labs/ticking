"""sweep_expand.py <chain> <seed.parquet> <labels.parquet> <edges_glob> <out.parquet>: extend institution seeds over EM sweep edges.

Seeds: official addresses (grade official, e.g. OKX PoR with verified signatures). Two structural steps on the chain's map edges:
  hubs      a classifier sweep hub (category cex_hot_wallet) that sends >= 50% of its outgoing transfers (by count) to the
            entity's official wallets -> grade strong (evidence: hub -> official edges)
  deposits  a classifier deposit (category cex_deposit) whose top outgoing counterparty (>= 80% of its outgoing transfers) is an
            official wallet or a strong hub of the entity -> grade strong if it swept at least twice, else candidate (evidence: the sweep edge: counterparty, n, blocks)
Everything else stays unnamed. No probabilities. Low memory: DuckDB 6 GB, 4 threads, 3 GB temp.
"""
import json
import sys

import duckdb


def main(chain, seed, labels, edges, out):
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'; SET threads=4; SET preserve_insertion_order=false; "
                "SET temp_directory='/srv/skew/em-data/tmp/research/duck'; SET max_temp_directory_size='3GB'")
    con.execute(f"""CREATE TABLE official AS SELECT DISTINCT from_hex(substr(address,3)) AS a, entity_id
                    FROM read_parquet('{seed}') WHERE chain = '{chain}' AND grade = 'official'""")
    con.execute(f"""CREATE TABLE cls AS SELECT address AS a, category FROM read_parquet('{labels}')
                    WHERE category IN ('cex_hot_wallet','cex_deposit')""")
    # hubs -> official
    con.execute(f"""CREATE TABLE hub_out AS
        SELECT e."from" AS a, o.entity_id, sum(e.n) AS n_to, min(e.first_block) AS fb, max(e.last_block) AS lb, count(*) AS edges
        FROM read_parquet('{edges}') e JOIN official o ON e."to" = o.a
        WHERE e."from" IN (SELECT a FROM cls WHERE category = 'cex_hot_wallet') AND e."from" NOT IN (SELECT a FROM official)
        GROUP BY 1, 2""")
    con.execute(f"""CREATE TABLE hub_tot AS SELECT e."from" AS a, sum(e.n) AS n_all FROM read_parquet('{edges}') e
                    WHERE e."from" IN (SELECT a FROM hub_out) GROUP BY 1""")
    con.execute("""CREATE TABLE hubs AS SELECT h.a, h.entity_id, h.n_to, t.n_all, h.fb, h.lb FROM hub_out h JOIN hub_tot t USING (a)
                   WHERE h.n_to * 2 >= t.n_all""")
    con.execute("CREATE TABLE targets AS SELECT a, entity_id, 'official' AS via FROM official UNION ALL SELECT a, entity_id, 'hub' FROM hubs")
    # deposits -> (official | hub)
    con.execute(f"""CREATE TABLE dep_to AS
        SELECT e."from" AS a, t.entity_id, e."to" AS dst, t.via, sum(e.n) AS n_to, min(e.first_block) AS fb, max(e.last_block) AS lb
        FROM read_parquet('{edges}') e JOIN targets t ON e."to" = t.a
        WHERE e."from" IN (SELECT a FROM cls WHERE category = 'cex_deposit') GROUP BY 1, 2, 3, 4""")
    con.execute(f"""CREATE TABLE dep_tot AS SELECT e."from" AS a, sum(e.n) AS n_all FROM read_parquet('{edges}') e
                    WHERE e."from" IN (SELECT DISTINCT a FROM dep_to) GROUP BY 1""")
    con.execute("""CREATE TABLE deps AS SELECT d.* , t.n_all FROM (
                     SELECT *, row_number() OVER (PARTITION BY a ORDER BY n_to DESC) AS rk FROM dep_to) d JOIN dep_tot t USING (a)
                   WHERE d.rk = 1 AND d.n_to * 5 >= t.n_all * 4""")
    con.execute(f"""COPY (
        SELECT '{chain}' AS chain, '0x' || lower(hex(a)) AS address, entity_id, 'hot_wallet' AS role, 'strong' AS grade,
               'em_sweep_hub' AS source,
               json_object('rule', 'hub sends >= 50% of transfers to official wallets', 'n_to_official', n_to, 'n_out', n_all,
                           'first_block', fb, 'last_block', lb) AS evidence_json
        FROM hubs
        UNION ALL
        SELECT '{chain}', '0x' || lower(hex(a)), entity_id, 'deposit',
               CASE WHEN n_all >= 2 THEN 'strong' ELSE 'candidate' END,   -- a single transfer cannot tell a deposit sweep from a user send
               'em_sweep_deposit',
               json_object('rule', 'deposit sweeps >= 80% of transfers to the entity', 'sweeps_to', '0x' || lower(hex(dst)),
                           'to_kind', via, 'n_to', n_to, 'n_out', n_all, 'first_block', fb, 'last_block', lb)
        FROM deps) TO '{out}' (FORMAT parquet)""")
    r = con.execute(f"SELECT role, entity_id, count(*) FROM read_parquet('{out}') GROUP BY 1, 2 ORDER BY 3 DESC").fetchall()
    print(json.dumps({"chain": chain, "official": con.execute("SELECT count(*) FROM official").fetchone()[0], "derived": r}))


if __name__ == "__main__":
    main(*sys.argv[1:6])
