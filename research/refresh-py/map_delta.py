"""Wallet-map deltas since the snapshot: every block after the map snapshot up to the latest processed block.

The base map (map/<chain>/{addresses,edges,address_tokens,address_dex,token_stats}) is exact up to the snapshot and
is never rewritten (em-charts and the cards index read it). This module rebuilds cumulative delta tables over
[snap_end + 1, hi] on every run (the gap + realtime chunks are small: a few GB), written to map/<chain>/_delta/:
  edges.parquet           (from, to, asset, kind, n, total_amount DOUBLE, first_block, last_block)   same keys as base
  addresses.parquet       per-address activity in the delta range (counts add to the base row, *_time/last_* replace)
  address_tokens.parquet  (address, token, kind, n_in, n_out, net DOUBLE, first_block, last_block)
  address_dex.parquet     from econ v3 swap actions since the snapshot (address = acting wallet)
  token_stats.parquet     per token in the delta range
  meta.json               {"from", "to", "built_utc"}
Amounts are DOUBLE (the base keeps exact DECIMAL/limb sums); counts are exact. Degrees in the delta are distinct peers
inside the delta range only (merging with base degrees is an upper bound).
"""
import json
import ctypes
import fcntl
import shutil
import tempfile
import uuid
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CHAINS, DATA, bind_inc_views, connect, log  # noqa: E402

MACROS = """
CREATE OR REPLACE MACRO b2d(b) AS (CASE WHEN b IS NULL OR octet_length(b) = 0 THEN 0.0 ELSE
  ('0x' || substr(hex(b), 1, 16))::UBIGINT::DOUBLE * 6.277101735386681e57 + ('0x' || substr(hex(b), 17, 16))::UBIGINT::DOUBLE * 3.402823669209385e38
  + ('0x' || substr(hex(b), 33, 16))::UBIGINT::DOUBLE * 1.8446744073709552e19 + ('0x' || substr(hex(b), 49, 16))::UBIGINT::DOUBLE END);
CREATE OR REPLACE MACRO kind_name(std) AS CASE std WHEN 'ERC20' THEN 'erc20' WHEN 'WETH_DEPOSIT' THEN 'erc20' WHEN 'WETH_WITHDRAWAL' THEN 'erc20'
  WHEN 'ERC721' THEN 'erc721' WHEN 'ERC1155_SINGLE' THEN 'erc1155' WHEN 'ERC1155_BATCH' THEN 'erc1155' END;
"""


