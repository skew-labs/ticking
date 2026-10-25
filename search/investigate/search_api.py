"""Unified search for the explorer (search/1): wallets, transactions, tokens / markets, protocols and entity names in one box.

GET /search?q=<text>&limit=8[&chain=]
  0x + 64 hex  transaction: looked up on every chain's RPC (chain, from, to, block)
  0x + 40 hex  wallet: the label on every chain's graph-loop core (category, entity, source); a token or venue at that address too
  0x prefix    wallets by address prefix (galaxy stars)
  text         tokens and markets (reviewed EVM catalog: symbol, name), protocols (directory names), entity names (labels), named
               institutions (research labels: name or alias -> type "entity", the entity page), with a close-match fallback for
               typos on symbols and protocol names
  An address that belongs to a named institution (research labels) is named by it first, with its grade (official / structural /
  estimated) and source.
Labels say where they come from and whether they are verified (a decoded registry / public registry / on-chain fact) or
inferred (classifier, propagation, agent): never a confidence number. Indexes are in memory, rebuilt in the background every
10 minutes; a query never waits on a rebuild.
"""
import concurrent.futures
import difflib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

import duckdb

EMC = "/srv/skew/em-charts"
GLX = "/srv/skew/em-graphloop/export"
CATALOG = os.environ.get("EM_SEARCH_CATALOG", "/srv/skew/tick-market-ui-20261007/server/evm_catalog.json")
KNOWN = "/srv/skew/em-refresh/labels/known_entities.csv"     # public-source names (treasuries, foundations, issuers)
CHAIN_OF_ID = {"42161": "arbitrum", "4663": "robinhood", "5042": "arc"}
GALAXY = "http://127.0.0.1:8890"
CHAINS = ("arbitrum", "robinhood", "arc")
GLSERVE = {"robinhood": (8710, 8712), "arbitrum": (8711, 8713), "arc": (8714,)}
RPC = {"arbitrum": ["https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"],
       "robinhood": ["https://robinhood-rpc.publicnode.com", "https://rpc.mainnet.chain.robinhood.com"],
       "arc": ["https://arc-rpc.publicnode.com", "https://rpc.mainnet.arc.io"]}
CHAIN_NAME = {"arbitrum": "Arbitrum One", "robinhood": "Robinhood Chain", "arc": "Arc"}
# Search scopes follow the registry. Physical label/RPC readers below remain
# capability-specific; accepting a scope never asserts indexed wallet coverage.
def search_registry():
    try:
        with open(os.environ.get("EM_CHAIN_REGISTRY", "/srv/skew/em-ticking-bridge/registry/chains.json")) as f:
            rows = json.load(f)["chains"]
        return {r["id"]:r.get("name") or r["id"] for r in rows if isinstance(r,dict) and re.fullmatch(r"[a-z][a-z0-9]{1,15}",r.get("id",""))}
    except (OSError,ValueError,KeyError,TypeError):
        return dict(CHAIN_NAME)
SEARCH_CHAIN_NAMES = search_registry()
SEARCH_CHAINS = tuple(SEARCH_CHAIN_NAMES)
CHAIN_NAME.update(SEARCH_CHAIN_NAMES)

VERIFIED_LABEL = {"registry", "public_registry"}          # graph-loop sources that are decoded / published facts
VERIFIED_NAMING = {"onchain", "registry", "evidence", "docs"}   # directory naming evidence (fallback = unnamed venue)
REBUILD_S = 600
TX = re.compile(r"^0x[0-9a-f]{64}$")
ADDR = re.compile(r"^0x[0-9a-f]{40}$")
UA = {"User-Agent": "em-search", "Content-Type": "application/json"}
_idx = {"at": 0, "tokens": [], "protocols": [], "entities": [], "vocab": {}}
_lock = threading.Lock()
_building = threading.Event()
_initial_lock = threading.Lock()


def short(a):
    return a[:6] + "…" + a[-4:] if a and len(a) > 12 else a


def pretty(name):
    """'aave_v3:addresses_provider' -> 'Aave V3 · addresses provider'; names written by people are kept as they are."""
    if " " in name and ":" not in name:
        return name
    head, _, tail = name.partition(":")
    head = " ".join(w.upper() if re.fullmatch(r"v\d+", w) else w.capitalize() for w in head.split("_"))
    return head + (" · " + tail.replace("_", " ") if tail else "")


