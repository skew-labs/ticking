"""Named institutions for the explorer (entities/1, entity/1): find an exchange or fund by name, open its page (sources, addresses
by grade and role, holdings per chain), and name an address that belongs to one.

GET /entities?q=<name or alias> | ?type=<exchange…>[&chain=]  [&limit=]   (list mode without q: by holdings on that chain, desc)
GET /entity?id=<entity_id>[&chain=][&grade=official|strong|candidate][&role=][&sort=grade|networth][&limit=][&cursor=]
Source: the research labels (em-research/labels/out: entities.parquet, addresses.parquet; rebuilt by the labels job, which
overwrites both files when it adds an entity). Grades: official = published by the entity itself (a signed proof-of-reserves
wallet), strong = structural (on-chain behaviour ties it to official wallets, e.g. a deposit address that sweeps into them),
candidate = estimated (listed, not confirmed on chain). Holdings per chain are the econ balance sheets joined to the addresses,
precomputed when either side changes (never per request); only addresses in a chain's sheet universe have a sheet, and the
counts say how many. Addresses are cached in a small DuckDB file next to this module, rebuilt on the same trigger.
"""
import json
import os
import re
import threading
import time

import duckdb

RL = "/srv/skew/em-research/labels/out"
ECON = "/srv/skew/em-data/econ"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "research_labels.duckdb")
# econ balance sheet per chain: v3 (verified net worth) where the refresh runs it, else the 6-VM chains' v1 sheets
SHEETS = {"arbitrum": (f"{ECON}/arbitrum/v3/sheet_summary.v3.parquet", "econ v3 (verified net worth)"),
          "robinhood": (f"{ECON}/robinhood/v3/sheet_summary.v3.parquet", "econ v3 (verified net worth)"),
          "ethereum": (f"{ECON}/ethereum/balance_sheet_summary.parquet", "econ v1 (6-VM chain; many token prices pending)"),
          "linea": (f"{ECON}/linea/balance_sheet_summary.parquet", "econ v1 (6-VM chain; many token prices pending)"),
          "optimism": (f"{ECON}/optimism/balance_sheet_summary.parquet", "econ v1 (6-VM chain; many token prices pending)")}
GRADE = {"official": (0, "official", True, "published by the entity itself (signed proof-of-reserves wallet)"),
         "strong": (1, "structural", False, "tied by on-chain behaviour to the entity's official wallets"),
         "candidate": (2, "estimated", False, "estimated: listed by the source, not confirmed on chain")}
CHAIN_RE = re.compile(r"[a-z][a-z0-9]{1,15}")
CURSOR_RE = re.compile(r"([0-2])\.([a-z][a-z0-9]{1,15})\.(0x[0-9a-f]{40})")
OFFSET_RE = re.compile(r"o(\d{1,7})")   # sort=networth pages by offset
REBUILD_S = 600
_st = {"key": None, "at": 0, "entities": [], "balances": {}, "con": None}
_lock = threading.Lock()
_building = threading.Event()


def _sources():
    return [f"{RL}/entities.parquet", f"{RL}/addresses.parquet"] + [p for p, _ in SHEETS.values() if os.path.exists(p)]


def _key():
    return tuple((p, os.path.getmtime(p)) for p in _sources() if os.path.exists(p))