def _build_to(chain, hi, out, econ_v3_dir=None):
    snap = CHAINS[chain]["snap_end"]
    lo = snap + 1
    con = connect(threads=2, mem="4GB")
    inputs = os.path.join(out, "_inputs")
    os.makedirs(inputs)
    try:
        con.execute(MACROS)
        n = bind_inc_views(con, chain, lo, hi)
        log(f"map delta {chain} [{lo},{hi}] from {n} files")
        t = time.time()
        con.execute("CREATE TEMP TABLE blk AS SELECT number AS bn, to_timestamp(timestamp::BIGINT)::TIMESTAMP AS ts FROM blocks")
        con.execute(f"""COPY (
            SELECT "from" AS f, "to" AS t, token, kind_name(standard) AS kind, block_number AS bn,
                   CASE WHEN standard = 'ERC721' THEN 1.0 ELSE b2d(amount) END AS amt
            FROM token_transfers WHERE kind_name(standard) IS NOT NULL) TO '{inputs}/tt.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 65536)""")
        con.execute(f"CREATE TEMP VIEW tt AS SELECT * FROM read_parquet('{inputs}/tt.parquet')")
        con.execute(f"""COPY (
            SELECT "from" AS f, "to" AS t, status, block_number AS bn, CASE WHEN status = 1 THEN b2d(value) ELSE 0.0 END AS v
            FROM transactions) TO '{inputs}/tx.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 65536)""")
        con.execute(f"CREATE TEMP VIEW tx AS SELECT * FROM read_parquet('{inputs}/tx.parquet')")
        log(f"  loaded tx/tt {time.time() - t:.0f}s")

        def copy(name, sql):
            con.execute(f"COPY ({sql}) TO '{out}/{name}.parquet.tmp' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)")
            os.replace(f"{out}/{name}.parquet.tmp", f"{out}/{name}.parquet")
            log(f"  {name}: {con.execute(f'SELECT count(*) FROM read_parquet(?)', [f'{out}/{name}.parquet']).fetchone()[0]} rows")

        copy("edges", """
            SELECT f AS "from", t AS "to", 'native'::BLOB AS asset, 'native_tx' AS kind, count(*)::UBIGINT AS n,
                   sum(v) AS total_amount, min(bn) AS first_block, max(bn) AS last_block
            FROM tx WHERE t IS NOT NULL GROUP BY f, t
            UNION ALL
            SELECT f, t, token, kind, count(*)::UBIGINT, sum(CASE WHEN kind = 'erc721' THEN 0 ELSE amt END), min(bn), max(bn)
            FROM tt GROUP BY f, t, token, kind""")
        copy("address_tokens", """
            SELECT address, token, kind, sum(n_in)::UBIGINT AS n_in, sum(n_out)::UBIGINT AS n_out, sum(net) AS net,
                   min(fb) AS first_block, max(lb) AS last_block
            FROM (SELECT t AS address, token, kind, count(*) AS n_in, 0 AS n_out, sum(amt) AS net, min(bn) fb, max(bn) lb FROM tt GROUP BY ALL
                  UNION ALL
                  SELECT f, token, kind, 0, count(*), -sum(amt), min(bn), max(bn) FROM tt GROUP BY ALL)
            WHERE address IS NOT NULL GROUP BY address, token, kind""")
        copy("token_stats", """
            SELECT token, any_value(kind) AS kind, count(*)::UBIGINT AS n_transfers, count(DISTINCT t)::UBIGINT AS n_receivers,
                   count(DISTINCT f)::UBIGINT AS n_senders, min(bn) AS first_block, max(bn) AS last_block
            FROM tt GROUP BY token""")
        t = time.time()
        con.execute("""CREATE TEMP VIEW act AS
            SELECT f AS address, bn, 1 AS tx_sent, CASE WHEN status <> 1 THEN 1 ELSE 0 END AS tx_sent_failed, 0 AS tx_received,
                   0 AS tx_received_failed, v AS native_sent, 0.0 AS native_received, 0 AS tt_out, 0 AS tt_in, 0 AS logs_emitted, 0 AS created
            FROM tx
            UNION ALL SELECT t, bn, 0, 0, 1, CASE WHEN status <> 1 THEN 1 ELSE 0 END, 0.0, v, 0, 0, 0, 0 FROM tx WHERE t IS NOT NULL
            UNION ALL SELECT f, bn, 0, 0, 0, 0, 0.0, 0.0, 1, 0, 0, 0 FROM tt
            UNION ALL SELECT t, bn, 0, 0, 0, 0, 0.0, 0.0, 0, 1, 0, 0 FROM tt
            UNION ALL SELECT address, block_number, 0, 0, 0, 0, 0.0, 0.0, 0, 0, 1, 0 FROM logs""")
        has_cc = con.execute("SELECT count(*) FROM contract_creations").fetchone()[0] > 0
        copy("addresses", f"""
            WITH a AS (
              SELECT address, min(bn) AS first_block, max(bn) AS last_block, sum(tx_sent)::UBIGINT AS tx_sent,
                     sum(tx_sent_failed)::UBIGINT AS tx_sent_failed, sum(tx_received)::UBIGINT AS tx_received,
                     sum(tx_received_failed)::UBIGINT AS tx_received_failed, sum(native_sent) AS native_value_sent,
                     sum(native_received) AS native_value_received, sum(tt_out)::UBIGINT AS token_transfers_out,
                     sum(tt_in)::UBIGINT AS token_transfers_in, sum(logs_emitted)::UBIGINT AS logs_emitted
              FROM act WHERE address IS NOT NULL GROUP BY address),
            deg AS (
              SELECT address, count(DISTINCT peer) FILTER (WHERE dir = 'out')::UBIGINT AS out_degree,
                     count(DISTINCT peer) FILTER (WHERE dir = 'in')::UBIGINT AS in_degree, count(DISTINCT peer)::UBIGINT AS distinct_counterparties
              FROM (SELECT "from" AS address, "to" AS peer, 'out' AS dir FROM read_parquet('{out}/edges.parquet')
                    UNION ALL SELECT "to", "from", 'in' FROM read_parquet('{out}/edges.parquet')) GROUP BY address)
            {", cc AS (SELECT creator AS address, count(*)::UBIGINT AS contracts_created FROM contract_creations GROUP BY 1), "
             "made AS (SELECT address, any_value(creator) AS creator, min(block_number) AS contract_creation_block FROM contract_creations GROUP BY 1)" if has_cc else ""}
            SELECT a.*, b1.ts AS first_time, b2.ts AS last_time, coalesce(deg.out_degree, 0) AS out_degree, coalesce(deg.in_degree, 0) AS in_degree,
                   coalesce(deg.distinct_counterparties, 0) AS distinct_counterparties
                   {", coalesce(cc.contracts_created, 0)::UBIGINT AS contracts_created, made.creator, made.contract_creation_block, made.address IS NOT NULL AS created_in_delta" if has_cc else
                    ", 0::UBIGINT AS contracts_created, NULL::BLOB AS creator, NULL::UBIGINT AS contract_creation_block, false AS created_in_delta"}
            FROM a LEFT JOIN blk b1 ON b1.bn = a.first_block LEFT JOIN blk b2 ON b2.bn = a.last_block LEFT JOIN deg USING (address)
            {"LEFT JOIN cc USING (address) LEFT JOIN made USING (address)" if has_cc else ""}""")
        log(f"  addresses {time.time() - t:.0f}s")
        E3 = econ_v3_dir or f"{DATA}/econ/{chain}/v3"
        parts = [p for p in os.listdir(f"{E3}/econ_actions") if p.startswith("part_") and int(p[5:-8]) > snap] if os.path.isdir(f"{E3}/econ_actions") else []
        if parts:
            lst = "[" + ",".join(f"'{E3}/econ_actions/{p}'" for p in parts) + "]"
            copy("address_dex", f"""
                WITH s AS (
                  SELECT wallet, asset AS t_in, amount_raw::DOUBLE AS a_in, json_extract_string(extra_json, '$.token_out') AS t_out,
                         TRY_CAST(json_extract_string(extra_json, '$.amount_out') AS DOUBLE) AS a_out, block_number AS bn
                  FROM read_parquet({lst}, union_by_name=true) WHERE action = 'swap' AND wallet IS NOT NULL AND block_number BETWEEN {lo} AND {hi})
                SELECT unhex(substr(wallet, 3)) AS address, unhex(substr(token, 3)) AS token, sum(n_sold)::UBIGINT AS n_sold,
                       sum(n_bought)::UBIGINT AS n_bought, sum(sold) AS amount_sold, sum(bought) AS amount_bought,
                       min(bn) AS first_block, max(bn) AS last_block
                FROM (SELECT wallet, t_in AS token, 1 AS n_sold, 0 AS n_bought, a_in AS sold, 0.0 AS bought, bn FROM s
                      UNION ALL SELECT wallet, t_out, 0, 1, 0.0, a_out, bn FROM s)
                WHERE token LIKE '0x%' AND length(token) = 42 GROUP BY ALL""")
        json.dump({"from": lo, "to": hi, "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                  open(f"{out}/meta.json", "w"))
    finally:
        con.close()
        shutil.rmtree(inputs, ignore_errors=True)
    return {"from": lo, "to": hi}


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_generation(generation, live):
    """Publish a complete generation. Preserve the previous directory for rollback.

    The first upgrade exchanges the legacy directory and a symlink with Linux
    renameat2; later releases replace the symlink atomically. Multi-file readers
    resolve _delta once before reading to stay pinned to one generation.
    """
    parent = os.path.dirname(live)
    link = live + '.next-' + uuid.uuid4().hex
    os.symlink(os.path.relpath(generation, parent), link)
    try:
        if os.path.isdir(live) and not os.path.islink(live):
            libc = ctypes.CDLL(None, use_errno=True)
            rename = libc.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(link), -100, os.fsencode(live), 2) != 0:
                errno = ctypes.get_errno()
                raise OSError(errno, os.strerror(errno))
            # link now names the complete legacy directory. It remains recoverable
            # even if moving that backup fails; publication has already succeeded.
            try:
                os.rename(link, os.path.join(os.path.dirname(generation), 'legacy-' + uuid.uuid4().hex))
            except OSError as exc:
                log('legacy delta backup retained at %s: %s' % (link, exc))
        else:
            os.replace(link, live)
        _sync_dir(parent)
    finally:
        if os.path.islink(link):
            os.unlink(link)


def build(chain, hi, econ_v3_dir=None):
    if chain not in CHAINS or isinstance(hi, bool) or int(hi) < CHAINS[chain]['snap_end']:
        raise ValueError('Invalid chain or delta end block')
    hi = int(hi)
    parent = f'{DATA}/map/{chain}'
    generations = parent + '/_delta.generations'
    os.makedirs(generations, exist_ok=True)
    with open(parent + '/.delta-build.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        generation = tempfile.mkdtemp(prefix=str(hi) + '-', dir=generations)
        try:
            result = _build_to(chain, hi, generation, econ_v3_dir)
            # Flush file contents and directory entries before exposing the set.
            for name in os.listdir(generation):
                with open(os.path.join(generation, name), 'rb') as f:
                    os.fsync(f.fileno())
            _sync_dir(generation)
            _sync_dir(generations)
            _publish_generation(generation, parent + '/_delta')
            return {**result, 'generation': os.path.basename(generation)}
        except BaseException:
            # Never remove a published generation after e.g. a directory fsync error.
            if os.path.realpath(parent + '/_delta') != os.path.realpath(generation):
                shutil.rmtree(generation, ignore_errors=True)
            raise


if __name__ == "__main__":
    print(build(sys.argv[1], int(sys.argv[2])))