# ---------------------------------------------------------------------------------------------------------------- indexes
def build():
    tokens = []
    try:
        cat = json.load(open(CATALOG))
        for m in cat.get("markets") or []:
            tokens.append({"chain": m["network"], "id": m["id"], "symbol": m.get("symbol") or "", "name": m.get("name") or "",
                           "kind": m.get("kind"), "category": m.get("category"), "address": (m.get("address") or "").lower(),
                           "weight": m.get("usdVolumeAllTime") or 0, "basis": m.get("selection_basis"),
                           "reviewed": bool(m.get("reviewed_at"))})
    except (OSError, ValueError):
        pass
    protocols = []
    for ch in CHAINS:
        p = f"{EMC}/data/{ch}/analytics/analytics.sqlite3"
        if not os.path.exists(p):
            continue
        try:
            row = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5).execute(
                "SELECT json FROM directory WHERE key = 'protocols'").fetchone()
        except sqlite3.Error:
            row = None
        for r in (json.loads(row[0])["rows"] if row else []):
            src = next((e.get("source") for e in r.get("evidence") or [] if e.get("source")), None)
            protocols.append({"chain": ch, "id": r["id"], "name": r["name"], "kind": r["kind"], "aggregate": r.get("aggregate"),
                              "liquidityUsd": r.get("liquidityUsd"), "volume7dUsd": r.get("volume7dUsd"), "users7d": r.get("users7d"),
                              "source": src, "verified": src in VERIFIED_NAMING,
                              "evidence": [e.get("text") for e in r.get("evidence") or [] if e.get("text")][:2],
                              "weight": (r.get("liquidityUsd") or 0) + (r.get("volume7dUsd") or 0)})
    entities = []
    c = duckdb.connect()
    c.execute("SET threads=1; SET memory_limit='1GB'")
    for ch in CHAINS:
        p = f"{GLX}/{ch}/labels.parquet"
        if not os.path.exists(p):
            continue
        try:
            for name, n, top, cat_, src in c.execute(f"""
                    SELECT entity_name, count(*), list('0x' || lower(hex(address)) ORDER BY importance DESC)[1:5],
                           arg_max(category, importance), arg_max(source, importance)
                    FROM '{p}' WHERE entity_name IS NOT NULL AND entity_name <> '' GROUP BY 1""").fetchall():
                entities.append({"chain": ch, "name": name, "title": pretty(name.replace(" (sibling)", "")), "wallets": n,
                                 "addresses": top, "category": cat_, "source": src, "verified": src in VERIFIED_LABEL,
                                 "weight": n})
        except duckdb.Error as e:
            print("search index: labels %s: %r" % (ch, e), flush=True)
    # names published by the projects themselves (known_entities.csv: chain, address, entity_name, category, kind, is_contract, source URL)
    try:
        import csv
        have = {(e["chain"], e["name"]) for e in entities}
        for r in csv.DictReader(open(KNOWN)):
            ch = CHAIN_OF_ID.get(r["chain"])
            if ch and (ch, r["entity_name"]) not in have:
                entities.append({"chain": ch, "name": r["entity_name"], "title": r["entity_name"], "wallets": 1, "addresses": [r["address"].lower()],
                                 "category": r["category"], "source": "public_registry", "verified": True, "weight": 1,
                                 "url": r.get("source")})
            else:
                for e in entities:
                    if (e["chain"], e["name"]) == (ch, r["entity_name"]):
                        e["url"] = r.get("source")
    except (OSError, KeyError):
        pass
    # quote assets (USDC, USDG, WETH, WBTC, ...): not markets of their own, so not in the catalog
    for ch in CHAINS:
        p = f"{EMC}/data/{ch}/candles.duckdb"
        if not os.path.exists(p):
            continue
        try:
            cc = duckdb.connect()
            cc.execute(f"ATTACH '{p}' AS cd (READ_ONLY)")
            listed = {t["address"] for t in tokens if t["chain"] == ch}
            for tok, sym, kind in cc.execute("SELECT token, symbol, kind FROM cd.anchors").fetchall():
                if tok != "0x0000000000000000000000000000000000000000" and tok not in listed:   # a catalog market wins
                    tokens.append({"chain": ch, "id": f"{ch}:{tok}", "symbol": sym, "name": f"{sym} · quote asset ({kind})", "kind": "anchor",
                                   "category": "quote asset", "address": tok, "weight": 1e15, "basis": "em-charts anchor table",
                                   "reviewed": True, "anchor": True})
            cc.close()
        except duckdb.Error:
            continue
    vocab = {}
    for t in tokens:
        vocab.setdefault(t["symbol"].lower(), []).append(("token", t))
    for pr in protocols:
        if pr["source"] != "fallback":
            vocab.setdefault(pr["name"].lower(), []).append(("protocol", pr))
    with _lock:
        _idx.update({"at": time.time(), "tokens": tokens, "protocols": protocols, "entities": entities, "vocab": vocab})


