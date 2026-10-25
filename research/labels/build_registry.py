"""build_registry.py <workdir>: institution registry + per-chain research label files.

Inputs in <workdir>: seed_*.parquet (official/candidate seeds), derived_*_<chain>.parquet (sweep_expand outputs), entities.json.
Outputs in <workdir>/out:
  addresses.parquet            every labelled address, all chains (chain, address, entity_id, entity_name, entity_type, role,
                               grade, source, source_url, as_of, evidence_json)
  entities.parquet             one row per entity with per-chain / per-grade counts (for search)
  <chain>/research_labels.parquet   graph-loop-export-shaped rows: address fixed 20 bytes, category, subtype, entity_name,
                               source, state, layer='research', grade, entity_id, evidence_json (readers overlay these first)
Grades: official (published by the institution, e.g. signed PoR), strong (structural on-chain evidence), candidate (weak).
"""
import glob
import json
import os
import sys

import duckdb


def main(w):
    ents = json.load(open(f"{w}/entities.json"))
    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'; SET threads=4")
    con.execute("CREATE TABLE ent(entity_id VARCHAR, name VARCHAR, type VARCHAR, aliases VARCHAR, website VARCHAR, sources VARCHAR)")
    con.executemany("INSERT INTO ent VALUES (?,?,?,?,?,?)", [(e["entity_id"], e["name"], e["type"], json.dumps(e.get("aliases", [])),
                    e.get("website"), json.dumps(e.get("sources", []))) for e in ents])
    src_url = {s["id"]: s for e in ents for s in e.get("sources", [])}
    con.execute("CREATE TABLE srcs(id VARCHAR, url VARCHAR, as_of VARCHAR)")
    con.executemany("INSERT INTO srcs VALUES (?,?,?)", [(k, v.get("file") or v.get("url"), v.get("as_of")) for k, v in src_url.items()])
    seeds = " UNION ALL ".join(f"""SELECT chain, address, entity_id, CASE WHEN source LIKE 'okx_por%' THEN 'por_wallet' ELSE 'wallet' END AS role,
        grade, source, json_object('network', network, 'snapshot_height', snapshot_height, 'message', message, 'signature', signature,
        'sig_verified', grade = 'official', 'coins', left(coins, 400)) AS evidence_json FROM read_parquet('{f}')"""
                               for f in sorted(glob.glob(f"{w}/seed_*.parquet")))
    derived = " UNION ALL ".join(f"SELECT chain, address, entity_id, role, grade, source, evidence_json FROM read_parquet('{f}')"
                                 for f in sorted(glob.glob(f"{w}/derived_*.parquet")))
    con.execute(f"""CREATE TABLE a AS
        WITH u AS ({seeds} {'UNION ALL ' + derived if derived else ''}),
        ranked AS (SELECT *, row_number() OVER (PARTITION BY chain, address ORDER BY
                     CASE grade WHEN 'official' THEN 0 WHEN 'strong' THEN 1 ELSE 2 END) AS rk FROM u)
        SELECT r.chain, r.address, r.entity_id, e.name AS entity_name, e.type AS entity_type, r.role, r.grade, r.source,
               coalesce(s.url, CASE WHEN r.source LIKE 'em_sweep%' THEN 'em-mapping graph (sweep edges)' END) AS source_url,
               coalesce(s.as_of, strftime(current_date, '%Y-%m-%d')) AS as_of, r.evidence_json
        FROM ranked r JOIN ent e USING (entity_id) LEFT JOIN srcs s ON s.id = regexp_replace(r.source, '_unverified$', '')
        WHERE rk = 1""")
    os.makedirs(f"{w}/out", exist_ok=True)
    con.execute(f"COPY a TO '{w}/out/addresses.parquet' (FORMAT parquet)")
    con.execute(f"""COPY (SELECT e.*, (SELECT json_group_object(chain, n) FROM (SELECT chain, count(*) n FROM a WHERE a.entity_id = e.entity_id GROUP BY 1)) AS per_chain,
                         (SELECT json_group_object(grade, n) FROM (SELECT grade, count(*) n FROM a WHERE a.entity_id = e.entity_id GROUP BY 1)) AS per_grade
                  FROM ent e) TO '{w}/out/entities.parquet' (FORMAT parquet)""")
    for (chain,) in con.execute("SELECT DISTINCT chain FROM a").fetchall():
        os.makedirs(f"{w}/out/{chain}", exist_ok=True)
        con.execute(f"""COPY (SELECT from_hex(substr(address, 3))::BLOB AS address,
               CASE WHEN role = 'deposit' THEN 'cex_deposit' WHEN entity_type = 'exchange' THEN 'cex_hot_wallet' ELSE entity_type END AS category,
               role AS subtype, entity_name, entity_id, entity_type, grade, source, source_url, as_of, 'final' AS state,
               'research' AS layer, evidence_json
            FROM a WHERE chain = '{chain}' ORDER BY address) TO '{w}/out/{chain}/research_labels.parquet' (FORMAT parquet)""")
    print(json.dumps({"entities": con.execute("SELECT count(*) FROM ent").fetchone()[0],
                      "by_chain_grade": con.execute("SELECT chain, grade, count(*) FROM a GROUP BY 1, 2 ORDER BY 1, 2").fetchall()}))


if __name__ == "__main__":
    main(sys.argv[1])