def build():
    key = _key()
    if not any(p.endswith("addresses.parquet") for p, _ in key):
        with _lock:
            _st.update({"key": key, "at": time.time(), "entities": [], "balances": {}})
        return
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = duckdb.connect(tmp)
    c.execute("SET threads=2; SET memory_limit='2GB'; SET preserve_insertion_order=false; SET enable_progress_bar=false")
    ranks = " ".join(f"WHEN '{g}' THEN {v[0]}" for g, v in GRADE.items())
    c.execute(f"""CREATE TABLE ra AS SELECT CASE grade {ranks} ELSE 3 END AS rank, chain, address, entity_id, entity_name, entity_type,
                         role, grade, source, source_url, as_of, evidence_json::VARCHAR AS evidence
                  FROM read_parquet('{RL}/addresses.parquet') ORDER BY entity_id, rank, chain, address""")
    c.execute("CREATE TABLE ents AS SELECT * FROM read_parquet(?)", [f"{RL}/entities.parquet"])
    parts, nw = [], []
    for ch, (p, basis) in SHEETS.items():
        if not os.path.exists(p):
            continue
        cols = {d[0] for d in c.execute(f"DESCRIBE SELECT * FROM read_parquet('{p}')").fetchall()}
        unp = "coalesce(s.unpriced_rows, 0)" if "unpriced_rows" in cols else "0"
        parts.append(f"""SELECT r.entity_id, '{ch}' AS chain, '{basis}' AS basis, count(*) AS addresses, count(s.wallet) AS with_sheet,
                                sum(s.net_worth_usd) AS net_usd, sum(s.liquid_usd) AS liquid_usd, sum({unp}) AS unpriced_rows
                         FROM ra r LEFT JOIN read_parquet('{p}') s ON s.wallet = r.address WHERE r.chain = '{ch}' GROUP BY 1""")
        nw.append(f"""SELECT r.chain, r.address, s.net_worth_usd AS net_worth FROM ra r JOIN read_parquet('{p}') s ON s.wallet = r.address
                      WHERE r.chain = '{ch}'""")
    # each address's own sheet net worth (only addresses inside a sheet universe: small), for netWorthUsd and sort=networth
    c.execute("CREATE TABLE ra_nw AS " + (" UNION ALL ".join(nw) if nw else "SELECT NULL::VARCHAR AS chain, NULL::VARCHAR AS address, NULL::DOUBLE AS net_worth WHERE false"))
    bal = {}
    for row in (c.execute(" UNION ALL ".join(parts)).fetchall() if parts else []):
        eid, ch, basis, n, ws, net, liq, unp = row
        bal.setdefault(eid, {})[ch] = {"usd": None if net is None else round(net, 2), "liquidUsd": None if liq is None else round(liq, 2),
                                       "addresses": n, "withSheet": ws, "unpricedRows": int(unp or 0), "basis": basis,
                                       "asOf": int(os.path.getmtime(SHEETS[ch][0]))}
    ents = []
    for r in c.execute("SELECT * FROM ents").fetchall():
        d = dict(zip([x[0] for x in c.description], r))
        for k in ("aliases", "sources", "per_chain", "per_grade"):
            try:
                d[k] = json.loads(d[k]) if isinstance(d.get(k), str) else d.get(k)
            except ValueError:
                pass
        ents.append(d)
    c.execute("CREATE INDEX ra_addr ON ra (address)")
    c.close()
    os.replace(tmp, CACHE)
    con = duckdb.connect(CACHE, read_only=True)
    with _lock:
        _st.update({"key": key, "at": time.time(), "entities": ents, "balances": bal, "con": con})   # the old connection is left to GC


def state():
    """The current tables; the first call builds them, later ones rebuild in the background when a source file changed."""
    if _st["con"] is None and not _st["at"]:
        build()
    elif time.time() - _st["at"] > REBUILD_S and not _building.is_set() and _key() != _st["key"]:
        _building.set()

        def run():
            try:
                build()
            except Exception as e:
                print("entities: rebuild failed: %r" % (e,), flush=True)
            finally:
                _building.clear()
        threading.Thread(target=run, daemon=True).start()
    with _lock:
        return dict(_st)


def _rows(st, sql, args):
    if st["con"] is None:
        return []
    cur = st["con"].cursor()
    try:
        res = cur.execute(sql, args)
        cols = [d[0] for d in res.description]
        return [dict(zip(cols, r)) for r in res.fetchall()]
    finally:
        cur.close()


def grade_of(g):
    rank, text, verified, why = GRADE.get(g, (3, g or "unknown", False, ""))
    return {"grade": g, "gradeText": text, "verified": verified, "gradeWhy": why}


