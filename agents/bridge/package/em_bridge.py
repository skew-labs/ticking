#!/usr/bin/env python3
"""em-ticking-bridge: Economic Machine data for Ticking (Arbitrum One + Robinhood Chain). Stdlib + sqlite, read-only.

Arbitrum One and Robinhood Chain markets are first-class Ticking networks: their catalog rows, candles, tapes and
streams are served by the console API and the engine bridge (network = arbitrum | robinhood, market <network>:<0x>).
This bridge only adds the wallet layer (production: --mode production, 127.0.0.1:8896, published by api.ticking.dev nginx):
    GET /api/public/em/tools/<tool>          wallet | holders | top | trace | trades | tx   (cards for workspace + assistant)
    GET /api/public/em/g/<path>              allow-listed, re-validated em-galaxy reads (127.0.0.1:8890)
    GET /api/public/em/icon/<chain>/<addr>   self-hosted artwork redirect or monogram fallback
    GET /api/public/em/health
Markets are the reviewed rows of the console's evm_catalog.json (--evm-catalog); em-charts reads carry the caller's
address in X-Forwarded-For so its per-client limits apply to people, not to this proxy.
Staging (--mode staging) additionally serves the staging build, passes other GET /api/* to the console app and the
resolver POST to the staging resolver. Every upstream call is a GET to a loopback service, except the staging resolver
POST and the JSON-RPC eth_getTransactionByHash reads of the tx tool. No credentials are read; responses never echo headers.
"""
import argparse
import csv
import http.client
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADDR = re.compile(r"^0x[0-9a-f]{40}$")
TX = re.compile(r"^0x[0-9a-f]{64}$")
MARKET = re.compile(r"^(arbitrum|robinhood):0x[0-9a-f]{40}$")
INTERVAL = re.compile(r"^[1-9][0-9]{0,2}[smhdw]$")
SEARCH = re.compile(r"^[\w .:\-]{1,64}$")
CHAINS = ("arbitrum", "robinhood")
VENUE = {"robinhood": ("robinhood", "Robinhood Chain"), "arbitrum": ("arbitrum", "Arbitrum One")}
EXPLORER = {"robinhood": "https://robinhoodchain.blockscout.com", "arbitrum": "https://arbiscan.io"}
RPC = {"robinhood": ["https://rpc.mainnet.chain.robinhood.com", "https://robinhood.drpc.org"],
       "arbitrum": ["https://arb1.arbitrum.io/rpc", "https://arbitrum.drpc.org"]}
CATEGORY = {
    "eoa_user": "Wallet", "eoa_whale": "Whale", "mev_bot": "MEV bot", "dex_pool": "DEX pool", "dex_router": "DEX router",
    "token_contract": "Token contract", "other_contract": "Contract", "contract_other": "Contract", "lending": "Lending market",
    "bridge": "Bridge", "cex_hot_wallet": "Exchange hot wallet", "cex_deposit": "Exchange deposit", "multisig_or_safe": "Multisig / Safe",
    "nft_contract": "NFT contract", "unknown": "Unclassified"}
PROTOCOL = {"morpho_blue": "Morpho Blue", "aave_v3": "Aave V3", "uniswap_v4": "Uniswap V4", "uniswap_v3": "Uniswap V3",
            "gmx_v2": "GMX V2", "gmx_v1": "GMX V1", "compound_v3": "Compound V3", "dolomite": "Dolomite"}
STABLE = {"USDG", "USDC", "USDT", "USDT0", "DAI", "USDE", "USDCE", "FRAX", "SYRUPUSDG"}
CFG = {}
REQ = threading.local()
LOG = ACCESS = sys.stderr


def log(msg):
    LOG.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg + "\n")


class HTTPError(Exception):
    def __init__(self, code, msg, retry=None):
        super().__init__(msg)
        self.code, self.msg, self.retry = code, msg, retry


def r2(v):
    try:
        v = float(v)
        return round(v, 2) if v == v and abs(v) != float("inf") else None
    except (TypeError, ValueError):
        return None


def pname(p):
    if not p:
        return None
    if p in PROTOCOL:
        return PROTOCOL[p]
    if ":" in p:
        a, b = p.split(":", 1)
        return "%s (%s)" % (a.replace("_", " ").title(), b[:8])
    return p.replace("_", " ").title()


WORDS = {"layerzero": "LayerZero", "morpho": "Morpho", "aave": "Aave", "univ2": "Uniswap V2", "univ3": "Uniswap V3", "univ4": "Uniswap V4", "uniswap": "Uniswap", "v2": "V2", "v3": "V3", "v4": "V4",
         "dex": "DEX", "lp": "LP", "usdg": "USDG", "mev": "MEV", "npm": "NPM", "oft": "OFT", "cex": "CEX", "eoa": "EOA", "gmx": "GMX"}


REGISTRY_KEY = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+(?::[a-z0-9_]+|:0x[0-9a-f]{4,})?\b")


def human(name):
    """Registry keys to reader labels: 'uniswap_v4:pool_manager' -> 'Uniswap V4 · Pool manager', 'morpho_blue:singleton' -> 'Morpho Blue'."""
    if not name or not isinstance(name, str):
        return None
    if name in PROTOCOL:
        return PROTOCOL[name]
    parts = []
    for i, part in enumerate(name.strip().split(":")):
        words = [w for w in re.split(r"[_\s]+", part.strip()) if w]
        if not words or (i and words == ["singleton"]):
            continue
        out = []
        for j, w in enumerate(words):
            if re.fullmatch(r"0x[0-9a-fA-F]+", w):
                out.append(w[:8] + ("…" if len(w) > 8 else ""))
            elif w.lower() in WORDS:
                out.append(WORDS[w.lower()])
            else:
                out.append(w.capitalize() if j == 0 or (i == 0 and len(words) <= 3 and w.lower() not in ("fork", "pool", "adapter", "router", "manager")) else w.lower())
        parts.append(" ".join(out))
    label = " · ".join(parts)
    return PROTOCOL.get(label, label) or None


# ------------------------------------------------------------------------------------------------
# Cache with request coalescing, and per-client token buckets

class Cache:
    def __init__(self, cap=4000):
        self.lock, self.cap, self.data, self.flight = threading.Lock(), cap, OrderedDict(), {}

    def get(self, key, ttl, fn, wait=30):
        now = time.time()
        with self.lock:
            hit = self.data.get(key)
            if hit and now - hit[0] < ttl:
                self.data.move_to_end(key)
                return hit[1]
            ev = self.flight.get(key)
            owner = ev is None
            if owner:
                ev = self.flight[key] = threading.Event()
        if not owner:
            ev.wait(wait)
            with self.lock:
                hit = self.data.get(key)
            if hit:
                return hit[1]
            raise HTTPError(503, "Upstream is busy; try again.")
        try:
            val = fn()
            with self.lock:
                self.data[key] = (time.time(), val)
                self.data.move_to_end(key)
                while len(self.data) > self.cap:
                    self.data.popitem(last=False)
            return val
        finally:
            with self.lock:
                self.flight.pop(key, None)
            ev.set()


CACHE = Cache()


class Limiter:
    RULES = {"tools": (40, 20), "data": (600, 120), "stream": (30, 10), "icon": (1200, 200), "page": (6000, 1000)}  # per minute, burst

    def __init__(self):
        self.lock, self.b = threading.Lock(), {}

    def take(self, ip, kind):
        rate, burst = self.RULES[kind]
        now = time.time()
        with self.lock:
            tokens, at = self.b.get((ip, kind), (burst, now))
            tokens = min(burst, tokens + (now - at) * rate / 60.0)
            if tokens < 1:
                self.b[(ip, kind)] = (tokens, now)
                raise HTTPError(429, "Too many requests. Slow down and retry shortly.", retry=max(1, int((1 - tokens) * 60 / rate) + 1))
            self.b[(ip, kind)] = (tokens - 1, now)
            if len(self.b) > 50000:
                self.b = {k: v for k, v in self.b.items() if now - v[1] < 600}


