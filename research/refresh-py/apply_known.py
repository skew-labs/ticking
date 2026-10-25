"""Apply known entities (labels/known_entities.csv: treasury, foundation, DAO, team multisigs from verifiable public sources)
to every label consumer:
  1. graph-loop cores (POST /admin/import_labels on :8710 robinhood / :8711 arbitrum; the core's validators still apply,
     e.g. a contract category is rejected where the graph image marks the address as an EOA),
  2. the graph-loop export read by em-charts analytics (em-graphloop/export/<chain>/labels.parquet, rewritten atomically
     with source='public_registry' rows taking precedence),
  3. galaxy.db nodes (category, entity_name, label_source='public_registry').
Idempotent; refresh.py re-runs it only when the CSV changes. Takes the shared locks labels-<chain> (export, also used by
em-labeldelta) and galaxy.db (emlock.py) around its writes.
"""
import csv
import hashlib
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emlock  # noqa: E402
from common import CHAINS, ROOT, connect, log  # noqa: E402

CSV = f"{ROOT}/labels/known_entities.csv"
EXPORT = "/srv/skew/em-graphloop/export/{chain}/labels.parquet"
GALAXY = "/srv/skew/em-galaxy/galaxy.db"
CATS = ["cex_hot_wallet", "cex_deposit", "bridge", "dex_router", "dex_pool", "lending", "token_contract", "nft_contract",
        "mev_bot", "multisig_or_safe", "contract_other", "eoa_user", "eoa_whale", "unknown"]


def digest():
    return hashlib.sha256(open(CSV, "rb").read()).hexdigest()


def rows_by_chain():
    by = {}
    for r in csv.DictReader(open(CSV)):
        ch = next(c for c, v in CHAINS.items() if str(v["id"]) == r["chain"])
        by.setdefault(ch, []).append(r)
    return by


def glserve_import(chain, rows):
    lines = "\n".join(json.dumps({"address": r["address"], "category": r["category"], "confidence": 0.99,
                                  "provider": "public_registry", "model": "known_entities_v1", "entity_name": r["entity_name"],
                                  "evidence": [{"key": "source", "value": r["source"]}, {"key": "kind", "value": r["kind"]}]})
                      for r in rows).encode()
    port = CHAINS[chain]["glserve"]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/admin/import_labels", lines, {"Content-Type": "application/x-ndjson"})
    res = json.load(urllib.request.urlopen(req, timeout=120))
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/admin/snapshot", b"", method="POST"), timeout=120).read()
    except Exception as e:  # noqa: BLE001
        log(f"{chain}: snapshot failed: {e}")
    return res


def patch_export(chain, rows):
    with emlock.hold(f"labels-{chain}", log=log):
        return _patch_export(chain, rows)


def _patch_export(chain, rows):
    path = EXPORT.format(chain=chain)
    if not os.path.exists(path):
        return 0
    con = connect(threads=2, mem="2GB")
    con.execute("CREATE TABLE k (address VARCHAR, category VARCHAR, entity_name VARCHAR, kind VARCHAR, source VARCHAR)")
    con.executemany("INSERT INTO k VALUES (?, ?, ?, ?, ?)", [(r["address"], r["category"], r["entity_name"], r["kind"], r["source"]) for r in rows])
    con.execute("CREATE TABLE cc (category VARCHAR, code UTINYINT)")
    con.executemany("INSERT INTO cc VALUES (?, ?)", [(c, i) for i, c in enumerate(CATS)])
    for attempt in range(3):
        m0 = os.stat(path).st_mtime_ns
        n = _write_export(con, path)
        if os.stat(path).st_mtime_ns == m0:     # em-labeldelta checks mtime the same way (it does not take the lock yet)
            os.replace(path + ".tmp", path)
            break
        os.remove(path + ".tmp")
    con.close()
    return n


def _write_export(con, path):
    con.execute(f"""COPY (
        WITH kb AS (SELECT unhex(substr(k.address, 3)) AS address, k.*, cc.code FROM k JOIN cc USING (category)),
        l AS (SELECT * FROM read_parquet('{path}'))
        SELECT l.* REPLACE (
                 coalesce(kb.category, l.category) AS category, coalesce(kb.code, l.category_code) AS category_code,
                 CASE WHEN kb.address IS NOT NULL THEN 0.99::FLOAT ELSE l.confidence END AS confidence,
                 CASE WHEN kb.address IS NOT NULL THEN 'public_registry' ELSE l.source END AS source,
                 CASE WHEN kb.address IS NOT NULL THEN 'confirmed' ELSE l.state END AS state,
                 CASE WHEN kb.address IS NOT NULL THEN kb.kind ELSE l.subtype END AS subtype,
                 CASE WHEN kb.address IS NOT NULL THEN 'public_registry' ELSE l.rule END AS rule,
                 coalesce(kb.entity_name, l.entity_name) AS entity_name,
                 CASE WHEN kb.address IS NOT NULL THEN 'public_registry' ELSE l.provider END AS provider,
                 CASE WHEN kb.address IS NOT NULL THEN 'known_entities_v1' ELSE l.model END AS model,
                 CASE WHEN kb.address IS NOT NULL THEN json_object('source', kb.source, 'kind', kb.kind)::VARCHAR ELSE l.evidence_json END AS evidence_json)
        FROM l LEFT JOIN kb USING (address)
        UNION ALL BY NAME
        SELECT kb.address, NULL::UINTEGER AS node_id, kb.category, kb.code AS category_code, 0.99::FLOAT AS confidence, 'public_registry' AS source,
               'confirmed' AS state, kb.kind AS subtype, NULL::UTINYINT AS tier, 'public_registry' AS rule, kb.entity_name, NULL::FLOAT AS importance,
               'public_registry' AS provider, 'known_entities_v1' AS model, json_object('source', kb.source, 'kind', kb.kind)::VARCHAR AS evidence_json,
               NULL::UBIGINT AS wal_seq
        FROM kb ANTI JOIN l USING (address)
    ) TO '{path}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
    return con.execute(f"SELECT count(*) FROM read_parquet('{path}.tmp') WHERE source = 'public_registry'").fetchone()[0]


def patch_galaxy(chain, rows):
    with emlock.hold("galaxy.db", log=log):
        g = emlock.connect_writer(GALAXY)
        emlock.batched(g, """UPDATE nodes SET category=?, entity_name=?, label_source='public_registry', confidence=0.99
                             WHERE chain=? AND address=?""", [(r["category"], r["entity_name"], chain, r["address"]) for r in rows],
                       what=f"galaxy {chain} known entities", log=log)
        n = g.total_changes
        g.close()
    return n


def main():
    by = rows_by_chain()
    out = {}
    for chain, rows in by.items():
        r1 = glserve_import(chain, rows)
        r2 = patch_export(chain, rows)
        r3 = patch_galaxy(chain, rows)
        out[chain] = {"entities": len(rows), "glserve": {k: r1.get(k) for k in ("accepted_committed", "rejected", "not_in_graph", "reject_reasons") if k in r1},
                      "export_rows": r2, "galaxy_nodes": r3}
        log(f"{chain}: {json.dumps(out[chain])}")
    json.dump({"sha256": digest(), "applied_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "result": out},
              open(f"{ROOT}/state/known_entities_applied.json", "w"), indent=1)
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=1))