def holdings(st, eid):
    per = st["balances"].get(eid, {})
    priced = [v["usd"] for v in per.values() if v["usd"] is not None]
    return {"perChain": per, "totalUsd": round(sum(priced), 2) if priced else None,
            "asOf": max((v["asOf"] for v in per.values()), default=None),
            "partial": any(v["unpricedRows"] for v in per.values()) or any(v["withSheet"] < v["addresses"] for v in per.values()),
            "chainsWithoutSheets": sorted(set((next((e["per_chain"] for e in st["entities"] if e["entity_id"] == eid), {}) or {})) - set(per))}


def summary(st, e):
    return {"entity_id": e["entity_id"], "name": e["name"], "type": e["type"], "website": e.get("website"),
            "per_chain": e.get("per_chain"), "per_grade": e.get("per_grade"), "holdings": holdings(st, e["entity_id"])}


def match(q, chain=None):
    """[(score, entity summary)] for a name / alias query (case-insensitive: exact 1.0, prefix 0.9, substring 0.6)."""
    st, ql, out = state(), q.strip().lower(), []
    for e in st["entities"]:
        names = [e["name"] or "", e["entity_id"] or ""] + [a for a in (e.get("aliases") or []) if isinstance(a, str)]
        s = max((1.0 if n.lower() == ql else 0.9 if n.lower().startswith(ql) else 0.6 if len(ql) >= 3 and ql in n.lower() else 0.0)
                for n in names) if ql else 0.0
        if s and (not chain or (e.get("per_chain") or {}).get(chain)):
            out.append((s, summary(st, e)))
    return sorted(out, key=lambda x: -x[0])


def address_entities(a):
    """Research rows of one address (every chain): the explorer names the wallet with these before any inferred label."""
    st = state()
    return [dict(r, **grade_of(r["grade"])) for r in _rows(st, "SELECT chain, address, entity_id, entity_name, entity_type, role, grade, "
                                                           "source, source_url, as_of FROM ra WHERE address = ? ORDER BY rank, chain", [a])]