LIMIT = Limiter()


# ------------------------------------------------------------------------------------------------
# Upstreams (loopback)

def hostport(s):
    h, p = s.rsplit(":", 1)
    return h, int(p)


def fetch(hp, path, timeout=20, headers=None):
    c = http.client.HTTPConnection(*hp, timeout=timeout)
    try:
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.getheader("Content-Type", ""), r.read(20_000_000)
    except OSError as e:
        raise HTTPError(502, "Upstream %s:%d unavailable (%s)." % (hp[0], hp[1], type(e).__name__))
    finally:
        c.close()


def fetch_json(hp, path, timeout=20, headers=None):
    code, _, b = fetch(hp, path, timeout, headers)
    if code != 200:
        try:
            d = json.loads(b)
            msg = d.get("detail") or d.get("error")
        except ValueError:
            msg = None
        raise HTTPError(404 if code == 404 else 502, msg if isinstance(msg, str) and code in (400, 404) else "Upstream error %d." % code)
    return json.loads(b)


def galaxy(path, ttl=60):
    return CACHE.get("g:" + path, ttl, lambda: fetch_json(CFG["galaxy"], "/api/" + path))


def charts(path, ttl=10):
    ip = getattr(REQ, "ip", None)
    return CACHE.get("c:" + path, ttl, lambda: fetch_json(CFG["charts"], path, headers={"X-Forwarded-For": ip} if ip else None))


_PROBE = {}


def charts_has(path, query):
    """em-charts endpoints added by other work land over time; probe once per 5 min and keep the fallback until then."""
    hit = _PROBE.get(path)
    if hit and time.time() - hit[0] < (300 if hit[1] else 30):   # a restarting upstream (503) is retried within 30 s
        return hit[1]
    try:
        code, _, b = fetch(CFG["charts"], path + "?" + query, timeout=8)
        ok = code == 200 or (code in (400, 404) and b"Not served" not in b and b"Unknown path" not in b)
    except HTTPError:
        ok = False
    _PROBE[path] = (time.time(), ok)
    return ok


# ------------------------------------------------------------------------------------------------
# Reviewed EVM markets (the console's evm_catalog.json; exact network + address identities)

def load_icons():
    p = CFG.get("icons")
    try:
        with open(p) as f:
            d = json.load(f)
        return d.get("icons", d)
    except (OSError, ValueError, TypeError):
        return {}


class Catalog:
    TTL = 60

    def __init__(self):
        self.lock = threading.Lock()
        self.at, self.stamp, self.rows, self.by_id, self.by_em, self.by_sym = 0, None, [], {}, {}, {}

    def refresh(self):
        if self.rows and time.time() - self.at < self.TTL:
            return
        with self.lock:
            try:
                st = os.stat(CFG["evm_catalog"])
                if self.rows and (st.st_ino, st.st_mtime_ns) == self.stamp:
                    self.at = time.time()
                    return
                with open(CFG["evm_catalog"]) as f:
                    markets = json.load(f)["markets"]
            except (OSError, ValueError, KeyError) as e:
                log("evm catalog unavailable: %s" % type(e).__name__)
                return
            icons = load_icons()
            rows, by_id, by_em, by_sym = [], {}, {}, {}
            for t in markets:
                mid, ch, addr = t.get("id"), t.get("network"), str(t.get("mint") or "")
                if not MARKET.match(mid or "") or mid != "%s:%s" % (ch, addr):
                    continue
                icon = icons.get(mid)
                row = {"id": mid, "symbol": t.get("symbol") or "TOKEN", "name": t.get("name") or t.get("symbol"), "network": ch, "mint": addr,
                       "icon": icon["path"] if isinstance(icon, dict) else "/api/public/em/icon/%s/%s" % (ch, addr)}
                em = {"chain": ch, "address": addr, "emId": mid, "kind": t.get("kind"), "row": row}
                rows.append(row)
                by_id[mid], by_em[(ch, addr)] = em, mid
                by_sym.setdefault((ch, re.sub(r"[^A-Z0-9]", "", row["symbol"].upper())), mid)
            self.rows, self.by_id, self.by_em, self.by_sym, self.at, self.stamp = rows, by_id, by_em, by_sym, time.time(), (st.st_ino, st.st_mtime_ns)
            log("catalog: %d reviewed EVM markets" % len(rows))

    def get(self, mid):
        if not MARKET.match(mid or ""):
            return None
        self.refresh()
        return self.by_id.get(mid)

    def market_for(self, chain, address):
        self.refresh()
        return self.by_em.get((chain, (address or "").lower()))

    def by_symbol(self, chain, symbol):
        self.refresh()
        sym = re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())
        return None if sym in STABLE else self.by_sym.get((chain, sym))


CATALOG = Catalog()


# ------------------------------------------------------------------------------------------------
# Market summaries from galaxy.db (one pass, refreshed every 30 min) and focus CSVs

class Summaries:
    def __init__(self):
        self.lock, self.at, self.flows, self.venues, self.recent, self.movers, self.asof = threading.Lock(), 0, {}, {}, {}, {}, {}

    def ensure(self):
        if time.time() - self.at > 1800 and not self.lock.locked():
            threading.Thread(target=self.build, daemon=True).start()

    def build(self):
        """One streaming pass over econ_actions (no window functions): flows, venues, the 40 latest actions and the 12 largest
        movers for every catalog symbol. About 1 s per million rows at nice 10; refreshed every 30 min."""
        with self.lock:
            t0 = time.time()
            for _ in range(60):
                CATALOG.refresh()
                if CATALOG.rows:
                    break
                time.sleep(2)
            want = {(r["network"], re.sub(r"[^A-Z0-9]", "", r["symbol"].upper())) for r in CATALOG.rows}
            try:
                import heapq
                con = sqlite3.connect("file:%s?mode=ro" % CFG["galaxy_db"], uri=True, timeout=10)
                con.execute("PRAGMA query_only=1")
                flows, venues, recent, vol, asof = defaultdict(lambda: [0, 0.0]), {}, defaultdict(list), defaultdict(lambda: defaultdict(lambda: [0.0, 0])), {}
                cur = con.execute("SELECT chain, asset_symbol, time, protocol, action, amount, usd, node_id, tx_hash FROM econ_actions")
                while True:
                    rows = cur.fetchmany(20000)
                    if not rows:
                        break
                    for ch, sym, t, p, act, amt, u, nid, tx in rows:
                        if t and t > asof.get(ch, 0):
                            asof[ch] = t
                        key = (ch, re.sub(r"[^A-Z0-9]", "", (sym or "").upper()))
                        if key not in want:
                            continue
                        u = u if isinstance(u, (int, float)) and 0 <= u < 3e9 else None
                        f = flows[key + (act,)]
                        f[0] += 1
                        f[1] += u or 0
                        v = venues.setdefault(key, {}).setdefault(p, {"n": 0, "usd": 0.0, "acts": defaultdict(float)})
                        v["n"] += 1
                        v["usd"] += u or 0
                        v["acts"][act] += u or 0
                        h = recent[key]
                        item = (t or 0, nid or 0, p, act, amt, u, tx)
                        if len(h) < 40:
                            heapq.heappush(h, item)
                        elif item[0] > h[0][0]:
                            heapq.heapreplace(h, item)
                        if nid is not None and u:
                            m = vol[key][nid]
                            m[0] += u
                            m[1] += 1
                nodes = defaultdict(set)
                movers = {}
                for key, per in vol.items():
                    top = sorted(per.items(), key=lambda kv: -kv[1][0])[:12]
                    movers[key] = top
                    nodes[key[0]].update(n for n, _ in top)
                for key, h in recent.items():
                    nodes[key[0]].update(x[1] for x in h if x[1])
                info = {}
                for ch, ids in nodes.items():
                    ids = list(ids)
                    for k in range(0, len(ids), 900):
                        part = ids[k:k + 900]
                        for nid, addr, cat, ent in con.execute("SELECT node_id, address, category, entity_name FROM nodes WHERE chain=? AND node_id IN (%s)"
                                                               % ",".join("?" * len(part)), [ch] + part):
                            info[(ch, nid)] = (addr, cat, ent)
                con.close()
                F, V, R, M = defaultdict(list), {}, {}, {}
                for (ch, sym, act), (n, u) in flows.items():
                    F[(ch, sym)].append({"action": act, "n": n, "usd": r2(u)})
                for key, ps in venues.items():
                    V[key] = sorted(({"protocol": pname(p), "n": v["n"], "usd": r2(v["usd"]), "action": max(v["acts"], key=v["acts"].get) if v["acts"] else None}
                                     for p, v in ps.items()), key=lambda x: -(x["usd"] or 0))[:8]
                for key, h in recent.items():
                    R[key] = [{"time": t, "protocol": pname(p), "action": act, "amount": amt if isinstance(amt, (int, float)) else None, "usd": u, "nodeId": nid or None,
                               "address": info.get((key[0], nid), (None,))[0], "category": info.get((key[0], nid), (None, None))[1],
                               "tx": tx} for t, nid, p, act, amt, u, tx in sorted(h, reverse=True)]
                for key, top in movers.items():
                    M[key] = [{"address": info[(key[0], nid)][0], "nodeId": nid, "category": info[(key[0], nid)][1], "entity": info[(key[0], nid)][2],
                               "usd": r2(u), "n": n} for nid, (u, n) in top if (key[0], nid) in info]
                self.flows, self.venues, self.recent, self.movers, self.asof, self.at = dict(F), V, R, M, asof, time.time()
                log("summaries built in %.1fs: %d symbols" % (time.time() - t0, len(F)))
            except sqlite3.Error as e:
                log("summaries failed: %r" % e)
                self.at = time.time() - 1800 + 120