def index():
    """The current indexes; the first call builds them, later ones trigger a background rebuild when they are old."""
    if not _idx["at"]:
        with _initial_lock:
            if not _idx["at"]:
                build()
    elif time.time() - _idx["at"] > REBUILD_S and not _building.is_set():
        _building.set()

        def run():
            try:
                build()
            finally:
                _building.clear()
        threading.Thread(target=run, daemon=True).start()
    with _lock:
        return dict(_idx)


# ---------------------------------------------------------------------------------------------------------------- lookups
def http_json(url, timeout, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def rpc(chain, method, params, timeout=2.0):
    for u in RPC.get(chain, ()):
        try:
            d = http_json(u, timeout, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "result" in d:
                return d["result"]
        except Exception:
            continue
    return None


def gl_label(chain, a):
    for port in GLSERVE.get(chain, ()):
        try:
            d = http_json(f"http://127.0.0.1:{port}/labels?addr={a}", 0.6)
            if not d.get("error"):
                return d
        except Exception:
            continue
    return None


def label_of(d):
    src = d.get("source")
    return {"text": d.get("entity") or (d.get("category") or "").replace("_", " "), "category": d.get("category"), "source": src,
            "verified": src in VERIFIED_LABEL}


def wallet_rows(a, chains):
    with concurrent.futures.ThreadPoolExecutor(len(chains)) as ex:
        found = dict(zip(chains, ex.map(lambda ch: gl_label(ch, a), chains)))
    rows = []
    for ch, d in found.items():
        if not d:
            continue
        lab = label_of(d)
        rows.append({"type": "wallet", "chain": ch, "id": f"{ch}:{a}", "title": pretty(d["entity"]) if d.get("entity") else short(a),
                     "subtitle": "%s · %s" % ((d.get("category") or "unclassified").replace("_", " "), CHAIN_NAME[ch]),
                     "label": lab, "route": f"#/address/{a}?chain={ch}", "score": 1.0, "weight": d.get("importance") or 0})
    if len(rows) > 1:   # the same address on several chains: one multi-chain row first
        rows.insert(0, {"type": "wallet", "chain": None, "id": a, "title": short(a),
                        "subtitle": "on " + ", ".join(CHAIN_NAME[r["chain"]] for r in rows), "route": f"#/address/{a}",
                        "score": 1.1, "weight": 0})
    if not rows:
        target = chains[0] if len(chains) == 1 else None
        rows.append({"type":"wallet", "chain":target, "id":f"{target}:{a}" if target else a, "title":short(a),
                     "subtitle":"Open this exact address; indexed activity and balances are not established by search",
                     "coverage":"lookup_target_only", "route":f"#/address/{a}" + (f"?chain={target}" if target else ""),
                     "score":0.5,"weight":0})
    return rows


def tx_rows(h, chains):
    with concurrent.futures.ThreadPoolExecutor(len(chains)) as ex:
        found = dict(zip(chains, ex.map(lambda ch: rpc(ch, "eth_getTransactionByHash", [h]), chains)))
    rows = []
    for ch, t in found.items():
        if not t:
            continue
        frm, to = (t.get("from") or "").lower(), (t.get("to") or "").lower()
        bn = int(t["blockNumber"], 16) if t.get("blockNumber") else None
        rows.append({"type": "tx", "chain": ch, "id": h, "title": short(h),
                     "subtitle": "%s → %s · %s" % (short(frm), short(to) if to else "contract creation",
                                                     ("block {:,}".format(bn) if bn is not None else "pending") + " · " + CHAIN_NAME[ch]),
                     "evidence": [f"eth_getTransactionByHash on {CHAIN_NAME[ch]}"], "from": frm, "to": to or None, "block": bn,
                     "label": {"text": "transaction", "source": "chain", "verified": True},
                     "route": f"#/tx/{ch}/{h}", "score": 1.0, "weight": 0})
    if not rows:
        rows.append({"type": "tx", "chain": None, "id": h, "title": short(h), "subtitle": "No transaction returned by the configured readers for " + ", ".join(CHAIN_NAME.get(ch,ch) for ch in chains),
                     "route": None, "score": 0.3, "weight": 0})
    return rows


def token_row(t, score):
    return {"type": "token", "chain": t["chain"], "id": t["id"], "title": t["symbol"],
            "subtitle": "%s · %s" % (t["name"], CHAIN_NAME.get(t["chain"], t["chain"])),
            "label": {"text": t.get("category") or t.get("kind"), "source": "anchor table" if t.get("anchor") else
                      "reviewed catalog" if t["reviewed"] else "catalog", "verified": t["reviewed"]},
            "evidence": [t["basis"]] if t.get("basis") else [],
            "route": f"#/address/{t['address']}?chain={t['chain']}" if t.get("anchor") else f"#/console/workspace/{t['id']}",
            "score": score, "weight": t["weight"]}


def usd(v):
    if v is None:
        return None
    return "$%.1fB" % (v / 1e9) if v >= 1e9 else "$%.1fM" % (v / 1e6) if v >= 1e6 else "$%.0fK" % (v / 1e3) if v >= 1e3 else "$%.0f" % v


def protocol_row(p, score):
    bits = [p["kind"]] + ([usd(p["liquidityUsd"]) + " liquidity"] if p.get("liquidityUsd") else []) + (
        [usd(p["volume7dUsd"]) + " 7d volume"] if p.get("volume7dUsd") else [])
    return {"type": "protocol", "chain": p["chain"], "id": p["id"], "title": p["name"],
            "subtitle": " · ".join(bits + [CHAIN_NAME.get(p["chain"], p["chain"])]),
            "label": {"text": p["kind"], "source": p["source"], "verified": p["verified"]}, "evidence": p["evidence"],
            "route": f"#/protocol/{p['chain']}/{p['id']}", "score": score, "weight": p["weight"]}


def entity_row(e, score, q):
    one = len(e["addresses"]) == 1 or e["wallets"] == 1
    return {"type": "wallet", "chain": e["chain"], "id": e["chain"] + ":" + (e["addresses"][0] if one else e["name"]), "title": e["title"],
            "subtitle": "%s · %s · %s" % ((e["category"] or "").replace("_", " "), "1 wallet" if e["wallets"] == 1 else "{:,} wallets".format(e["wallets"]),
                                          CHAIN_NAME.get(e["chain"], e["chain"])),
            "label": {"text": e["name"], "category": e["category"], "source": e["source"], "verified": e["verified"],
                      **({"url": e["url"]} if e.get("url") else {})},
            "addresses": e["addresses"],
            "route": f"#/address/{e['addresses'][0]}?chain={e['chain']}" if one else "#/search?" + urllib.parse.urlencode({"q": e["name"]}),
            "score": score, "weight": e["weight"]}


def research_rows(a, chains):
    """The research labels' entity of this address (any chain the labels cover), ahead of the graph-loop label."""
    import entities_api
    rows = []
    try:
        found = entities_api.address_entities(a)
    except Exception as e:
        print("search: research labels: %r" % (e,), flush=True)
        found = []
    for r in found:
        if r["chain"] not in chains:
            continue
        rows.append({"type": "wallet", "chain": r["chain"], "id": f"{r['chain']}:{a}", "title": r["entity_name"],
                     "subtitle": "%s · %s · %s · %s" % (r["entity_type"], r["role"].replace("_", " "), r["gradeText"],
                                                        CHAIN_NAME.get(r["chain"], r["chain"].capitalize())),
                     "label": {"text": r["entity_name"], "category": r["entity_type"], "source": r["source"], "verified": r["verified"],
                               "grade": r["grade"], "gradeText": r["gradeText"], "url": r["source_url"]},
                     "entity": {"id": r["entity_id"], "route": "#/entity/" + r["entity_id"]},
                     "route": f"#/address/{a}?chain={r['chain']}", "score": 1.15 if r["verified"] else 1.12, "weight": 0})
    return rows


def entity_page_row(sc, e):
    h = e["holdings"]
    n = sum((e.get("per_chain") or {}).values())
    bits = [e["type"], "{:,} addresses on {} chains".format(n, len(e.get("per_chain") or {}))]
    if h["totalUsd"] is not None:
        bits.append(usd(h["totalUsd"]) + " in balance sheets" + (" (partial)" if h["partial"] else ""))
    off = (e.get("per_grade") or {}).get("official", 0)
    return {"type": "entity", "chain": None, "id": e["entity_id"], "title": e["name"], "subtitle": " · ".join(bits),
            "label": {"text": e["type"], "source": "research labels", "verified": off > 0,
                      "grades": e.get("per_grade")},
            "route": "#/entity/" + e["entity_id"], "score": sc + 0.05, "weight": n}


def text_score(q, *fields):
    """1.0 exact, 0.9 prefix, 0.75 word prefix, 0.5 substring, 0 otherwise (case-insensitive)."""
    best = 0.0
    for f in fields:
        f = (f or "").lower()
        if not f:
            continue
        if f == q:
            return 1.0
        if f.startswith(q):
            best = max(best, 0.9)
        elif any(w.startswith(q) for w in re.split(r"[\s·:/_()-]+", f)):
            best = max(best, 0.75)
        elif len(q) >= 3 and q in f:
            best = max(best, 0.5)
    return best


def search(qs):
    q = (qs.get("q") or "").strip()
    if not q:
        return 400, {"detail": "q is required."}
    if len(q) > 100:
        return 400, {"detail": "q is at most 100 characters."}
    lim = min(20, max(1, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 8
    chain = (qs.get("chain") or "").lower() or None
    if chain and chain not in SEARCH_CHAINS:
        return 400, {"detail":"chain must be a registered EVM chain id."}
    chains = (chain,) if chain else SEARCH_CHAINS
    ql = q.lower()
    t0 = time.time()
    if TX.match(ql):
        rows = tx_rows(ql, chains)
    elif ADDR.match(ql):
        rows = research_rows(ql, chains) + wallet_rows(ql, chains)
        ix = index()
        rows += [token_row(t, 1.2) for t in ix["tokens"] if t["address"] == ql and t["chain"] in chains]   # a token address: the token first
    elif ql.startswith("0x") and re.fullmatch(r"0x[0-9a-f]{4,39}", ql):
        rows = []
        try:
            d = http_json(f"{GALAXY}/api/search?" + urllib.parse.urlencode({"q": ql, "limit": lim, **({"chain": chain} if chain else {})}), 0.25)
            for r in d.get("results") or []:
                rows.append({"type": "wallet", "chain": r["chain"], "id": f"{r['chain']}:{r['address']}",
                             "title": pretty(r["entity_name"]) if r.get("entity_name") else short(r["address"]),
                             "subtitle": "%s · %s" % ((r.get("category") or "unclassified").replace("_", " "), CHAIN_NAME.get(r["chain"], r["chain"])),
                             "label": {"text": (r.get("category") or "").replace("_", " "), "category": r.get("category"), "source": "galaxy map",
                                       "verified": False},
                             "route": f"#/address/{r['address']}?chain={r['chain']}", "score": 0.9, "weight": r.get("score") or 0})
        except Exception:
            pass
        ix = index()
        rows += [token_row(t, 0.85) for t in ix["tokens"] if t["address"].startswith(ql) and t["chain"] in chains]
    else:
        ix = index()
        rows = []
        for t in ix["tokens"]:
            if t["chain"] in chains:
                s = text_score(ql, t["symbol"], t["name"])
                if s:
                    rows.append(token_row(t, s + 0.02))          # tokens first on ties: the box is mostly used for tickers
        for p in ix["protocols"]:
            if p["chain"] in chains and not (p["source"] == "fallback" and len(ql) < 4):
                s = text_score(ql, p["name"], p["id"])
                if s:
                    rows.append(protocol_row(p, s))
        for e in ix["entities"]:
            if e["chain"] in chains:
                s = text_score(ql, e["name"], e["title"])
                if s:
                    rows.append(entity_row(e, s - 0.05, ql))
        try:
            import entities_api
            rows += [entity_page_row(sc, e) for sc, e in entities_api.match(ql, chain)]   # "binance" -> the entity page first
        except Exception as e:
            print("search: research entities: %r" % (e,), flush=True)
        if not rows and len(ql) >= 3:                            # typos: close matches on symbols and protocol names
            seen = {r["id"] for r in rows}
            for w in difflib.get_close_matches(ql, list(ix["vocab"]), n=5, cutoff=0.75):
                for kind, obj in ix["vocab"][w]:
                    if obj["chain"] in chains and obj["id"] not in seen:
                        r = token_row(obj, 0.4) if kind == "token" else protocol_row(obj, 0.4)
                        r["didYouMean"] = obj["symbol"] if kind == "token" else obj["name"]
                        rows.append(r)
                        seen.add(obj["id"])
    rows.sort(key=lambda r: (-r["score"], -(r.get("weight") or 0)))
    out = []
    for r in rows[:lim]:
        r.pop("weight", None)
        r["score"] = round(r["score"], 2)
        out.append(r)
    if not out:
        notes_empty = ["no wallet, token, protocol, institution or transaction matches; named institutions cover the exchanges in the "
                       "research labels so far (others' wallets are labelled by behaviour as cex deposit / hot wallet, without a name)"]
    else:
        notes_empty = []
    return 200, {"schema": "search/1", "q": q, "results": out, "tookMs": round(1000 * (time.time() - t0), 1),
                 "notes": ["labels: verified = a decoded registry or public registry fact; inferred = classifier, propagation or agent label",
                           "Solana markets are not included here (use /api/public/markets)"] + notes_empty}


ROUTES = {"/search": search}