def entities(qs):
    q = (qs.get("q") or "").strip()
    typ, ch = (qs.get("type") or "").lower(), (qs.get("chain") or "").lower()
    if len(q) > 100 or (not q and not typ and not ch):
        return 400, {"detail": "q is 1-100 characters (an entity name or alias), or list by type and / or chain without q."}
    if ch and not CHAIN_RE.fullmatch(ch):
        return 400, {"detail": "chain is a chain name such as arbitrum or ethereum."}
    if typ and not re.fullmatch(r"[a-z_]{1,32}", typ):
        return 400, {"detail": "type is an entity type such as exchange."}
    lim = min(50, max(1, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 10
    if q:
        found = [dict(s, score=round(sc, 2)) for sc, s in match(q, ch or None) if not typ or s["type"] == typ]
    else:   # list mode: every entity of that type / on that chain, by holdings there (all chains without chain=), desc
        st = state()
        found = [summary(st, e) for e in st["entities"] if (not typ or e["type"] == typ) and (not ch or (e.get("per_chain") or {}).get(ch))]
        key = lambda s: (s["holdings"]["perChain"].get(ch, {}).get("usd") if ch else s["holdings"]["totalUsd"]) or 0
        found.sort(key=lambda s: -key(s))
    rows = found[:lim]
    return 200, {"schema": "entities/1", "q": q or None, "filter": {"type": typ or None, "chain": ch or None}, "rows": rows,
                 "notes": ["Named entities from the research labels (exchanges first). An entity listed here has at least one address its "
                           "own publication names (official); the others are structural or estimated, per address.",
                           "holdings: econ balance sheets of its addresses per chain; withSheet < addresses means part of them has no sheet."]}


def entity(qs):
    st = state()
    eid = (qs.get("id") or "").lower()
    e = next((x for x in st["entities"] if x["entity_id"] == eid), None)
    if e is None:
        return 404, {"detail": "No entity with that id (search /entities?q= for one)."}
    ch, grade, role = (qs.get("chain") or "").lower(), (qs.get("grade") or "").lower(), (qs.get("role") or "").lower()
    sort = (qs.get("sort") or "grade").lower()
    if sort not in ("grade", "networth"):
        return 400, {"detail": "sort must be grade or networth."}
    if ch and not CHAIN_RE.fullmatch(ch):
        return 400, {"detail": "chain is a chain name such as arbitrum or ethereum."}
    if grade and grade not in GRADE:
        return 400, {"detail": "grade must be official, strong or candidate."}
    if role and not re.fullmatch(r"[a-z_]{1,32}", role):
        return 400, {"detail": "role is a role name such as por_wallet or deposit."}
    lim = min(200, max(1, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 50
    where, args = ["r.entity_id = ?"], [eid]
    for col, v in (("chain", ch), ("grade", grade), ("role", role)):
        if v:
            where.append(f"r.{col} = ?")
            args.append(v)
    cur, off = qs.get("cursor") or "", 0
    if cur and sort == "grade":
        m = CURSOR_RE.fullmatch(cur)
        if not m:
            return 400, {"detail": "cursor is the nextCursor of the previous page."}
        where.append("(r.rank, r.chain, r.address) > (?, ?, ?)")
        args += [int(m.group(1)), m.group(2), m.group(3)]
    elif cur:
        m = OFFSET_RE.fullmatch(cur)
        if not m:
            return 400, {"detail": "cursor is the nextCursor of the previous page."}
        off = int(m.group(1))
    # sort=networth: addresses with a balance sheet first, largest net worth first, then the grade order
    order = "n.net_worth DESC NULLS LAST, r.rank, r.chain, r.address" if sort == "networth" else "r.rank, r.chain, r.address"
    rows = _rows(st, f"""SELECT r.*, n.net_worth FROM ra r LEFT JOIN ra_nw n ON n.chain = r.chain AND n.address = r.address
                         WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?""", args + [lim + 1, off])
    more = len(rows) > lim
    rows = rows[:lim]
    nw = {(r["chain"], r["address"]): r["net_worth"] for r in rows}
    addrs = []
    for r in rows:
        try:
            ev = json.loads(r["evidence"]) if r["evidence"] else None
        except ValueError:
            ev = r["evidence"]
        if isinstance(ev, dict):
            ev.pop("signature", None)   # the signature stays in the source file; the page says it was verified
        addrs.append({"chain": r["chain"], "address": r["address"], "role": r["role"], **grade_of(r["grade"]), "source": r["source"],
                      "source_url": r["source_url"], "as_of": r["as_of"], "evidence": ev,
                      "netWorthUsd": None if nw.get((r["chain"], r["address"])) is None else round(nw[(r["chain"], r["address"])], 2),
                      "route": f"#/address/{r['address']}?chain={r['chain']}"})
    last = rows[-1] if rows else None
    return 200, {"schema": "entity/1", "entity": {k: e.get(k) for k in ("entity_id", "name", "type", "aliases", "website")},
                 "sources": e.get("sources"), "totals": {"per_chain": e.get("per_chain"), "per_grade": e.get("per_grade")},
                 "holdings": holdings(st, eid),
                 "filter": {"chain": ch or None, "grade": grade or None, "role": role or None, "sort": sort},
                 "addresses": addrs,
                 "nextCursor": (f"o{off + lim}" if sort == "networth" else f"{last['rank']}.{last['chain']}.{last['address']}") if more and last else None,
                 "notes": ["Addresses are ordered official, structural, estimated, then by chain and address; sort=networth puts the "
                           "addresses with a balance sheet first, largest net worth first.",
                           "netWorthUsd: the address's econ balance sheet on its chain (null when the chain or the address has none).",
                           "holdings.partial: some addresses have no sheet or some positions have no price (pending, cannot estimate); "
                           "the totals cover priced positions only."]}


ROUTES = {"/entities": entities, "/entity": entity}