SUMMARIES = Summaries()


def focus_rows(chain, name):
    def read():
        try:
            with open(os.path.join(CFG["focus"], chain, name + ".csv"), newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except OSError:
            return []
    return CACHE.get("focus:%s:%s" % (chain, name), 600, read)


# ------------------------------------------------------------------------------------------------
# Tools

def find_wallet(address, chain=None):
    res = galaxy("search?" + urllib.parse.urlencode({"q": address, "limit": 4}))
    hits = [r for r in res.get("results", []) if (r.get("address") or "").lower() == address and (not chain or r.get("chain") == chain)]
    return hits[0] if hits else None


def need_addr(qs, key="address"):
    a = (qs.get(key) or "").strip()
    if not ADDR.match(a.lower()) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", a):
        raise HTTPError(400, "A full 0x address (40 hex characters) is required.")
    return a.lower()


def opt_chain(qs):
    c = qs.get("chain")
    if c in (None, ""):
        return None
    if c not in CHAINS:
        raise HTTPError(400, "chain must be arbitrum or robinhood.")
    return c


def tool_wallet(qs):
    addr, chain = need_addr(qs), opt_chain(qs)
    hit = find_wallet(addr, chain)
    if hit:
        chain = hit["chain"]
    card = {"kind": "wallet", "address": addr, "chain": chain, "inGalaxy": bool(hit and hit.get("in_map"))}
    node = None
    if hit and hit.get("node_id") is not None and hit.get("in_map"):
        node = galaxy("node/%s/%d" % (chain, int(hit["node_id"])))
    else:
        for ch in ([chain] if chain else list(CHAINS[::-1])):
            try:
                node = galaxy("wallet/%s/%s" % (ch, addr), ttl=300)
            except HTTPError:
                continue
            chain = card["chain"] = ch
            break
    if node is None:
        raise HTTPError(404, "This wallet is not in the Economic Machine map of Arbitrum One or Robinhood Chain.")
    label = node.get("label") if isinstance(node.get("label"), dict) else {}
    card["nodeId"] = node.get("node_id")
    card["category"] = node.get("category") or label.get("category") or "unknown"
    card["categoryName"] = CATEGORY.get(card["category"], str(card["category"]).replace("_", " ").title())
    card["entity"] = human(node.get("entity_name"))
    card["protocol"] = pname(node.get("protocol"))
    for k in ("tx_in", "tx_out", "first_seen", "last_seen", "degree"):
        if isinstance(node.get(k), (int, float)):
            card[k] = node[k]
    bs = node.get("balance_sheet") or {}
    if isinstance(bs, dict) and bs:
        card["sheet"] = {k: r2(bs.get(k)) for k in ("net_worth", "liquid", "supplied", "collateral", "debt", "lp", "perps") if bs.get(k)}
        card["asOf"] = bs.get("as_of")
        pos = [p for p in (bs.get("positions") or []) if isinstance(p, dict)]
        card["positions"] = [{"kind": p.get("kind"), "protocol": pname(p.get("protocol")), "asset": p.get("asset"), "amount": r2(p.get("amount")),
                              "usd": r2(p.get("usd")), "flag": p.get("q")} for p in sorted(pos, key=lambda p: -abs(p.get("usd") or 0))[:8]]
        flags = sorted({p["flag"] for p in card["positions"] if p.get("flag")} | set(node.get("quality_flags") or []))
        if bs.get("unverified") or node.get("unverified"):
            flags.append("unverified")
        if flags:
            card["flags"] = flags
    if hit and hit.get("in_map") and node.get("node_id") is not None:
        try:
            act = galaxy("activity/%s/%d?limit=12" % (chain, int(node["node_id"])))
            card["recent"] = [{"time": t.get("time"), "text": t.get("en"), "usd": r2(t.get("usd")), "tx": t.get("tx_hash")} for t in (act.get("txs") or [])[:5]]
            if isinstance(act.get("story"), dict):
                card["headline"] = act["story"].get("headline_en")
        except HTTPError:
            pass
    for p in card.get("positions") or []:
        mid = CATALOG.by_symbol(chain, p.get("asset"))
        if mid:
            card["market"] = mid
            break
    if not card.get("market"):
        for t in (node.get("top_tokens") or [])[:6]:
            mid = CATALOG.by_symbol(chain, t.get("symbol") if isinstance(t, dict) else None)
            if mid:
                card["market"] = mid
                break
    if card.get("market") in CATALOG.by_id:
        card["marketSymbol"] = CATALOG.by_id[card["market"]]["row"]["symbol"]
    card["explorer"] = EXPLORER.get(chain)
    return card


def tool_holders(qs):
    em = CATALOG.get(qs.get("market") or "")
    if not em and qs.get("symbol") and not qs.get("market"):
        chains = [qs["chain"]] if qs.get("chain") in CHAINS else ["robinhood", "arbitrum"]
        em = next((CATALOG.get(m) for m in (CATALOG.by_symbol(ch, qs["symbol"]) for ch in chains) if m), None)
    if not em:
        raise HTTPError(404, "Unknown market. Wallet data covers Arbitrum One and Robinhood Chain markets.")
    SUMMARIES.ensure()
    ch, sym = em["chain"], re.sub(r"[^A-Z0-9]", "", em["row"]["symbol"].upper())
    tok = next((r for r in focus_rows(ch, "focus_stock_tokens") if (r.get("token") or "").lower() == em["address"]), None)
    holders = []
    if tok:
        rows = sorted((r for r in focus_rows(ch, "focus_stock_top_holders") if (r.get("token") or "").lower() == em["address"]),
                      key=lambda r: int(r.get("rank") or 999))[:15]
        for r in rows:
            a = (r.get("wallet") or "").lower()
            if not ADDR.match(a):
                continue
            h = {"address": a, "share": r2(float(r.get("share_of_supply") or 0) * 100)}
            try:
                hit = find_wallet(a, ch)
            except HTTPError:
                hit = None
            if hit:
                h.update(nodeId=hit.get("node_id"), category=hit.get("category"), entity=human(hit.get("entity_name")))
            h["categoryName"] = CATEGORY.get(h.get("category") or "unknown", h.get("category"))
            holders.append(h)
    key = (ch, sym)
    movers = [dict(m, categoryName=CATEGORY.get(m.get("category") or "unknown", m.get("category")),
                   entity=human(m.get("entity"))) for m in SUMMARIES.movers.get(key, []) if m.get("address")]
    return {"kind": "holders", "market": em["row"]["id"], "chain": ch, "symbol": em["row"]["symbol"], "token": em["address"],
            "icon": em["row"]["icon"], "holderCount": int(float(tok["holders"])) if tok and tok.get("holders") else None,
            "holders": holders, "movers": movers, "flows": sorted(SUMMARIES.flows.get(key, []), key=lambda f: -(f["usd"] or 0))[:8],
            "venues": SUMMARIES.venues.get(key, []), "recent": SUMMARIES.recent.get(key, [])[:12], "asOf": SUMMARIES.asof.get(ch),
            "ready": bool(SUMMARIES.at)}


_TOP = {"at": 0, "rows": None, "lock": threading.Lock()}


def tool_top(qs):
    sym = re.sub(r"[^A-Z0-9]", "", (qs.get("symbol") or "").upper())[:16]
    with _TOP["lock"]:
        if _TOP["rows"] is None or time.time() - _TOP["at"] > 600:
            con = sqlite3.connect("file:%s?mode=ro" % CFG["galaxy_db"], uri=True, timeout=10)
            try:
                rows = []
                asof = con.execute("SELECT max(as_of) FROM balance_sheets WHERE chain='robinhood'").fetchone()[0]
                for node_id, nw, pj, addr, cat in con.execute(
                        "SELECT b.node_id, b.net_worth, b.positions_json, n.address, n.category FROM balance_sheets b "
                        "JOIN nodes n ON n.chain=b.chain AND n.node_id=b.node_id WHERE b.chain='robinhood' AND b.debt != 0"):
                    try:
                        pos = json.loads(pj or "[]")
                    except ValueError:
                        continue
                    coll, usdg = {}, 0.0
                    for p in pos:
                        if not isinstance(p, dict) or p.get("q"):
                            continue
                        a = str(p.get("asset") or "").upper()
                        if p.get("kind") == "collateral" and p.get("usd"):
                            coll[a] = coll.get(a, 0) + float(p["usd"])
                        if p.get("kind") == "debt" and a == "USDG":
                            usdg += abs(float(p.get("usd") or 0))
                    if usdg > 0 and coll:
                        rows.append({"address": addr, "nodeId": node_id, "category": cat, "netWorth": r2(nw), "usdgDebt": r2(usdg),
                                     "collateral": {k: r2(v) for k, v in sorted(coll.items(), key=lambda kv: -kv[1])}})
            finally:
                con.close()
            _TOP.update(at=time.time(), rows=rows, asof=asof)
        rows = _TOP["rows"]
    if sym:
        rows = sorted((r for r in rows if sym in r["collateral"]), key=lambda r: -r["collateral"][sym])
    else:
        rows = sorted(rows, key=lambda r: -sum(r["collateral"].values()))
    out = []
    for r in rows[:10]:
        out.append(dict(r, categoryName=CATEGORY.get(r["category"] or "unknown", r["category"]),
                        market=CATALOG.by_symbol("robinhood", sym or next(iter(r["collateral"]), None))))
    return {"kind": "top", "title": ("Wallets borrowing USDG against %s" % sym) if sym else "Wallets borrowing USDG against stock tokens",
            "symbol": sym or None, "chain": "robinhood", "rows": out, "count": len(rows),
            "asOf": _TOP.get("asof"),
            "note": "Morpho Blue positions from Economic Machine balance sheets%s. Quality-flagged positions are excluded." %
                    (time.strftime(" (as of %d %b %Y %H:%M UTC)", time.gmtime(_TOP["asof"])) if _TOP.get("asof") else "")}


def tool_trace(qs):
    addr, chain = need_addr(qs), opt_chain(qs)
    hit = find_wallet(addr, chain)
    if not hit or not hit.get("in_map"):
        card = tool_wallet(qs)
        return {"kind": "trace", "address": addr, "chain": card.get("chain"), "counterparties": [], "wallet": card,
                "note": "This wallet is outside the drawn map; its full-map card is shown instead."}
    nb = galaxy("neighbors/%s/%d?limit=10" % (hit["chain"], int(hit["node_id"])))
    cps = [{"address": n.get("address"), "nodeId": n.get("node_id"), "category": n.get("category"),
            "categoryName": CATEGORY.get(n.get("category") or "unknown", n.get("category")), "entity": human(n.get("entity_name")),
            "direction": n.get("dir"), "usd": r2(n.get("usd")), "n": n.get("n"), "assets": (n.get("assets") or [])[:3]} for n in (nb.get("neighbors") or [])[:10]]
    return {"kind": "trace", "address": addr, "chain": hit["chain"], "counterparties": cps}


def tool_trades(qs):
    """A wallet's actions on one EM market for chart markers. em-charts wallet-trades gives buys/sells with side plus collateral and
    borrow actions (attributed to tx.from); the galaxy's decoded actions fill what it misses (e.g. Safe wallets, older history)."""
    addr = need_addr(qs)
    em = CATALOG.get(qs.get("market") or "")
    if not em:
        raise HTTPError(400, "market must be an Arbitrum One or Robinhood Chain market id.")
    sym = em["row"]["symbol"].upper()
    items, sources = [], []
    q = urllib.parse.urlencode({"chain": em["chain"], "address": addr, "token": em["address"]})
    if charts_has("/api/public/em/wallet-trades", q):
        try:
            d = charts("/api/public/em/wallet-trades?" + q, ttl=30)
            for t in (d.get("trades") or []) + (d.get("actions") or []):
                tm = t.get("time")
                if not isinstance(tm, (int, float)):
                    continue
                tm = int(tm if tm > 1e12 else tm * 1000)
                amount = t.get("amount") if t.get("amount") is not None else t.get("base_volume")
                usdv = t.get("usd") if t.get("usd") is not None else t.get("quote_volume")
                price = t.get("price") or (usdv / amount if usdv and amount else None)
                side = t.get("side") if t.get("side") in ("B", "A") else None
                tx = str(t.get("tx") or "").lower()
                items.append({"time": tm, "action": t.get("action") or ("buy" if side == "B" else "sell" if side == "A" else "swap"), "side": side,
                              "amount": r2(amount), "usd": r2(usdv), "price": price, "text": t.get("text"), "tx": tx if TX.match(tx) else None})
            sources.append("em-charts")
        except HTTPError:
            pass
    hit = find_wallet(addr, em["chain"])
    if hit and hit.get("in_map"):
        seen = {(i["tx"], i["action"]) for i in items if i.get("tx")}
        act = galaxy("activity/%s/%d?limit=40" % (em["chain"], int(hit["node_id"])))
        for a in act.get("actions") or []:
            if str(a.get("asset") or "").upper() != sym or not a.get("amount") or not a.get("usd"):
                continue
            tx = str(a.get("tx_hash") or "").lower()
            if (tx, a.get("action")) in seen:
                continue
            items.append({"time": int(a["time"]) * 1000, "action": a.get("action"), "side": None, "amount": r2(a.get("amount")), "usd": r2(a.get("usd")),
                          "price": a["usd"] / a["amount"], "text": a.get("en"), "tx": tx if TX.match(tx) else None})
        sources.append("galaxy")
    items = sorted((i for i in items if i.get("price")), key=lambda i: i["time"])[-200:]
    return {"kind": "trades", "address": addr, "market": em["row"]["id"], "symbol": sym, "source": "+".join(sources) or "none",
            "explorer": EXPLORER[em["chain"]], "items": items}


def rpc_tx(chain, tx):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionByHash", "params": [tx]}).encode()
    last = None
    for url in RPC[chain]:
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "em-ticking-bridge/1"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read(200_000))
            if d.get("result"):
                return d["result"]
            last = "not found"
        except Exception as e:  # try the next public endpoint
            last = type(e).__name__
    raise HTTPError(404, "Transaction not found on %s (%s)." % (VENUE[chain][1], last))


