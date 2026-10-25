#!/usr/bin/env python3
"""Apply econ v3 balance sheets (as of the latest refreshed block) and wallet-map deltas to the live galaxy.db in place.

usage: galaxy_v3.py <galaxy.db> <chain> [<chain> ...] [--dry-run] [--base-from <pristine galaxy.db copy>]

Same rules as galaxy_v2.py, reading /srv/skew/em-data/econ/<chain>/v3/{sheet_rows,sheet_summary}.v3.parquet + as_of.json.
Also: balance_sheets.as_of = as-of time; nodes activity (last_seen/last_block, tx_in/tx_out, token_in/token_out) updated
from map/<chain>/_delta/addresses.parquet; index.json/manifest.json/meta last_updated = as-of time.
Node activity is absolute and idempotent: value = snapshot value (v3/galaxy_nodes_base.parquet, captured before a node
is first touched; --base-from rebuilds it from a pristine copy) + cumulative delta since the snapshot, and only rows
that differ are written. A run that stopped half-way is therefore repaired by the next run.
Writes: shared flock /srv/skew/em-data/locks/galaxy.db.lock (emlock.py) around the write phase, one short
BEGIN IMMEDIATE transaction per <= 500 rows, busy_timeout 3 s, SQLITE_BUSY retried with jittered exponential backoff.
Original v2 description:
Reads /srv/skew/em-data/econ/<chain>/v2/{sheet_rows,sheet_summary}.v2.parquet and rewrites, for every galaxy node
that has a v1 balance sheet or a v2 sheet: balance_sheets (kind sums, net_worth, positions_json with a per-position
"q" quality flag) and stories.net_worth. Adds table bs_quality(chain, node_id, flags_json, net_worth_v1, net_worth_v2,
gross_net_usd) and patches the stories net_worth_usd in data/<chain>/index.json.
Kind sums follow build_layout.py: |usd| per kind for rows with |usd| >= 1, net = liquid+supplied+collateral+lp+perp-debt,
excluding rows flagged unverified_principal / morpho_interest_runaway (still listed in positions_json with "q");
ERC-4626 vault wallets (flag vault_pass_through) keep gross kind sums but net_worth 0.
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emlock  # noqa: E402

ROOT = "/srv/skew/em-data/econ"
NODE_COLS = ("last_block", "last_seen", "tx_out", "tx_in", "token_out", "token_in")
TMPDIR = "/srv/skew/em-data/tmp/refresh"
T0 = time.time()


def load_rows(d, name, cols, rows):
    """Bulk-load Python rows into a DuckDB table through a temp CSV (executemany is ~100x slower for 1M rows)."""
    os.makedirs(TMPDIR, exist_ok=True)
    path = f"{TMPDIR}/galaxy_v3_{name}_{os.getpid()}.csv"
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)
    spec = "{" + ", ".join(f"'{k}': '{v}'" for k, v in cols.items()) + "}"
    d.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM read_csv('{path}', header=false, columns={spec})")
    os.remove(path)


def log(msg):
    print("%s [%6.0fs] %s" % (time.strftime("%H:%M:%S"), time.time() - T0, msg), file=sys.stderr, flush=True)
KIND = {"liquid": 0, "supplied": 1, "collateral": 2, "debt": 3, "lp": 4, "perp_collateral": 5}
KIND_NAME = ["liquid", "supplied", "collateral", "debt", "lp", "perp"]
# shown in positions_json with their "q" flag but kept out of kind sums and net worth
UNVERIFIED = {"unverified_principal", "morpho_interest_runaway", "reserve_unbacked", "decoded_exceeds_onchain"}
# rows the econ check holds back (econ/<chain>/held_rows.parquet, 2026-10-20): q = "held:<held_code>", out of sums the same way
HELD_PREFIX = "held:"


def pretty(p):
    return (p or "wallet").replace("_", " ").title()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("chains", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wallets", default=None, help="CSV with a 'wallet' column: only these wallets are rewritten (per chain dir if '{chain}' in path)")
    ap.add_argument("--base-from", default=None, help="pristine galaxy.db copy (node activity as of the snapshot) to rebuild "
                                                       "v3/galaxy_nodes_base.parquet from, for the nodes in the current delta")
    a = ap.parse_args()
    g = emlock.connect_writer(a.db, busy_ms=30000)      # reads may wait; writes below switch to 3 s per attempt
    if not a.dry_run:
        with emlock.hold("galaxy.db", log=log):
            emlock.retry(lambda: emlock.tx(g, lambda c: c.execute(
                """CREATE TABLE IF NOT EXISTS bs_quality(chain TEXT, node_id INTEGER, flags_json TEXT, net_worth_v1 REAL,
                   net_worth_v2 REAL, gross_net_usd REAL, PRIMARY KEY (chain, node_id))""")), what="bs_quality", log=log)
    for ch in a.chains:
        t = time.time()
        g.execute("PRAGMA busy_timeout=30000")
        rows_f = f"{ROOT}/{ch}/v3/sheet_rows.v3.parquet"
        sum_f = f"{ROOT}/{ch}/v3/sheet_summary.v3.parquet"
        asof = json.load(open(f"{ROOT}/{ch}/v3/as_of.json"))
        as_of_t = int(asof["as_of_time"])
        as_of_iso = asof["as_of_utc"]
        nodes = dict(g.execute("SELECT address, node_id FROM nodes WHERE chain=?", (ch,)).fetchall())
        v1 = {nid: (nw, pj) for nid, nw, pj in g.execute("SELECT node_id, net_worth, positions_json FROM balance_sheets WHERE chain=?", (ch,))}
        pname_src = dict(v1)
        if a.wallets:
            wf = a.wallets.replace("{chain}", ch)
            keep = {r[0] for r in duckdb.sql(f"SELECT lower(wallet) FROM read_csv('{wf}', header=true)").fetchall()}
            nodes = {ad: n for ad, n in nodes.items() if ad in keep}
            ids_keep = set(nodes.values())
            v1 = {nid: v for nid, v in v1.items() if nid in ids_keep}
        pname = {}
        for _, pj in pname_src.values():
            for p in json.loads(pj or "[]"):
                if p.get("protocol") and p.get("protocol_name"):
                    pname[p["protocol"]] = p["protocol_name"]
        for pid, name in g.execute("SELECT protocol, name FROM protocols WHERE chain=?", (ch,)):
            pname.setdefault(pid, name)
        d = duckdb.connect()
        d.execute("SET threads=4; SET memory_limit='4GB'")
        load_rows(d, "gw", {"address": "VARCHAR", "node_id": "BIGINT"}, nodes.items())
        held_f = f"{ROOT}/{ch}/held_rows.parquet"
        if os.path.exists(held_f):   # matched on (wallet, category, protocol, asset); an empty asset holds every asset
            d.execute(f"""CREATE TEMP TABLE held AS SELECT wallet, category, coalesce(protocol, '') AS protocol,
                                 lower(coalesce(asset, '')) AS asset, any_value(held_code) AS code
                          FROM read_parquet('{held_f}') GROUP BY 1, 2, 3, 4""")
            hq, hj = "coalesce('{p}' || coalesce(h1.code, h2.code), r.quality)".format(p=HELD_PREFIX), (
                "LEFT JOIN held h1 ON h1.wallet = r.wallet AND h1.category = r.category AND h1.protocol = coalesce(r.protocol, '') "
                "AND h1.asset = lower(coalesce(r.asset, '')) "
                "LEFT JOIN held h2 ON h2.wallet = r.wallet AND h2.category = r.category AND h2.protocol = coalesce(r.protocol, '') AND h2.asset = ''")
            log(f"[{ch}] held rows: {d.execute('SELECT count(*) FROM held').fetchone()[0]} keys from {held_f}")
        else:
            hq, hj = "r.quality", ""
        rows = d.execute(f"""SELECT gw.node_id, r.category, r.protocol, coalesce(r.symbol, r.asset), r.amount, r.usd, {hq}
                             FROM read_parquet('{rows_f}') r JOIN gw ON gw.address = r.wallet {hj}
                             WHERE r.category IN ('liquid','supplied','collateral','debt','lp','perp_collateral')
                               AND abs(coalesce(r.usd, 0)) >= 1
                             ORDER BY gw.node_id, abs(r.usd) DESC""").fetchall()
        summ = {nid: (flags, nw, gross) for nid, flags, nw, gross in d.execute(
            f"""SELECT gw.node_id, s.flags, s.net_worth_usd, s.gross_net_usd FROM read_parquet('{sum_f}') s JOIN gw ON gw.address = s.wallet""").fetchall()}
        agg = {}
        for nid, cat, proto, asset, amt, usd, q in rows:
            e = agg.setdefault(nid, [[0.0] * 6, []])
            k = KIND[cat]
            if q not in UNVERIFIED and not str(q or "").startswith(HELD_PREFIX):
                e[0][k] += abs(usd)
            if len(e[1]) < 12:
                p = {"protocol": proto or "wallet", "kind": KIND_NAME[k], "asset": asset, "amount": round(float(amt or 0), 6),
                     "usd": round(abs(float(usd)), 2), "protocol_name": pname.get(proto or "wallet", pretty(proto))}
                if q:
                    p["q"] = q
                if str(q or "").startswith(HELD_PREFIX):   # held: listed without amounts
                    p.update(amount=None, usd=None, held=True, heldCode=q[len(HELD_PREFIX):])
                e[1].append(p)
        ids = set(v1) | set(agg)          # a summary without any >= $1 row does not create a new galaxy sheet
        bs_up, st_up, q_up = [], [], []
        for nid in ids:
            sums, pos = agg.get(nid, [[0.0] * 6, []])
            flags, nw2_s, gross = summ.get(nid, ([], None, None))
            flags = list(flags or [])
            net = sums[0] + sums[1] + sums[2] + sums[4] + sums[5] - sums[3]
            if "vault_pass_through" in flags:
                net = 0.0
            bs_up.append((round(sums[0], 2), round(sums[1], 2), round(sums[2], 2), round(sums[3], 2), round(sums[4], 2),
                          round(sums[5], 2), round(net, 2), json.dumps(pos, separators=(",", ":"), ensure_ascii=False), ch, nid))
            st_up.append((round(net, 2), ch, nid))
            q_up.append((ch, nid, json.dumps(flags), v1.get(nid, (None,))[0], round(net, 2), gross))
        changed = sum(1 for r in bs_up if r[9] in v1 and abs((v1[r[9]][0] or 0) - r[6]) > max(1.0, 0.01 * abs(v1[r[9]][0] or 0)))
        new = sum(1 for r in bs_up if r[9] not in v1)
        log(f"[{ch}] sheets v1={len(v1)} v2_rows_wallets={len(agg)} updates={len(bs_up)} net_changed={changed} new={new} ({time.time() - t:.1f}s)")
        # node activity targets are computed before taking the write lock
        nd_up = node_activity(g, d, ch, nodes, a)
        if a.dry_run:
            log(f"[{ch}] dry run: {len(nd_up)} node activity rows would change")
            continue
        with emlock.hold("galaxy.db", log=log):
            write_chain(g, a, ch, asof, as_of_t, as_of_iso, v1, ids, bs_up, st_up, q_up, nd_up)


def node_activity(g, d, ch, nodes, a):
    """Absolute activity rows (last_block, last_seen, tx_out, tx_in, token_out, token_in, chain, node_id) that differ
    from the live values: snapshot base + cumulative delta since the snapshot."""
    dpath = f"/srv/skew/em-data/map/{ch}/_delta/addresses.parquet"
    base_f = f"{ROOT}/{ch}/v3/galaxy_nodes_base.parquet"
    if not os.path.exists(dpath):
        return []
    act = d.execute(f"""SELECT gw.node_id, a.last_block, epoch(a.last_time)::BIGINT, a.tx_sent, a.tx_received,
                               a.token_transfers_out, a.token_transfers_in
                        FROM read_parquet('{dpath}') a JOIN gw ON gw.address = '0x' || lower(hex(a.address))""").fetchall()
    ids = [r[0] for r in act]
    idset = set(ids)
    cur = {r[0]: r[1:] for r in g.execute(f"SELECT node_id, {', '.join(NODE_COLS)} FROM nodes WHERE chain=?", (ch,)) if r[0] in idset}
    base = {}
    if a.base_from:
        src = sqlite3.connect(f"file:{a.base_from}?mode=ro", uri=True)
        base = {r[0]: r[1:] for r in src.execute(f"SELECT node_id, {', '.join(NODE_COLS)} FROM nodes WHERE chain=?", (ch,)) if r[0] in idset}
        src.close()
        log(f"[{ch}] node base rebuilt from {a.base_from}: {len(base)} nodes")
    elif os.path.exists(base_f):
        base = {r[0]: tuple(r[1:]) for r in d.execute(f"SELECT node_id, {', '.join(NODE_COLS)} FROM read_parquet('{base_f}')").fetchall()}
    elif os.path.exists(f"{ROOT}/{ch}/v3/galaxy_nodes_applied.json"):
        # earlier (incremental) runs already moved live rows away from the snapshot: their values cannot serve as base
        raise RuntimeError(f"{base_f} missing but node activity was applied before; run once with --base-from <pristine galaxy.db copy>")
    # a node not in the base was never touched by a refresh: its live row still holds the snapshot values
    missing = [nid for nid in ids if nid not in base and nid in cur]
    for nid in missing:
        base[nid] = cur[nid]
    if (missing or a.base_from) and not a.dry_run:
        load_rows(d, "nb", {"node_id": "BIGINT", **{c: "BIGINT" for c in NODE_COLS}}, ((k,) + tuple(v) for k, v in base.items()))
        d.execute(f"COPY nb TO '{base_f}.tmp' (FORMAT parquet)")
        os.replace(f"{base_f}.tmp", base_f)
        log(f"[{ch}] node base: {len(base)} nodes ({len(missing)} captured now)")
    out = []
    for nid, lb, lt, txs, txr, tto, tti in act:
        b = base.get(nid)
        if b is None:
            continue
        # delta activity is always after the snapshot, so its last block/time replace the snapshot values
        tgt = (lb, lt, (b[2] or 0) + int(txs or 0), (b[3] or 0) + int(txr or 0), (b[4] or 0) + int(tto or 0), (b[5] or 0) + int(tti or 0))
        if tuple(cur.get(nid, ())) != tgt:
            out.append(tgt + (ch, nid))
    return out


def write_chain(g, a, ch, asof, as_of_t, as_of_iso, v1, ids, bs_up, st_up, q_up, nd_up):
    """Write phase (caller holds the galaxy.db lock): small retried transactions, then meta / index.json / manifest.
    Every statement sets absolute values, so re-running after a partial failure is safe."""
    g.execute("PRAGMA busy_timeout=3000")

    def batched(sql, rows):
        emlock.batched(g, sql, rows, n=500, what=f"galaxy {ch}", log=log)
    batched("""UPDATE balance_sheets SET liquid=?, supplied=?, collateral=?, debt=?, lp=?, perps=?, net_worth=?, positions_json=?
               WHERE chain=? AND node_id=?""", [r for r in bs_up if r[9] in v1])
    batched("""INSERT OR REPLACE INTO balance_sheets(liquid, supplied, collateral, debt, lp, perps, net_worth, positions_json, chain, node_id, as_of)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""", [r + (as_of_t,) for r in bs_up if r[9] not in v1])
    # every sheet of this chain now describes the as-of block (rows without a v3 position are kept with as_of updated:
    # v3 recomputes the full sheet universe, so a v1 sheet missing from v3 means the wallet no longer holds positions)
    batched("UPDATE balance_sheets SET as_of=? WHERE chain=? AND node_id=?", [(as_of_t, ch, nid) for nid in ids])
    batched("UPDATE stories SET net_worth=? WHERE chain=? AND node_id=?", st_up)
    batched("INSERT OR REPLACE INTO bs_quality VALUES (?,?,?,?,?,?)", q_up)
    # node activity since the snapshot (map delta): snapshot base + cumulative delta, only rows that differ
    batched("""UPDATE nodes SET last_block=?, last_seen=?, tx_out=?, tx_in=?, token_out=?, token_in=?
               WHERE chain=? AND node_id=?""", nd_up)
    json.dump({"nodes_written": len(nd_up), "as_of_block": asof["as_of_block"], "mode": "absolute (galaxy_nodes_base.parquet + delta)"},
              open(f"{ROOT}/{ch}/v3/galaxy_nodes_applied.json.tmp", "w"))
    os.replace(f"{ROOT}/{ch}/v3/galaxy_nodes_applied.json.tmp", f"{ROOT}/{ch}/v3/galaxy_nodes_applied.json")
    log(f"[{ch}] node activity written for {len(nd_up)} galaxy wallets")

    def meta_tx(c):
        prev = (c.execute("SELECT value FROM meta WHERE key='last_updated'").fetchone() or [""])[0]
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_updated', ?)", (max(as_of_iso, prev),))
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"as_of_{ch}", json.dumps(asof)))
    emlock.retry(lambda: emlock.tx(g, meta_tx), what=f"galaxy {ch} meta", log=log)
    # index.json: stories top list carries net_worth_usd
    ix_path = os.path.join(os.path.dirname(a.db), "data", ch, "index.json")
    if os.path.exists(ix_path):
        ix = json.load(open(ix_path, encoding="utf-8"))
        nw = {r[2]: r[0] for r in st_up}
        for s in ix.get("stories", []):
            if s.get("node_id") in nw:
                s["net_worth_usd"] = nw[s["node_id"]]
        ix["last_updated"] = as_of_iso
        ix["as_of_block"] = asof["as_of_block"]
        ix["snapshot_block"] = asof.get("snapshot_block")
        tmp = ix_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ix, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, ix_path)
    mpath = os.path.join(os.path.dirname(a.db), "data", "manifest.json")
    if os.path.exists(mpath):
        m = json.load(open(mpath, encoding="utf-8"))
        m.setdefault("as_of", {})[ch] = {"block": asof["as_of_block"], "utc": as_of_iso}
        m["last_updated"] = max(m.get("last_updated") or "", as_of_iso)
        with open(mpath + ".tmp", "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(mpath + ".tmp", mpath)
    log(f"[{ch}] applied (as of {as_of_iso}, block {asof['as_of_block']})")


if __name__ == "__main__":
    main()
