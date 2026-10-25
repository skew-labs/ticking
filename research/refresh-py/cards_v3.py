"""Refresh the agent's full-map cards index (index/<chain>/cards.duckdb) for every address active since the snapshot.

Changed set A = addresses in map/<chain>/_delta/addresses.parquet (cumulative since the snapshot, so A only grows).
The first time an address is refreshed, its original snapshot card rows are saved to index/<chain>/pristine/*.parquet;
every run rebuilds the cards of A from those pristine rows + the cumulative delta, so a refresh is idempotent and never
double counts. Ranking follows tools/build_fullmap_index.py: top 10 edges per direction, 12 tokens, 8 DEX tokens (an
edge or token outside the snapshot top list that only becomes relevant later is approximated by its delta alone).
The live file is held open read-only by the galaxy server, so the update goes to a copy that atomically replaces
cards.duckdb (the agent re-attaches when the inode changes).
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, connect, log, need_space  # noqa: E402

KEY = "CAST('0x' || substr(hex({c}), 1, 16) AS UBIGINT)"
TOP_EDGES, TOP_TOKENS, TOP_DEX = 10, 12, 8
TABLES = ("addr", "out_top", "in_top", "tok_top", "dex_top")


def refresh(chain):
    d = os.path.realpath(f"{DATA}/map/{chain}/_delta")
    idx = f"{DATA}/index/{chain}"
    live, nxt, pdir = f"{idx}/cards.duckdb", f"{idx}/cards.duckdb.next", f"{idx}/pristine"
    meta = json.load(open(f"{d}/meta.json"))
    need_space(80 + os.path.getsize(live) / 1e9, "cards copy")
    os.makedirs(pdir, exist_ok=True)
    t0 = time.time()
    for f in (nxt, nxt + ".wal"):
        if os.path.exists(f):
            os.remove(f)
    shutil.copyfile(live, nxt)
    log(f"cards {chain}: copied {os.path.getsize(live) / 1e9:.1f} GB in {time.time() - t0:.0f}s")
    con = connect(nxt, threads=3, mem="6GB")
    k = KEY.format
    con.execute(f"CREATE TEMP TABLE AK AS SELECT DISTINCT {k(c='address')} AS k, address FROM read_parquet('{d}/addresses.parquet') WHERE address IS NOT NULL")
    na = con.execute("SELECT count(*) FROM AK").fetchone()[0]
    # pristine snapshot rows: add the addresses touched for the first time (their cards rows are still the originals)
    have = os.path.exists(f"{pdir}/addr.parquet")
    con.execute("CREATE TEMP TABLE P AS " + (f"SELECT DISTINCT k, address FROM read_parquet('{pdir}/addr_keys.parquet')" if have
                                             else "SELECT NULL::UBIGINT AS k, NULL::BLOB AS address WHERE false"))
    con.execute("CREATE TEMP TABLE NEWA AS SELECT * FROM AK ANTI JOIN P USING (k, address)")
    nn = con.execute("SELECT count(*) FROM NEWA").fetchone()[0]
    for t in TABLES:
        old = f"UNION ALL SELECT * FROM read_parquet('{pdir}/{t}.parquet')" if have else ""
        con.execute(f"""COPY (SELECT x.* FROM {t} x SEMI JOIN NEWA n ON n.k = x.k AND n.address = x.address {old})
                        TO '{pdir}/{t}.parquet.tmp' (FORMAT parquet, COMPRESSION zstd)""")
    con.execute(f"COPY (SELECT * FROM P UNION ALL SELECT * FROM NEWA) TO '{pdir}/addr_keys.parquet.tmp' (FORMAT parquet)")
    for t in TABLES + ("addr_keys",):
        os.replace(f"{pdir}/{t}.parquet.tmp", f"{pdir}/{t}.parquet")
    log(f"cards {chain}: {na} changed addresses ({nn} touched for the first time)")
    has_dex = os.path.exists(f"{d}/address_dex.parquet")

    con.execute(f"""CREATE TEMP TABLE new_addr AS
        WITH b AS (SELECT * FROM read_parquet('{pdir}/addr.parquet')),
             x AS (SELECT * FROM read_parquet('{d}/addresses.parquet') WHERE address IS NOT NULL),
             dx AS ({f"SELECT address, sum(n_sold)::UBIGINT AS trades, count(DISTINCT token)::UBIGINT AS toks FROM read_parquet('{d}/address_dex.parquet') GROUP BY 1" if has_dex else "SELECT NULL::BLOB AS address, 0::UBIGINT AS trades, 0::UBIGINT AS toks WHERE false"})
        SELECT {k(c='x.address')} AS k, x.address,
               coalesce(b.first_time, x.first_time) AS first_time, greatest(b.last_time, x.last_time) AS last_time,
               coalesce(b.first_block, x.first_block) AS first_block, greatest(coalesce(b.last_block, 0), x.last_block) AS last_block,
               coalesce(b.tx_sent, 0) + x.tx_sent, coalesce(b.tx_sent_failed, 0) + x.tx_sent_failed, coalesce(b.tx_received, 0) + x.tx_received,
               coalesce(b.token_transfers_out, 0) + x.token_transfers_out, coalesce(b.token_transfers_in, 0) + x.token_transfers_in,
               coalesce(b.distinct_counterparties, 0) + x.distinct_counterparties, coalesce(b.out_degree, 0) + x.out_degree,
               coalesce(b.in_degree, 0) + x.in_degree,
               coalesce(b.is_contract, false) OR coalesce(x.created_in_delta, false),
               coalesce(b.creator, x.creator), coalesce(b.contract_creation_block, x.contract_creation_block),
               coalesce(b.contracts_created, 0) + coalesce(x.contracts_created, 0), coalesce(b.logs_emitted, 0) + x.logs_emitted,
               coalesce(b.dex_trades, 0) + coalesce(dx.trades, 0), coalesce(b.dex_pools, 0), greatest(coalesce(b.dex_tokens, 0), coalesce(dx.toks, 0)),
               coalesce(b.native_sent, 0) + x.native_value_sent / 1e18, coalesce(b.native_received, 0) + x.native_value_received / 1e18
        FROM x LEFT JOIN b USING (address) LEFT JOIN dx USING (address)""")
    for name, me, peer in (("out_top", '"from"', '"to"'), ("in_top", '"to"', '"from"')):
        con.execute(f"""CREATE TEMP TABLE new_{name} AS
            WITH g AS (
              SELECT address, peer, asset, kind, sum(n)::UBIGINT AS n, sum(amount) AS amount, min(first_block) AS first_block, max(last_block) AS last_block
              FROM (SELECT address, peer, asset, kind, n, amount, first_block, last_block FROM read_parquet('{pdir}/{name}.parquet')
                    UNION ALL SELECT {me}, {peer}, asset, kind, n, total_amount, first_block, last_block FROM read_parquet('{d}/edges.parquet') WHERE {me} IS NOT NULL)
              GROUP BY ALL)
            SELECT {k(c='address')} AS k, address, peer, asset, kind, n, amount, first_block, last_block FROM g
            QUALIFY row_number() OVER (PARTITION BY address ORDER BY n DESC, amount DESC NULLS LAST) <= {TOP_EDGES}""")
    con.execute(f"""CREATE TEMP TABLE new_tok_top AS
        WITH g AS (
          SELECT address, token, kind, sum(n_in)::UBIGINT AS n_in, sum(n_out)::UBIGINT AS n_out, sum(net) AS net_approx,
                 min(first_block) AS first_block, max(last_block) AS last_block
          FROM (SELECT address, token, kind, n_in, n_out, net_approx AS net, first_block, last_block FROM read_parquet('{pdir}/tok_top.parquet')
                UNION ALL SELECT address, token, kind, n_in, n_out, net, first_block, last_block FROM read_parquet('{d}/address_tokens.parquet'))
          GROUP BY ALL)
        SELECT {k(c='address')} AS k, address, token, kind, n_in, n_out, net_approx > 0 AS holds, net_approx, first_block, last_block FROM g
        QUALIFY row_number() OVER (PARTITION BY address ORDER BY n_in + n_out DESC) <= {TOP_TOKENS}""")
    dex_delta = (f"UNION ALL SELECT address, token, n_sold, n_bought, amount_sold, amount_bought, first_block, last_block "
                 f"FROM read_parquet('{d}/address_dex.parquet')") if has_dex else ""
    con.execute(f"""CREATE TEMP TABLE new_dex_top AS
        WITH g AS (
          SELECT address, token, sum(n_sold)::UBIGINT AS n_sold, sum(n_bought)::UBIGINT AS n_bought, sum(amount_sold) AS amount_sold,
                 sum(amount_bought) AS amount_bought, min(first_block) AS first_block, max(last_block) AS last_block
          FROM (SELECT address, token, n_sold, n_bought, amount_sold, amount_bought, first_block, last_block FROM read_parquet('{pdir}/dex_top.parquet')
                {dex_delta})
          GROUP BY ALL)
        SELECT {k(c='address')} AS k, address, token, n_sold, n_bought, amount_sold, amount_bought, first_block, last_block FROM g
        QUALIFY row_number() OVER (PARTITION BY address ORDER BY n_sold + n_bought DESC) <= {TOP_DEX}""")
    log(f"  merged rows built {time.time() - t0:.0f}s")
    for t in TABLES:
        n0 = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        con.execute(f"DELETE FROM {t} USING AK WHERE {t}.k = AK.k AND {t}.address = AK.address")
        con.execute(f"INSERT INTO {t} SELECT * FROM new_{t} ORDER BY k, address")
        log(f"  {t}: {n0} -> {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]} rows")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key VARCHAR, value VARCHAR)")
    con.execute("DELETE FROM meta WHERE key IN ('delta_to_block', 'delta_from_block', 'refreshed_utc', 'changed_addresses')")
    con.execute("INSERT INTO meta VALUES ('delta_from_block', ?), ('delta_to_block', ?), ('refreshed_utc', ?), ('changed_addresses', ?)",
                [str(meta["from"]), str(meta["to"]), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), str(na)])
    con.execute("CHECKPOINT")
    con.close()
    os.replace(nxt, live)
    # agent servers keep the old inode attached until their full-map status check runs; ask them now so the replaced
    # file (13-44 GB) is released instead of staying allocated as a deleted-but-open file
    import urllib.request
    for port in (8890, 8892):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/agent/status", timeout=60).read()
        except Exception:
            pass
    log(f"cards {chain}: refreshed {na} addresses through block {meta['to']} in {time.time() - t0:.0f}s")
    return {"changed": na, "to": meta["to"]}


if __name__ == "__main__":
    print(refresh(sys.argv[1]))