def tool_tx(qs):
    """tx -> its sender's wallet card (execution inspector: from a swap to the wallet behind it)."""
    chain = opt_chain(qs)
    tx = (qs.get("tx") or "").lower()
    if not chain or not TX.match(tx):
        raise HTTPError(400, "chain and a 0x transaction hash (64 hex characters) are required.")
    t = CACHE.get("tx:%s:%s" % (chain, tx), 86400, lambda: rpc_tx(chain, tx))
    frm, to = (t.get("from") or "").lower(), (t.get("to") or "").lower()
    out = {"kind": "tx", "chain": chain, "tx": tx, "from": frm if ADDR.match(frm) else None, "to": to if ADDR.match(to) else None,
           "explorer": "%s/tx/%s" % (EXPLORER[chain], tx)}
    if out["from"]:
        try:
            out["wallet"] = tool_wallet({"address": frm, "chain": chain})
        except HTTPError as e:
            out["walletError"] = e.msg
    return out


# ------------------------------------------------------------------------------------------------
# Economic Machine market economy (panel, chart overlays, assistant). em-charts token analytics first
# (/api/public/em/token-state, liquidations, flows, events, token-brief); until a section is served there, the
# map-snapshot equivalent from em-galaxy is returned and labelled as such. Every section carries status + as-of.

EM_WINDOWS = ("1h", "24h", "7d")
BUCKET = {"dex_pool": "dex_liquidity", "dex_router": "dex_liquidity", "lending": "lending_collateral", "cex_hot_wallet": "exchanges",
          "cex_deposit": "exchanges", "bridge": "bridges", "eoa_whale": "whales", "eoa_user": "retail", "mev_bot": "retail",
          "multisig_or_safe": "vaults_contracts", "other_contract": "vaults_contracts", "contract_other": "vaults_contracts",
          "nft_contract": "vaults_contracts", "token_contract": "issuer", "unknown": "unknown"}
BUCKET_LABEL = {"lending_collateral": "Posted as lending collateral", "dex_liquidity": "DEX liquidity", "exchanges": "Exchanges",
                "whales": "Whales", "retail": "Wallets", "bridges": "Bridges", "vaults_contracts": "Vaults & contracts",
                "issuer": "Issuer", "unknown": "Unclassified", "other": "Rest of supply"}
EVENT_OF_ACTION = {"supply_collateral": "collateral_in", "withdraw_collateral": "collateral_out", "borrow": "borrow", "repay": "repay",
                   "liquidate": "liquidation", "liquidation": "liquidation", "bridge_in": "bridge_in", "bridge_out": "bridge_out",
                   "lp_add": "lp_add", "lp_remove": "lp_remove", "supply": "collateral_in", "withdraw": "collateral_out"}


def clean(v, depth=0):
    """Re-serialisable, bounded copy of an upstream document: finite numbers, short strings, capped lists."""
    if depth > 6:
        return None
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v if v == v and abs(v) != float("inf") else None
    if isinstance(v, str):
        return v[:300]
    if isinstance(v, list):
        return [clean(x, depth + 1) for x in v[:500]]
    if isinstance(v, dict):
        return {str(k)[:64]: clean(x, depth + 1) for k, x in list(v.items())[:200]}
    return None


def em_get(path, query, ttl=60):
    q = urllib.parse.urlencode(query)
    if not charts_has(path, q):
        return None
    try:
        return clean(charts(path + "?" + q, ttl=ttl))
    except HTTPError:
        return None


def section(live, snapshot, asof_live=None, asof_snap=None):
    if live is not None:
        return {"status": "live", "source": "em-charts token analytics", "asOf": asof_live or live.get("asOf"), "data": live}
    if snapshot:
        return {"status": "snapshot", "source": "Economic Machine map snapshot", "asOf": asof_snap, "data": snapshot}
    return {"status": "pending", "source": None, "asOf": None, "data": None}


VENUE_CLASSES = {"dex", "lending", "bridge", "exchange", "vault", "issuer", "burn", "dex_pool", "dex_router", "cex_hot_wallet", "cex_deposit", "token_contract"}
VENUE_BUCKETS = {"dex_liquidity", "lending_contracts", "lending_collateral", "lending_supplied", "lending_unattributed", "perp_liquidity", "exchanges",
                 "bridges", "vaults_contracts", "issuer"}
HOLDER_CLASS = {"eoa": "Wallet", "eoa_user": "Wallet", "whale_label": "Labelled whale", "eoa_whale": "Whale", "contract": "Smart-contract wallet",
                "multisig_or_safe": "Multisig / Safe", "mev": "MEV bot", "mev_bot": "MEV bot", "retail": "Retail wallet", "whale": "Whale"}


def where_and_who(em, sym, state, leverage, flows, holders, borrowers):
    """Split holders into venues (protocol contracts: where the supply sits) and wallets (who holds, borrows or moves it)."""
    ch, live = em["chain"], (state.get("data") or {}) if state.get("status") == "live" else {}
    venues, people = {}, {}
    rows = live.get("topHolders") or [{"address": h["address"], "share": (h.get("share") or 0) / 100, "usd": None, "amount": None, "bucket": BUCKET.get(h.get("category") or "unknown"),
                                       "class": h.get("category"), "entity": h.get("entity")} for h in holders.get("holders") or []]
    for h in rows:
        a = str(h.get("address") or "").lower()
        if not ADDR.match(a):
            continue
        if h.get("class") in VENUE_CLASSES or h.get("bucket") in VENUE_BUCKETS:
            name = human(h.get("entity")) or {"dex_liquidity": "Other DEX pools", "bridges": "Bridge contracts", "exchanges": "Exchange wallets",
                                              "vaults_contracts": "Vaults & contracts"}.get(h.get("bucket")) or BUCKET_LABEL.get(h.get("bucket"), "Protocol contracts")
            v = venues.setdefault(name, {"name": name, "share": 0.0, "usd": 0.0, "contracts": 0, "address": a, "bucket": h.get("bucket")})
            v["share"] += h.get("share") or 0
            v["usd"] += h.get("usd") or 0
            v["contracts"] += 1
            continue
        people[a] = {"wallet": a, "size": h.get("usd") or 0, "kind": h.get("class"), "entity": human(h.get("entity")),
                     "role": "Holds %s %s%s" % (num_s(h.get("amount")), sym, " (%s · %.2f%% of supply)" % (usd_s(h.get("usd")), (h.get("share") or 0) * 100) if h.get("usd") else
                                               " (%.2f%% of supply)" % ((h.get("share") or 0) * 100))}
    lev = (leverage.get("data") or {}) if leverage.get("status") == "live" else {}
    debt = {}
    for p in lev.get("positions") or []:
        a = str(p.get("wallet") or "").lower()
        if ADDR.match(a):
            d = debt.setdefault(a, {"debt": 0.0, "coll": 0.0, "symbol": p.get("debtSymbol") or "USD", "protocol": human(p.get("protocol"))})
            d["debt"] += p.get("debtUsd") or 0
            d["coll"] += p.get("collateralUsd") or 0
    for b in borrowers if not lev else []:
        debt.setdefault(b["wallet"], {"debt": b.get("debtUsd") or 0, "coll": b.get("collateralUsd") or 0, "symbol": "USDG", "protocol": "Morpho Blue"})
    for a, d in debt.items():
        role = "Borrows %s %s against %s on %s (%s collateral)" % (usd_s(d["debt"]), d["symbol"], sym, d["protocol"] or "a lending market", usd_s(d["coll"]))
        w = people.setdefault(a, {"wallet": a, "size": 0, "kind": None, "entity": None, "role": None})
        w["role"] = role if not w["role"] else w["role"] + "; " + role[0].lower() + role[1:]
        w["size"] = max(w["size"], d["coll"], d["debt"])
    fl = (flows.get("data") or {}) if flows.get("status") == "live" else {}
    for t in (fl.get("topWallets") or [])[:6]:
        a = str(t.get("wallet") or "").lower()
        if ADDR.match(a) and a not in people:
            n = t.get("netUsd") or 0
            people[a] = {"wallet": a, "size": abs(n), "kind": t.get("class"), "entity": None,
                         "role": "Net %s %s of %s in %s" % ("bought" if n >= 0 else "sold", usd_s(abs(n)), sym, fl.get("window") or "24h")}
    ranked = sorted(people.values(), key=lambda w: -(w["size"] or 0))[:10]
    for w in ranked:
        if not w["entity"] and not w["kind"]:
            try:
                hit = find_wallet(w["wallet"], ch)
            except HTTPError:
                hit = None
            if hit:
                w["kind"], w["entity"] = hit.get("category"), human(hit.get("entity_name"))
        w["label"] = w["entity"] or HOLDER_CLASS.get(w["kind"] or "", CATEGORY.get(w["kind"] or "", "Unlabeled holder") if w["kind"] not in (None, "unknown") else "Unlabeled holder")
        w["category"] = w["kind"] if w["kind"] in CATEGORY else {"eoa": "eoa_user", "whale_label": "eoa_whale", "contract": "multisig_or_safe", "mev": "mev_bot"}.get(w["kind"], "unknown")
    return sorted(venues.values(), key=lambda v: -v["share"])[:8], ranked


def usd_s(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    a = abs(v)
    return ("-" if v < 0 else "") + ("$%.2fB" % (a / 1e9) if a >= 1e9 else "$%.2fM" % (a / 1e6) if a >= 1e6 else "$%.1fK" % (a / 1e3) if a >= 1e3 else "$%.0f" % a)


def num_s(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return "{:,.0f}".format(v) if abs(v) >= 100 else "{:,.4g}".format(v)


def tool_economy(qs):
    em = CATALOG.get(qs.get("market") or "")
    if not em and qs.get("symbol") and not qs.get("market"):
        chains = [qs["chain"]] if qs.get("chain") in CHAINS else ["robinhood", "arbitrum"]
        em = next((CATALOG.get(m) for m in (CATALOG.by_symbol(ch, qs["symbol"]) for ch in chains) if m), None)
    if not em:
        raise HTTPError(404, "Unknown market. The Economic Machine covers reviewed Arbitrum One and Robinhood Chain markets.")
    window = qs.get("window") or "24h"
    if window not in EM_WINDOWS:
        raise HTTPError(400, "window must be 1h, 24h or 7d.")
    mid, ch, row = em["emId"], em["chain"], em["row"]
    sym = re.sub(r"[^A-Z0-9]", "", row["symbol"].upper())
    holders = CACHE.get("t:holders:market=" + mid, 300 if SUMMARIES.at else 0, lambda: tool_holders({"market": mid}))
    snap_asof = holders.get("asOf") or SUMMARIES.asof.get(ch) or _TOP.get("asof")
    # money state: top holders by economic class (share of supply)
    buckets = {}
    for h in holders.get("holders") or []:
        b = BUCKET.get(h.get("category") or "unknown", "vaults_contracts")
        x = buckets.setdefault(b, {"key": b, "label": BUCKET_LABEL[b], "share": 0.0, "holders": 0, "protocols": {}})
        x["share"] += (h.get("share") or 0) / 100
        x["holders"] += 1
        if h.get("entity"):
            name = h["entity"].split(":")[0].title()
            x["protocols"][name] = x["protocols"].get(name, 0) + (h.get("share") or 0) / 100
    covered = sum(b["share"] for b in buckets.values())
    snap_state = None
    if buckets:
        rows = sorted(buckets.values(), key=lambda b: -b["share"])
        for b in rows:
            b["protocols"] = [{"name": k, "share": round(v, 6)} for k, v in sorted(b["protocols"].items(), key=lambda kv: -kv[1])[:4]]
            b["share"] = round(b["share"], 6)
        rows.append({"key": "other", "label": "Rest of supply (outside the top %d holders)" % len(holders.get("holders") or []),
                     "share": round(max(0.0, 1 - covered), 6), "holders": None, "protocols": []})
        snap_state = {"buckets": rows, "holders": holders.get("holderCount"), "topHolders": holders.get("holders") or [],
                      "coverage": {"notes": ["Top %d holders at the map snapshot, classified by wallet type; the rest of supply is not broken down."
                                             % len(holders.get("holders") or [])], "unpricedShare": None}}
    live_state = em_get("/api/public/em/token-state", {"market": mid})
    for b in (live_state or {}).get("buckets") or []:
        for pr in b.get("protocols") or []:
            pr["name"] = human(pr.get("name")) or pr.get("name")
    state = section(live_state, snap_state, asof_snap=snap_asof)
    # leverage: wallets borrowing against this token (liquidation prices arrive with em-charts liquidations)
    top = CACHE.get("t:top:symbol=" + sym, 300, lambda: tool_top({"symbol": sym})) if ch == "robinhood" else {"rows": []}
    borrowers = [{"wallet": r["address"], "protocol": "Morpho Blue", "collateralUsd": (r.get("collateral") or {}).get(sym), "debtUsd": r.get("usdgDebt"),
                  "debtSymbol": "USDG", "netWorth": r.get("netWorth"), "category": r.get("category"), "categoryName": r.get("categoryName")}
                 for r in top.get("rows") or [] if (r.get("collateral") or {}).get(sym)]
    live_lev = em_get("/api/public/em/liquidations", {"market": mid})
    for pos in ((live_lev or {}).get("positions") or []) + ((live_lev or {}).get("atRisk") or []):
        pos["protocol"] = human(pos.get("protocol")) or pos.get("protocol")
    leverage = section(live_lev,
                       {"borrowers": borrowers, "coverage": {"notes": ["Morpho Blue USDG borrowers with this token as collateral, from Economic Machine "
                                                                       "balance sheets. Liquidation prices need on-chain LLTVs and arrive with live analytics."]}}
                       if borrowers else None, asof_snap=top.get("asOf"))
    # flows: windowed by class when live; otherwise decoded actions over the map window
    acts = sorted(holders.get("flows") or [], key=lambda f: -(f.get("usd") or 0))
    flows = section(em_get("/api/public/em/flows", {"market": mid, "window": window}),
                    {"window": "map", "actions": acts, "coverage": {"notes": ["Decoded economic actions over the mapped history, not a %s window." % window]}}
                    if acts else None, asof_snap=snap_asof)
    venues, wallets = where_and_who(em, sym, state, leverage, flows, holders, borrowers)
    brief = em_get("/api/public/em/token-brief", {"market": mid})
    return {"kind": "economy", "market": mid, "network": ch, "chain": VENUE[ch][1], "symbol": row["symbol"], "name": row["name"], "icon": row["icon"],
            "window": window, "state": state, "leverage": leverage, "flows": flows, "venues": venues, "wallets": wallets,
            "brief": brief, "explorer": EXPLORER[ch], "generatedAt": int(time.time())}


def tool_events(qs):
    em = CATALOG.get(qs.get("market") or "")
    if not em:
        raise HTTPError(404, "Unknown market.")
    now = int(time.time())
    lo = int(qs["from"]) if str(qs.get("from", "")).isdigit() else now - 7 * 86400
    hi = int(qs["to"]) if str(qs.get("to", "")).isdigit() else now
    if not 0 <= lo < hi or hi - lo > 400 * 86400:
        raise HTTPError(400, "from/to must be unix seconds, at most 400 days apart.")
    live = em_get("/api/public/em/events", {"market": em["emId"], "from": lo, "to": hi})
    if live is not None:
        evs = [dict(e, protocol=human(e.get("protocol")), text=REGISTRY_KEY.sub(lambda m: human(m.group(0)) or m.group(0), str(e.get("text") or "")))
               for e in live.get("events") or [] if isinstance(e, dict)]
        return {"kind": "events", "market": em["emId"], "status": "live", "source": "em-charts token analytics", "events": evs, "asOf": live.get("asOf")}
    SUMMARIES.ensure()
    sym = re.sub(r"[^A-Z0-9]", "", em["row"]["symbol"].upper())
    out = []
    for r in SUMMARIES.recent.get((em["chain"], sym), []):
        kind = EVENT_OF_ACTION.get(r.get("action"))
        if not kind or not r.get("time") or not lo <= r["time"] <= hi or not (r.get("usd") or 0) > 0 and kind != "liquidation":
            continue
        out.append({"time": r["time"], "kind": kind, "usd": r.get("usd"), "amount": r.get("amount"), "wallet": r.get("address"),
                    "walletClass": r.get("category"), "tx": r.get("tx") if TX.match(str(r.get("tx") or "")) else None, "protocol": r.get("protocol"),
                    "text": "%s %s on %s" % (r.get("action", "").replace("_", " "), sym, r.get("protocol") or "a protocol")})
    return {"kind": "events", "market": em["emId"], "status": "snapshot" if out else "pending", "source": "Economic Machine map snapshot",
            "events": out, "asOf": SUMMARIES.asof.get(em["chain"])}


TOOLS = {"wallet": tool_wallet, "holders": tool_holders, "top": tool_top, "trace": tool_trace, "trades": tool_trades, "tx": tool_tx,
         "economy": tool_economy, "events": tool_events}
TOOL_TTL = {"wallet": 120, "holders": 300, "top": 300, "trace": 120, "trades": 30, "tx": 3600, "economy": 60, "events": 60}
TOOL_PARAMS = {"wallet": {"address", "chain"}, "holders": {"market", "symbol", "chain"}, "top": {"symbol"}, "trace": {"address", "chain"},
               "trades": {"address", "market"}, "tx": {"chain", "tx"}, "economy": {"market", "window", "symbol", "chain"}, "events": {"market", "from", "to"}}


# ------------------------------------------------------------------------------------------------
# Galaxy passthrough: allow-listed paths, re-validated params, rebuilt query

def galaxy_route(rest, qs):
    m = re.fullmatch(r"search", rest)
    if m:
        q = (qs.get("q") or "").strip()
        if not SEARCH.match(q):
            raise HTTPError(400, "Search with 1–64 letters, digits, spaces or . : - _")
        lim = min(20, max(1, int(qs.get("limit", "12")) if str(qs.get("limit", "12")).isdigit() else 12))
        return "search?" + urllib.parse.urlencode({"q": q, "limit": lim}), 60
    m = re.fullmatch(r"(node|neighbors|activity)/(arbitrum|robinhood)/(\d{1,9})", rest)
    if m:
        kind = m.group(1)
        if kind == "node":
            return "node/%s/%s%s" % (m.group(2), m.group(3), "?brief=1" if qs.get("brief") == "1" else ""), 120
        cap = 20 if kind == "neighbors" else 40
        lim = min(cap, max(1, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 12
        return "%s/%s/%s?limit=%d" % (kind, m.group(2), m.group(3), lim), 120
    m = re.fullmatch(r"wallet/(arbitrum|robinhood)/(0x[0-9a-fA-F]{40})", rest)
    if m:
        return "wallet/%s/%s" % (m.group(1), m.group(2).lower()), 300
    if rest == "live/status":
        return "live/status", 5
    if rest == "live/recent":
        q = {"limit": min(50, int(qs["limit"])) if str(qs.get("limit", "")).isdigit() else 20}
        if qs.get("chain") in CHAINS:
            q["chain"] = qs["chain"]
        if str(qs.get("min_usd", "")).isdigit():
            q["min_usd"] = qs["min_usd"]
        if str(qs.get("since", "")).isdigit():
            q["since"] = qs["since"]
        return "live/recent?" + urllib.parse.urlencode(q), 5
    raise HTTPError(404, "Unknown Economic Machine route.")


# ------------------------------------------------------------------------------------------------
# Artwork: self-hosted logos (built by build_assets.py) or a monogram

MONO = [("#ecebfd", "#5658e8"), ("#e7f6f1", "#17856d"), ("#fdecef", "#c76678"), ("#eef4fd", "#3b6fd6"), ("#fdf4e6", "#b7791f")]


def monogram(symbol, stock=False):
    sym = re.sub(r"[^A-Za-z0-9]", "", symbol or "?")[:4].upper() or "?"
    bg, fg = ("#1c2233", "#ffffff") if stock else MONO[sum(map(ord, sym)) % len(MONO)]
    size = 30 if len(sym) <= 2 else 24 if len(sym) == 3 else 19
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="32" fill="%s"/>'
            '<text x="32" y="33" text-anchor="middle" dominant-baseline="central" font-family="Space Grotesk,DM Sans,Arial,sans-serif" '
            'font-weight="600" font-size="%d" fill="%s" letter-spacing="-0.5">%s</text></svg>' % (bg, size, fg, sym)).encode()


# ------------------------------------------------------------------------------------------------
# HTTP

def client_ip(h):
    peer = h.client_address[0]
    if peer in ("127.0.0.1", "::1"):
        xr = (h.headers.get("X-Real-IP") or "").strip()
        if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", xr):
            return xr
    return peer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "em-ticking-bridge/1"
    sys_version = ""

    def log_message(self, *a):
        pass

    def cors(self):
        o = self.headers.get("Origin")
        return {"Access-Control-Allow-Origin": o, "Vary": "Origin"} if o and o in CFG["origins"] else {"Vary": "Origin"}

    def send_body(self, code, body, ctype="application/json", headers=None):
        if code >= 400:
            ACCESS.write("%s %d %s %s\n" % (time.strftime("%H:%M:%S"), code, self.command, self.path[:200]))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in {**self.cors(), **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code, obj, cache="no-store", extra=None):
        b = json.dumps(obj, separators=(",", ":"), allow_nan=False, default=str).encode()
        self.send_body(code, b, "application/json", {"Cache-Control": cache, **(extra or {})})

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in self.cors().items():
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        if len(self.path) > 2048 or "\x00" in path:
            return self.send_json(414, {"detail": "Request too long."})
        qs = {}
        for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items():
            if len(v) != 1 or len(k) > 32 or len(v[0]) > 200:
                return self.send_json(400, {"detail": "Invalid query."})
            qs[k] = v[0]
        try:
            if path.startswith("/api/") or path.startswith("/embed/"):
                return self.api(path, u.query, qs)
            if CFG["mode"] == "staging":
                LIMIT.take(client_ip(self), "page")
                return self.static(path)
            raise HTTPError(404, "Not found.")
        except HTTPError as e:
            self.send_json(e.code, {"detail": e.msg, "error": e.msg}, extra={"Retry-After": str(e.retry)} if e.retry else None)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("error %s %r" % (path, e))
            try:
                self.send_json(500, {"detail": "Internal error."})
            except Exception:
                pass

    def do_POST(self):
        if CFG["mode"] != "staging" or self.path.split("?")[0] != "/api/public/intelligence/resolve":
            return self.send_json(405, {"detail": "Read-only."})
        try:
            LIMIT.take(client_ip(self), "tools")
        except HTTPError as e:
            return self.send_json(429, {"error": e.msg}, extra={"Retry-After": str(e.retry)})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2048:
            return self.send_json(413, {"error": "Request is too large."})
        body = self.rfile.read(n)
        c = http.client.HTTPConnection(*CFG["resolver"], timeout=20)
        try:
            c.request("POST", "/api/public/intelligence/resolve", body=body, headers={"Content-Type": "application/json",
                      "Origin": self.headers.get("Origin") or "", "X-Real-IP": client_ip(self)})
            r = c.getresponse()
            self.send_body(r.status, r.read(400_000), r.getheader("Content-Type", "application/json"), {"Cache-Control": "no-store"})
        except OSError:
            self.send_json(503, {"error": "The assistant is not running."})
        finally:
            c.close()

    # --- API ------------------------------------------------------------------------------------
    def api(self, path, query, qs):
        ip = client_ip(self)
        REQ.ip = ip
        if path == "/api/public/em/health":
            return self.send_json(200, health())
        m = re.fullmatch(r"/api/public/em/icon/(arbitrum|robinhood)/(0x[0-9a-f]{40})", path)
        if m:
            LIMIT.take(ip, "icon")
            return self.icon(m.group(1), m.group(2))
        if path.startswith("/api/public/em/tools/"):
            name = path.rsplit("/", 1)[1]
            fn = TOOLS.get(name)
            if not fn:
                raise HTTPError(404, "Unknown tool.")
            if set(qs) - TOOL_PARAMS[name]:
                raise HTTPError(400, "Unexpected parameter.")
            LIMIT.take(ip, "tools")
            key = "t:%s:%s" % (name, urllib.parse.urlencode(sorted(qs.items())))
            ttl = TOOL_TTL[name] if (name != "holders" or SUMMARIES.at) else 0   # never cache a not-ready summary
            return self.send_json(200, CACHE.get(key, ttl, lambda: fn(qs)), cache="public, max-age=30" if ttl else "no-store")
        if path.startswith("/api/public/em/g/"):
            LIMIT.take(ip, "data")
            route, ttl = galaxy_route(path[len("/api/public/em/g/"):], qs)
            return self.send_json(200, galaxy(route, ttl), cache="public, max-age=%d" % min(ttl, 60))
        if path.startswith("/api/public/em/"):
            raise HTTPError(404, "Unknown Economic Machine route.")
        if CFG["mode"] == "staging":
            return self.passthrough(path, query)
        raise HTTPError(404, "Not served by the Economic Machine bridge.")

    def icon(self, chain, addr):
        icons = CACHE.get("icons", 300, load_icons)
        hit = icons.get("%s:%s" % (chain, addr))
        if isinstance(hit, dict) and hit.get("path", "").startswith(("/assets/", "/api/public/token-icons/")):
            return self.send_body(302, b"", "text/plain", {"Location": hit["path"], "Cache-Control": "public, max-age=3600"})
        mid = CATALOG.market_for(chain, addr)
        em = CATALOG.by_id.get(mid) if mid else None
        return self.send_body(200, monogram(em["row"]["symbol"] if em else "?", stock=bool(em and em.get("kind") == "stock")), "image/svg+xml",
                              {"Cache-Control": "public, max-age=3600"})

    # --- staging only -------------------------------------------------------------------------
    def passthrough(self, path, query):
        stream = "/stream/" in path
        c = http.client.HTTPConnection(*CFG["console"], timeout=900 if stream else 30)
        try:
            c.request("GET", self.path, headers={"Host": "api.ticking.dev", "Accept": self.headers.get("Accept") or "*/*", "X-Real-IP": client_ip(self)})
            r = c.getresponse()
        except OSError as e:
            c.close()
            raise HTTPError(502, "Console app unavailable (%s)." % type(e).__name__)
        try:
            ctype = r.getheader("Content-Type", "application/octet-stream")
            if not ctype.startswith("text/event-stream"):
                return self.send_body(r.status, r.read(), ctype, {k: v for k, v in r.getheaders() if k.lower() in ("cache-control", "etag", "retry-after")})
            self.send_response(r.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                chunk = r.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            c.close()

    def static(self, path):
        rel = path.lstrip("/") or "index.html"
        if any(s == ".." or s.startswith(".") for s in rel.split("/")):
            raise HTTPError(404, "Not found.")
        root = os.path.realpath(CFG["static"])
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            if "." in rel.rsplit("/", 1)[-1]:
                raise HTTPError(404, "Not found.")
            full = os.path.join(root, "index.html")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith((".js", ".mjs")):
            ctype = "text/javascript"
        cache = "public, max-age=31536000, immutable" if "/_app/" in full else "public, max-age=86400" if "/assets/em/" in full else "no-cache"
        gz = full + ".gz"
        if "gzip" in (self.headers.get("Accept-Encoding") or "") and os.path.isfile(gz):
            with open(gz, "rb") as f:
                body = f.read()
            return self.send_body(200, body, ctype, {"Cache-Control": cache, "Content-Encoding": "gzip"})
        with open(full, "rb") as f:
            body = f.read()
        self.send_body(200, body, ctype, {"Cache-Control": cache})


def health():
    out = {"ok": True, "mode": CFG["mode"], "catalog": len(CATALOG.rows), "catalogAge": int(time.time() - CATALOG.at) if CATALOG.at else None,
           "summariesAge": int(time.time() - SUMMARIES.at) if SUMMARIES.at else None, "upstreams": {}}
    for name in ("galaxy", "charts"):
        try:
            code, _, _ = fetch(CFG[name], "/api/health" if name == "galaxy" else "/api/public/markets?network=robinhood&kind=none", timeout=3)
            out["upstreams"][name] = code == 200
        except HTTPError:
            out["upstreams"][name] = False
    out["ok"] = all(out["upstreams"].values()) and bool(CATALOG.rows)
    return out


def main():
    global LOG, ACCESS
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("production", "staging"), default="production")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8896)
    ap.add_argument("--console", default="127.0.0.1:19428")
    ap.add_argument("--galaxy", default="127.0.0.1:8890")
    ap.add_argument("--charts", default="127.0.0.1:8895")
    ap.add_argument("--resolver", default="127.0.0.1:8894")
    ap.add_argument("--galaxy-db", default="/srv/skew/em-galaxy/galaxy.db")
    ap.add_argument("--focus", default="/srv/skew/em-galaxy/focus")
    ap.add_argument("--icons", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "token-index.json"))
    ap.add_argument("--evm-catalog", default="/srv/skew/tick-market-ui-20261007/server/evm_catalog.json")
    ap.add_argument("--static", default=None)
    ap.add_argument("--allow-origin", action="append", default=[])
    ap.add_argument("--logs", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "::1"):
        raise SystemExit("The bridge listens on loopback only; nginx publishes it.")
    CFG.update(mode=a.mode, console=hostport(a.console), galaxy=hostport(a.galaxy), charts=hostport(a.charts), resolver=hostport(a.resolver),
               galaxy_db=a.galaxy_db, focus=a.focus, icons=a.icons, evm_catalog=a.evm_catalog, static=a.static, origins={"https://ticking.dev", *a.allow_origin})
    if a.mode == "staging" and not a.static:
        raise SystemExit("--static is required in staging mode")
    os.makedirs(a.logs, exist_ok=True)
    LOG = open(os.path.join(a.logs, "bridge.log"), "a", buffering=1)
    ACCESS = open(os.path.join(a.logs, "access.log"), "a", buffering=1)
    mimetypes.add_type("image/webp", ".webp")
    mimetypes.add_type("font/woff2", ".woff2")
    threading.Thread(target=CATALOG.refresh, daemon=True).start()
    threading.Thread(target=SUMMARIES.build, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    srv.request_queue_size = 64
    log("listening %s:%d mode=%s" % (a.host, a.port, a.mode))
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
