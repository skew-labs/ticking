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
import math
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



def _registry_rows():
    """The one chain registry: --chains (as main() reads it), else registry/chains.json next to this file; the two first-class
    chains when it is missing. Read at import because the chain constants below are module level."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registry", "chains.json")
    for i, a in enumerate(sys.argv):
        if a == "--chains" and i + 1 < len(sys.argv):
            path = sys.argv[i + 1]
        elif a.startswith("--chains="):
            path = a.split("=", 1)[1]
    try:
        with open(path) as f:
            rows = json.load(f)["chains"]
        if [r["id"] for r in rows][:2] == ["arbitrum", "robinhood"]:
            return rows
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return [{"id": "arbitrum", "name": "Arbitrum One", "explorer": "https://arbiscan.io"},
            {"id": "robinhood", "name": "Robinhood Chain", "explorer": "https://robinhoodchain.blockscout.com"}]


REGISTRY = _registry_rows()
EVM_CHAINS = tuple(r["id"] for r in REGISTRY)   # every registry chain: what a request may name (markets, wallets, tools)
ADDR = re.compile(r"^0x[0-9a-f]{40}$")
TX = re.compile(r"^0x[0-9a-f]{64}$")
MARKET = re.compile(r"^(%s):0x[0-9a-f]{40}$" % "|".join(EVM_CHAINS))
INTERVAL = re.compile(r"^[1-9][0-9]{0,2}[smhdw]$")
SEARCH = re.compile(r"^[\w .:\-]{1,64}$")
CHAINS = ("arbitrum", "robinhood")   # chains with galaxy stars and the live feed (node / neighbors / activity, flowmap live)
VENUE = {**{r["id"]: (r["id"], r["name"]) for r in REGISTRY},
         "robinhood": ("robinhood", "Robinhood Chain"), "arbitrum": ("arbitrum", "Arbitrum One")}
EXPLORER = {**{r["id"]: r["explorer"] for r in REGISTRY if r.get("explorer")},
            "robinhood": "https://robinhoodchain.blockscout.com", "arbitrum": "https://arbiscan.io"}
RPC = {"robinhood": ["https://rpc.mainnet.chain.robinhood.com", "https://robinhood.drpc.org"],
       "arbitrum": ["https://arb1.arbitrum.io/rpc", "https://arbitrum.drpc.org"]}
# Public endpoints already used by invscreen; verify chainId before a transaction lookup.
RPC.update({"ethereum":["https://ethereum-rpc.publicnode.com"],"optimism":["https://mainnet.optimism.io"],
            "base":["https://mainnet.base.org"],"polygon":["https://polygon-bor-rpc.publicnode.com"],
            "linea":["https://rpc.linea.build"],"unichain":["https://mainnet.unichain.org"],
            "hyperevm":["https://rpc.hyperliquid.xyz/evm"],"xlayer":["https://rpc.xlayer.tech"]})
RPC_CHAIN_IDS={r["id"]:r.get("chainId") for r in REGISTRY}
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
        if ip in ("127.0.0.1", "::1"):   # internal callers (Ask resolver, evals) share this one key: end users are limited upstream
            rate, burst = rate * 10, burst * 10
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
        # the caller's input was refused upstream (400) or not found (404): pass the code on; anything else is our upstream failing
        raise HTTPError(code if code in (400, 404) else 502, msg if isinstance(msg, str) and code in (400, 404) else "Upstream error %d." % code)
    return json.loads(b)


def galaxy(path, ttl=60, timeout=20):
    return CACHE.get("g:" + path, ttl, lambda: fetch_json(CFG["galaxy"], "/api/" + path, timeout))


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
        if os.environ.get("EM_BRIDGE_NO_SUMMARY_BUILD") == "1":
            return
        if time.time() - self.at > 1800 and not self.lock.locked():
            threading.Thread(target=self.build, daemon=True).start()

    def build(self):
        """One streaming pass over econ_actions (no window functions): flows, venues, the 40 latest actions and the 12 largest
        movers for every catalog symbol. About 1 s per million rows at nice 10; refreshed every 30 min."""
        if os.environ.get("EM_BRIDGE_NO_SUMMARY_BUILD") == "1":
            return
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
    if c not in EVM_CHAINS:
        raise HTTPError(400, "chain must be one of: %s." % ", ".join(EVM_CHAINS))
    return c


def wallet_position(p, chain, as_of=None, sheet_held=False):
    """Keep source quantities and exact contract identity; symbols are display names."""
    held = bool(sheet_held or p.get("held"))
    amount = p.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
        amount = None
    elif isinstance(amount, float) and not math.isfinite(amount):
        amount = None
    elif isinstance(amount, str) and not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", amount):
        amount = None
    symbol = p.get("symbol") or p.get("asset")
    out = {"kind": p.get("kind"), "protocol": pname(p.get("protocol")),
           "asset": p.get("asset") or symbol, "symbol": symbol, "chain": chain,
           "amount": None if held else amount,
           "amountText": None if held or amount is None else str(amount),
           "amountPrecision": "source", "usd": None if held else r2(p.get("usd")),
           "flag": p.get("q"), "pricePending": isinstance(p.get("price"), str) or None,
           "held": held or None, "heldCode": p.get("heldCode"), "heldReason": p.get("heldReason")}
    contracts = []
    for key in ("token", "tokenAddress", "token_address", "contractAddress", "asset"):
        value = p.get(key)
        if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", value) and value.lower() not in {"0x" + "0" * 40, "0x" + "e" * 40}:
            contracts.append(value.lower())
    market = p.get("market")
    conflict = p.get("chain") not in (None, chain)
    if isinstance(market, str) and ":" in market:
        network, address = market.split(":", 1)
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", address) and address.lower() not in {"0x" + "0" * 40, "0x" + "e" * 40}:
            conflict = conflict or network != chain
            contracts.append(address.lower())
    conflict = conflict or len(set(contracts)) > 1
    out["identityResolved"] = bool(contracts) and not conflict
    if conflict:
        out["identityConflict"] = True
    if out["identityResolved"]:
        out.update(token=contracts[0], market=chain + ":" + contracts[0])
    raw = p.get("rawAmount", p.get("raw_amount"))
    decimals = p.get("decimals")
    if not held and not isinstance(raw, bool) and isinstance(raw, (int, str)) and re.fullmatch(r"-?\d+", str(raw)):
        out["rawAmount"] = str(raw)
        if type(decimals) is int and 0 <= decimals <= 255:
            out.update(decimals=decimals, amountPrecision="raw")
    when = p.get("asOf", p.get("as_of", as_of))
    if when is not None:
        out["asOf"] = when
    return out


def tool_wallet(qs):
    addr, chain = need_addr(qs), opt_chain(qs)
    limit = qs.get("positions_limit", "8")
    if isinstance(limit, bool) or not re.fullmatch(r"\d{1,3}", str(limit)) or not 1 <= int(limit) <= 100:
        raise HTTPError(400, "positions_limit must be between 1 and 100.")
    limit = int(limit)
    # the search walks every compact chain before it answers: only the galaxy-star chains need it (node ids)
    hit = find_wallet(addr, chain) if not chain or chain in CHAINS else None
    if hit:
        chain = hit["chain"]
    card = {"kind": "wallet", "address": addr, "chain": chain, "inGalaxy": bool(hit and hit.get("in_map"))}
    node = None
    if hit and hit.get("node_id") is not None and hit.get("in_map"):
        node = galaxy("node/%s/%d" % (chain, int(hit["node_id"])))
    else:
        for ch in ([chain] if chain else list(CHAINS[::-1])):
            try:
                node = galaxy("wallet/%s/%s" % (ch, addr), ttl=300, timeout=20 if ch in CHAINS else 45)   # compact: Blob reads when cold
            except HTTPError:
                continue
            chain = card["chain"] = ch
            break
    if node is None:
        if chain and chain not in CHAINS:   # a registry chain the map does not serve yet: say so instead of a 404
            st = next((r for r in chains_status()["chains"] if r.get("id") == chain), {})
            status = st.get("status") or "coming"
            if st.get("hasData"):           # the chain is served; this wallet is just not in its map
                raise HTTPError(404, "This wallet is not in the Economic Machine map of %s." % VENUE.get(chain, (chain, chain))[1])
            return {"kind": "wallet", "address": addr, "chain": chain, "inGalaxy": False, "inMap": False, "status": status,
                    "message": "The Economic Machine does not serve %s wallets yet (%s)." % (VENUE.get(chain, (chain, chain))[1], status)}
        raise HTTPError(404, "This wallet is not in the Economic Machine map of Arbitrum One or Robinhood Chain.")
    label = node.get("label") if isinstance(node.get("label"), dict) else {}
    card["nodeId"] = node.get("node_id")
    card["category"] = node.get("category") or label.get("category") or "unknown"
    card["categoryName"] = CATEGORY.get(card["category"], str(card["category"]).replace("_", " ").title())
    card["entity"] = human(node.get("entity_name"))
    card["protocol"] = pname(node.get("protocol"))
    card.update(wallet_brand(node, label))
    for k in ("tx_in", "tx_out", "first_seen", "last_seen", "degree"):
        if isinstance(node.get(k), (int, float)):
            card[k] = node[k]
    bs = node.get("balance_sheet") or {}
    if isinstance(bs, dict) and bs:
        card["sheet"] = {k: r2(bs.get(k)) for k in ("net_worth", "liquid", "supplied", "collateral", "debt", "lp", "perps") if bs.get(k)}
        # held (owner decision: kept back while it is re-reconciled), partial (unpriced positions) and native-balance notes travel with
        # the sheet, so a page never shows those values as plain numbers
        if bs.get("held_positions"):   # some rows held back by the econ check (not in the totals, no amounts)
            card["sheet"].update(heldPositions=bs["held_positions"], heldCodes=bs.get("heldCodes"))
        if bs.get("held"):
            card["sheet"].update(held=True, heldReason=str(bs.get("heldReason") or "re-reconciling"), heldRows=bs.get("held_rows"),
                                 heldCode=bs.get("heldCode"), heldReasonKo=bs.get("heldReasonKo"))
        if bs.get("partial"):
            card["sheet"].update(partial=True, note=bs.get("note"), unpricedRows=bs.get("unpriced_rows"))
        if isinstance(bs.get("native_balance"), str):
            card["sheet"]["nativeNote"] = bs["native_balance"]
        card["asOf"] = bs.get("as_of")
        pos = [p for p in (bs.get("positions") or []) if isinstance(p, dict)]
        card["positions"] = [wallet_position(p, chain, bs.get("as_of"), bs.get("held"))
                             for p in sorted(pos, key=lambda p: -abs(p.get("usd") or 0))[:limit]]
        card["positionsTotal"] = bs.get("positions_total", len(pos))
        card["positionsReturned"] = len(card["positions"])
        card["positionsTruncated"] = card["positionsTotal"] > card["positionsReturned"]
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
        mid = p.get("market")
        if mid and mid in CATALOG.by_id:
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
    launch = launch_buying(ch, em["address"])
    movers = [dict(m, categoryName=CATEGORY.get(m.get("category") or "unknown", m.get("category")),
                   entity=human(m.get("entity"))) for m in SUMMARIES.movers.get(key, []) if m.get("address")]
    return {"kind": "holders", "market": em["row"]["id"], "chain": ch, "symbol": em["row"]["symbol"], "token": em["address"],
            "icon": em["row"]["icon"], "holderCount": int(float(tok["holders"])) if tok and tok.get("holders") else None,
            "holders": holders, "movers": movers, "flows": sorted(SUMMARIES.flows.get(key, []), key=lambda f: -(f["usd"] or 0))[:8],
            "venues": SUMMARIES.venues.get(key, []), "recent": SUMMARIES.recent.get(key, [])[:12], "asOf": SUMMARIES.asof.get(ch),
            "launchBuying": launch, "ready": bool(SUMMARIES.at)}


SUSPECT_WHY = ("Suspected, not grouped: these buyers share an exchange withdrawal address. Thousands of people withdraw from the same exchange, "
               "so this alone is not evidence of one owner.")


def launch_buying(chain, token):
    """First-hour linked and suspected buying from invscreen's bundle-summary, summary numbers only (the wallets stay behind Pro on the
    bundle-summary route). A cold summary takes ~20 s there, so this waits 4 s and says preparing; the next card has it from its cache."""
    try:
        b = CACHE.get("lb:%s:%s" % (chain, token), 900, lambda: fetch_json(CFG["invscreen"], "/bundle-summary?" + urllib.parse.urlencode(
            {"market": "%s:%s" % (chain, token), "window": "1h"}), timeout=4))
    except Exception:   # timeout, not in the map yet, invscreen down
        return {"status": "preparing"}
    if not isinstance(b, dict) or b.get("available") is False:
        return {"status": "unavailable", "reason": (b or {}).get("reason") if isinstance(b, dict) else None}
    out = {k: b.get(k) for k in ("window", "buysUsd", "linkedBuysUsd", "pct", "groups", "wallets", "grade",
                                 "suspectedBuysUsd", "suspectedPct", "suspectedWallets", "suspectedSets")}
    out.update(status="ready", launch=(b.get("launch") or {}).get("timeUtc"), asOf=(b.get("asOf") or {}).get(chain),
               linkedNote="Linked groups: wallets joined by links and timing (grades inferred / judged), never a shared funder alone.",
               suspectedNote=SUSPECT_WHY if b.get("suspectedWallets") else None)
    return out


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
    urls=RPC.get(chain) or []
    expected=RPC_CHAIN_IDS.get(chain)
    if not urls or expected is None:
        raise HTTPError(503, "Transaction reader is not configured for %s." % VENUE[chain][1], retry=60)
    def call(url,method,params):
        body=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
        req=urllib.request.Request(url,data=body,headers={"Content-Type":"application/json","User-Agent":"em-ticking-bridge/1"})
        with urllib.request.urlopen(req,timeout=8) as r:
            data=json.loads(r.read(200_000))
        if "error" in data or "result" not in data:
            raise ValueError("RPC response failed")
        return data["result"]
    errors=[]
    for url in urls:
        try:
            actual=CACHE.get("rpc-chain:"+url,600,lambda:call(url,"eth_chainId",[]))
            if int(actual,16)!=int(expected):
                raise ValueError("RPC chain mismatch")
            result=call(url,"eth_getTransactionByHash",[tx])
            if result is not None:
                if not isinstance(result,dict) or str(result.get("hash","")).lower()!=tx:
                    raise ValueError("RPC transaction mismatch")
                return result
        except Exception as exc:
            errors.append(type(exc).__name__)
    if errors:
        raise HTTPError(503, "Transaction lookup is temporarily unavailable on %s." % VENUE[chain][1], retry=10)
    raise HTTPError(404, "Transaction not found on %s." % VENUE[chain][1])


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
TOOL_PARAMS = {"wallet": {"address", "chain", "positions_limit"}, "holders": {"market", "symbol", "chain"}, "top": {"symbol"}, "trace": {"address", "chain"},
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
    m = re.fullmatch(r"wallet/(%s)/(0x[0-9a-fA-F]{40})" % "|".join(EVM_CHAINS), rest)
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

# em-account bridge layer v8
# Accounts (em-account on loopback): plans and billing, watches, alerts, alert email, the assistant allowance.
ACCOUNT_PATHS = ("/api/public/em/account", "/api/public/em/billing", "/api/public/em/watch", "/api/public/em/alerts",
                 "/api/public/em/notify", "/api/public/em/email", "/api/public/em/source-alerts/email", "/api/public/em/credits", "/api/public/em/intelligence",
                 "/api/public/em/investigations", "/api/public/em/keys", "/api/public/em/v1")
ACCOUNT = {"loaded": 0, "cfg": None}


def is_account(path):
    return any(path == p or path.startswith(p + "/") for p in ACCOUNT_PATHS)


def account_cfg():
    if time.time() - ACCOUNT["loaded"] > 30:
        cfg = None
        try:
            with open(CFG["account_config"]) as f:
                raw = json.load(f)
            with open(raw["internal_token_file"]) as f:
                tok = f.read().strip()
            cfg = {"addr": hostport(raw["account"]), "gate": raw.get("assistant_gate", "off"), "token": tok}
        except (OSError, ValueError, KeyError, TypeError):
            cfg = None
        ACCOUNT.update(loaded=time.time(), cfg=cfg)
    return ACCOUNT["cfg"]


def account_call(path, session, ip=None):
    """em-account's internal API (shared secret); None when it is not configured or unreachable."""
    cfg = account_cfg()
    if not cfg:
        return None
    c = http.client.HTTPConnection(*cfg["addr"], timeout=5)
    try:
        c.request("POST", path, body=json.dumps({"session": session or "", "ip": ip}),
                  headers={"Content-Type": "application/json", "X-EM-Internal": cfg["token"]})
        r = c.getresponse()
        body = r.read(200_000)
        return json.loads(body) if r.status == 200 else None
    except (OSError, ValueError):
        return None
    finally:
        c.close()


def search_up(up):
    """With --search-paths investigate the search upstream is em-investigate, which serves the same reads without the
    /api/public/em prefix (/search, /entities, /entity, /wallet-pnl-xc)."""
    return up[len("/api/public/em"):] if CFG.get("search_paths") == "investigate" and up.startswith("/api/public/em/") else up


def ask_cookie(h):
    for part in (h.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "tk_ask" and re.fullmatch(r"[0-9a-f]{32}", v):
            return v
    return ""


def session_cookie(h):
    for part in (h.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "ticking_session":
            return v
    return ""


def with_entitlements(data, session):
    try:
        d = json.loads(data)
    except ValueError:
        return data
    g = account_call("/internal/entitlements", session)
    if not isinstance(d, dict) or not g or not g.get("entitlements"):
        return data
    d["entitlements"] = g["entitlements"]
    return json.dumps(d, separators=(",", ":"), default=str).encode()


# Plan-gated detail on investigation reads (the numbers stay for everyone; who and why are Pro): a response may declare
# its own {"tierGate": {"pro": ["path.*.field", ...]}}; otherwise these defaults apply. Paths: dot-separated keys, * = every item.
TIER_RANK = {"anon": 0, "free": 1, "individual": 2, "pro": 3, "team": 4}
GATE_DEFAULT = {"/api/public/em/bundle-summary": {"pro": ["evidence", "counter", "launchBlockBuyers", "hubBuyersExcluded", "windows.*.evidence",
                                                          "windows.*.counter", "windows.*.groupsDetail", "windows.*.exchangeOnly"]}}
TIERS_SEEN = {}


def tier_of(h):
    session = session_cookie(h)
    if not session:
        return "anon"
    key = session
    hit = TIERS_SEEN.get(key)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    g = account_call("/internal/entitlements", session) or {}
    tier = (g.get("entitlements") or {}).get("tier") or "anon"
    if len(TIERS_SEEN) > 5000:
        TIERS_SEEN.clear()
    TIERS_SEEN[key] = (time.time(), tier)
    return tier


def _strip(node, parts):
    """Blank the field at `parts` (dot path, * = every item); True when something was removed."""
    if node is None or not parts:
        return False
    k, rest = parts[0], parts[1:]
    if k == "*":
        return any([_strip(x, rest) for x in node]) if isinstance(node, list) else False
    if not isinstance(node, dict) or k not in node:
        return False
    if rest:
        return _strip(node[k], rest)
    node[k] = None
    return True


def gate_detail(path, data, h):
    """Copy of `data` without the fields above the viewer's plan, and whether anything was gated (then: not cacheable)."""
    spec = (data.get("tierGate") if isinstance(data, dict) else None) or GATE_DEFAULT.get(path)
    if not spec or not account_cfg():
        return data, False
    rank = TIER_RANK.get(tier_of(h), 0)
    locked = [(need, paths) for need, paths in spec.items() if TIER_RANK.get(need, 9) > rank]
    if not locked:
        return data, True
    out, gone = json.loads(json.dumps(data, default=str)), set()
    for need, paths in locked:
        for pth in paths:
            if _strip(out, pth.split(".")):
                gone.add(pth)
    out["locked"] = {"required": min((n for n, _ in locked), key=lambda n: TIER_RANK.get(n, 9)), "fields": sorted(gone),
                     "reason": "Wallets and evidence behind these numbers are on Pro."}
    return out, True


GATE_TEXT = {"signin": "Sign in to keep asking the Ticking Assistant (one preview answer a day without an account; Free accounts get 5 a day).",
             "daily_limit": "You have used today's %s questions on the %s plan. Upgrade for more, or come back tomorrow.",
             "fair_use": "The assistant is paused for this account today (fair-use limit). It resets at 00:00 UTC."}


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

    def send_body(self, code, body, ctype="application/json", headers=None, cookies=()):
        if code >= 400:
            ACCESS.write("%s %d %s %s\n" % (time.strftime("%H:%M:%S"), code, self.command, self.path[:200]))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in {**self.cors(), **(headers or {})}.items():
            self.send_header(k, v)
        for v in cookies:
            self.send_header("Set-Cookie", v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code, obj, cache="no-store", extra=None):
        if code == 200 and isinstance(obj, dict) and obj.get("schema") in LOGO_SCHEMAS:
            obj = protocol_logos(obj)   # whichever block built the answer (directory, search)
        b = json.dumps(obj, separators=(",", ":"), allow_nan=False, default=str).encode()
        # Canonical paths for known, independently identified graph artwork.
        # Rewrite serialized strings so shared cached objects remain immutable.
        for old, new in GRAPH_ARTWORK_ALIASES.items():
            b = b.replace(json.dumps(old).encode(), json.dumps(new).encode())
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
        self.write_method("POST")

    def do_PUT(self):
        self.write_method("PUT")

    def do_DELETE(self):
        self.write_method("DELETE")

    def write_method(self, method):
        path = urllib.parse.unquote(self.path.split("?")[0])
        try:
            if is_account(path):
                return self.account_proxy(method)
            if method == "POST" and path == "/api/public/intelligence/resolve" and (CFG["mode"] == "staging" or account_cfg()):
                return self.resolve()
            if CFG["mode"] == "staging" and path.startswith("/api/") and not path.startswith("/api/public/"):
                return self.passthrough(path, "", method)
            return self.send_json(405, {"detail": "Read-only."})
        except HTTPError as e:
            self.send_json(e.code, {"detail": e.msg, "error": e.msg}, extra={"Retry-After": str(e.retry)} if e.retry else None)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("error %s %s %r" % (method, path, e))
            try:
                self.send_json(500, {"detail": "Internal error."})
            except Exception:
                pass

    def account_proxy(self, method):
        cfg = account_cfg()
        if not cfg:
            raise HTTPError(404, "Accounts are not served here yet.")
        LIMIT.take(client_ip(self), "data")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 256_000:
            raise HTTPError(413, "Request is too large.")
        body = self.rfile.read(n) if n else None
        hdrs = {"X-Real-IP": client_ip(self)}
        for k in ("Cookie", "Origin", "Content-Type", "Stripe-Signature", "Accept", "User-Agent", "Authorization"):
            if self.headers.get(k):
                hdrs[k] = self.headers[k]
        c = http.client.HTTPConnection(*cfg["addr"], timeout=120)
        try:
            c.request(method, self.path, body=body, headers=hdrs)
            r = c.getresponse()
            data = r.read(2_000_000)
        except OSError:
            raise HTTPError(503, "Accounts are briefly unavailable.")
        finally:
            c.close()
        keep = {k: v for k, v in r.getheaders() if k.lower() in ("cache-control", "location", "retry-after", "content-disposition", "x-export-source",
                                                                  "x-ratelimit-limit", "x-ratelimit-remaining", "x-quota-monthly-limit",
                                                                  "x-quota-monthly-remaining")}
        self.send_body(r.status, data, r.getheader("Content-Type", "application/json"), {"Cache-Control": "no-store", **keep})

    def resolve(self):
        try:
            LIMIT.take(client_ip(self), "tools")
        except HTTPError as e:
            return self.send_json(429, {"error": e.msg}, extra={"Retry-After": str(e.retry)})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2048:
            return self.send_json(413, {"error": "Request is too large."})
        body = self.rfile.read(n)
        cfg = account_cfg()
        gated = bool(cfg and cfg["gate"] == "enforce")
        session, allowance = session_cookie(self), None
        # Signed-out visitors are counted by a first-party cookie (behind a CDN many visitors share one address).
        ask_id = ask_cookie(self)
        new_ask = None if ask_id else os.urandom(16).hex()
        who = "c:" + (ask_id or new_ask)
        ask_set = [("tk_ask=%s; Path=/api/public/intelligence; Max-Age=31536000; HttpOnly; SameSite=Lax%s"
                    % (new_ask, "; Secure" if CFG["mode"] == "production" else ""))] if new_ask else []
        if gated:
            allowance = account_call("/internal/assistant/consume", session, who)
            if allowance is None:
                return self.send_json(503, {"error": "The assistant is briefly unavailable."})
            if not allowance.get("allowed"):
                reason = allowance.get("reason") or "signin"
                text = GATE_TEXT.get(reason, GATE_TEXT["signin"])
                if reason == "daily_limit":
                    text = text % (allowance.get("daily"), (allowance.get("tier") or "").title())
                return self.send_body(401 if reason == "signin" else 429, json.dumps({"error": text, "gate": allowance}).encode(),
                                      "application/json", {"Cache-Control": "no-store"}, cookies=ask_set)
        c = http.client.HTTPConnection(*CFG["resolver"], timeout=20)
        ok = False
        try:
            c.request("POST", "/api/public/intelligence/resolve", body=body, headers={"Content-Type": "application/json",
                      "Origin": self.headers.get("Origin") or "", "X-Real-IP": client_ip(self)})
            r = c.getresponse()
            ok = r.status < 500
            extra = {"Cache-Control": "no-store"}
            if allowance:
                extra["X-Ticking-Assistant"] = "%s/%s" % (allowance.get("used_today"), allowance.get("daily") if allowance.get("daily") is not None else "fair-use")
            self.send_body(r.status, r.read(400_000), r.getheader("Content-Type", "application/json"), extra, cookies=ask_set)
        except OSError:
            self.send_json(503, {"error": "The assistant is not running."})
        finally:
            c.close()
            if gated and not ok:
                account_call("/internal/assistant/refund", session, who)

    # --- API ------------------------------------------------------------------------------------
    def api(self, path, query, qs):
        if is_account(path):
            return self.account_proxy("GET")
        ip = client_ip(self)
        REQ.ip = ip
        if path == "/api/public/em/health":
            return self.send_json(200, health())
        if path == "/api/public/em/chains":
            return self.send_json(200, chains_status())
        m = re.fullmatch(r"/api/public/em/icon/([a-z][a-z0-9]{1,15})/(0x[0-9a-f]{40})", path)
        if m and m.group(1) in chain_ids():
            LIMIT.take(ip, "icon")
            return self.icon(m.group(1), m.group(2))
        if path.startswith("/api/public/em/tools/"):
            name = path.rsplit("/", 1)[1]
            fn = TOOLS.get(name)
            if not fn:
                raise HTTPError(404, "Unknown tool.")
            if set(qs) - TOOL_PARAMS[name]:
                raise HTTPError(400, "Unexpected parameter.")
            request_limit(self, ip, "tools")
            key = "t:%s:%s" % (name, urllib.parse.urlencode(sorted(qs.items())))
            ttl = TOOL_TTL[name] if (name != "holders" or SUMMARIES.at) else 0   # never cache a not-ready summary
            return self.send_json(200, CACHE.get(key, ttl, lambda: fn(qs)), cache="public, max-age=30" if ttl else "no-store")
        if path.startswith("/api/public/em/g/"):
            request_limit(self, ip, "data")
            route, ttl = galaxy_route(path[len("/api/public/em/g/"):], qs)
            return self.send_json(200, galaxy(route, ttl), cache="public, max-age=%d" % min(ttl, 60))
        if path == "/api/public/em/search":
            request_limit(self, ip, "data")
            return self.send_json(200, search_route(qs), cache="public, max-age=30")
        if path in INVSCREEN_ROUTES:
            request_limit(self, ip, "data")
            data, ttl = invscreen_route(path, qs)
            data, gated = gate_detail(path, data, self)
            return self.send_json(200, data, cache="private, no-store" if gated else "public, max-age=%d" % min(ttl, 60))
        if path in ENTITY_ROUTES:
            request_limit(self, ip, "data")
            return self.send_json(200, entity_route(path, qs), cache="public, max-age=60")
        if path in XC_ROUTES:
            request_limit(self, ip, "data")
            data, ttl = xc_route(path, qs)
            return self.send_json(200, data, cache="public, max-age=%d" % min(ttl, 60))
        if path in FLOW_PATHS:   # money-flow map (wallet map)
            return flow_route(self, path, qs, ip)
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
    def passthrough(self, path, query, method="GET"):
        stream = "/stream/" in path
        c = http.client.HTTPConnection(*CFG["console"], timeout=900 if stream else 30)
        hdrs = {"Host": "api.ticking.dev", "Accept": self.headers.get("Accept") or "*/*", "X-Real-IP": client_ip(self)}
        for k in ("Cookie", "Origin", "Content-Type", "Authorization"):
            if self.headers.get(k):
                hdrs[k] = self.headers[k]
        body = None
        if method != "GET":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                c.close()
                raise HTTPError(413, "Request is too large.")
            body = self.rfile.read(n) if n else b""
        try:
            c.request(method, self.path, body=body, headers=hdrs)
            r = c.getresponse()
        except OSError as e:
            c.close()
            raise HTTPError(502, "Console app unavailable (%s)." % type(e).__name__)
        try:
            ctype = r.getheader("Content-Type", "application/octet-stream")
            if not ctype.startswith("text/event-stream"):
                data = r.read()
                if method == "GET" and path == "/api/console" and r.status == 200 and account_cfg():
                    data = with_entitlements(data, session_cookie(self))
                cookies = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
                return self.send_body(r.status, data, ctype, {k: v for k, v in r.getheaders() if k.lower() in ("cache-control", "etag", "retry-after")},
                                      cookies=cookies)
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


# ------------------------------------------------------------------------------------------------
# The one chain registry (registry/chains.json) with each chain's status now: live = Economic Machine data and a real-time
# manifest under 1 h old; history-only = data without fresh real time; coming = no Economic Machine data yet (raw extraction
# alone does not count). The frontend reads this; a chain changes status without a release.
CHAIN_DIRS = {"charts": "/srv/skew/em-charts/data/%s", "map": "/srv/skew/em-data/map/%s", "realtime": "/srv/skew/em-realtime/out/%s/manifests"}
# Protocol logos (registry/protocol-logos.json -> assets/em/protocol-logos/index.json, tools/protocol_logos.py): fills the empty
# `icon` of protocols/1, protocol/1 and search/1 protocol rows (and named institutions in entities/1, entity/1, search), plus `iconCredit`. A row gets its brand only when it is the
# real protocol: named by the registry or docs, or every venue key is one the catalogue verified against the protocol's
# published factory (or registry deployer). Forks and names read from contracts keep icon null (the pages draw a monogram).
LOGO_SCHEMAS = ("protocols/1", "protocol/1", "search/1", "entities/1", "entity/1")
LOGO_SEEN = {}   # "chain:id" -> brand | None, from rows that carried their evidence keys


def logo_index():
    def load():
        path = CFG.get("protocol_logos") or os.path.join(CFG.get("static") or "", "assets/em/protocol-logos/index.json")
        try:
            with open(path) as f:
                idx = json.load(f)
            return idx if idx.get("schema") == "protocol-logos/1" else None
        except (OSError, ValueError):
            return None
    return CACHE.get("protocol-logos", 300, load)


def logo_brand(idx, chain, row):
    if not isinstance(row, dict) or row.get("aggregate") or not chain or not isinstance(row.get("id"), str):
        return None
    ev = [e for e in row.get("evidence") or [] if isinstance(e, dict)]
    brand = idx["registry"].get(row["id"]) if any(e.get("source") in ("registry", "docs") for e in ev) else None
    if not brand:
        v = idx["verified"].get("%s:%s" % (chain, row["id"]))
        keys = {e.get("key") for e in ev}
        brand = v["brand"] if v and keys and keys <= set(v["keys"]) else None
        if v:
            LOGO_SEEN["%s:%s" % (chain, row["id"])] = brand   # only the catalogue's verified rows (bounded)
    return brand


def logo_search_brand(idx, r):
    # search rows carry evidence text, not venue keys: registry/docs names map directly; a verified fork-named row needs the
    # directory's own keys (seen in a protocols answer, else read once from the protocol route)
    if (r.get("label") or {}).get("source") in ("registry", "docs") and r.get("id") in idx["registry"]:
        return idx["registry"][r["id"]]
    k = "%s:%s" % (r.get("chain"), r.get("id"))
    if k not in idx["verified"]:
        return None
    if k not in LOGO_SEEN:
        try:
            d = CACHE.get("logo-row:" + k, 600, lambda: fetch_json((CFG["self_host"], CFG["self_port"]),
                          "/api/public/em/protocol?" + urllib.parse.urlencode({"chain": r["chain"], "id": r["id"]}), timeout=4))
            logo_brand(idx, r["chain"], d)
        except Exception:
            return None
    return LOGO_SEEN.get(k)


def protocol_logos(obj):
    idx = logo_index()
    if not idx:
        return obj
    def deco(row, brand):
        b = idx["brands"].get(brand) if brand else None
        return dict(row, icon=b["file"], iconCredit="%s · %s" % (b["name"], b["credit"])) if b else row
    try:
        if obj["schema"] == "protocols/1":
            return dict(obj, rows=[r if r.get("icon") else deco(r, logo_brand(idx, r.get("chain") or obj.get("chain"), r)) for r in obj.get("rows") or []])
        if obj["schema"] == "protocol/1":
            return obj if obj.get("icon") else deco(obj, logo_brand(idx, obj.get("chain"), obj))
        if obj["schema"] == "entities/1":
            return dict(obj, rows=[deco(r, (idx.get("entities") or {}).get(r.get("entity_id"))) if isinstance(r, dict) and not r.get("icon") else r for r in obj.get("rows") or []])
        if obj["schema"] == "entity/1":
            e = obj.get("entity") or {}
            return obj if e.get("icon") else dict(obj, entity=deco(e, (idx.get("entities") or {}).get(e.get("entity_id"))))
        ent = idx.get("entities") or {}
        def search_row(r):
            if not isinstance(r, dict) or r.get("icon"):
                return r
            if r.get("type") == "protocol":
                return deco(r, logo_search_brand(idx, r))
            if r.get("type") == "wallet" and (r.get("label") or {}).get("grade") not in ("official", "strong"):
                return r   # a candidate (estimated) address of an institution: group icon on the page, never the institution's mark
            eid = r.get("id") if r.get("type") == "entity" else (r.get("entity") or {}).get("id") if r.get("type") == "wallet" else None
            return deco(r, ent.get(eid)) if isinstance(eid, str) else r   # a named institution's own mark
        return dict(obj, results=[search_row(r) for r in obj.get("results") or []])
    except (KeyError, TypeError, AttributeError) as e:
        log("protocol logos skipped: %r" % e)
        return obj


def wallet_brand(node, label=None):
    """Artwork basis for one wallet card: the brand mark (icon + iconCredit) only when the name is proven: a registry label (the contract
    registry, a published deployment list: label_source registry/public_registry/docs, or the compact label's `rule: registry`) whose
    protocol key or name is in the logo index, or an institution's own wallet in the research labels graded official or structural.
    Anything else is named by a classifier or an agent: no mark, `estimated` (the page shows the group icon and "Inferred")."""
    label = label or {}
    name = str(node.get("entity_name") or label.get("entity_name") or "")
    src = str(node.get("label_source") or label.get("source") or "")
    ev = node.get("evidence") if isinstance(node.get("evidence"), list) else []
    registry = src in ("registry", "public_registry", "docs") or "rule: registry" in ev
    research = src.startswith("research (") and src[10:].split(" ")[0].rstrip(")") in ("official", "structural")   # e.g. "research (official (signed …))"
    out = {"labelBasis": "registry" if registry else "research" if research else "agent" if src == "agent" else "classifier"}
    if not (registry or research):
        if name or (node.get("category") or label.get("category") or "unknown") != "unknown":
            out["estimated"] = True
        return out
    idx = logo_index()
    if not idx:
        return out
    if research:   # the compact card carries the entity's name; the id when it has one
        ents = idx.get("entities") or {}
        brand = ents.get(node.get("entity_id")) or ents.get(re.sub(r"[^a-z0-9]", "", name.lower()))
    elif ":" in name:
        brand = (idx.get("labels") or {}).get(name.split(":", 1)[0])
    else:
        brand = next((b for p, b in (idx.get("named") or {}).items() if name.startswith(p)), None)
    b = idx["brands"].get(brand) if brand else None
    if b:
        out.update(icon=b["file"], iconCredit="%s · %s" % (b["name"], b["credit"]))
    return out


CHAINS_CACHE = [0.0, None]


GRAPH_ARTWORK_ALIASES = {
    "/assets/em/marks/brand-usdc.svg": "/assets/tokens/researched/30b17f7e97d965787082a76f.webp",
    **{f"/assets/em/marks/chain-{number}.svg": f"/assets/em/marks/chain-{chain}.svg" for number, chain in {
        1: "ethereum", 42161: "arbitrum", 8453: "base", 10: "optimism", 137: "polygon",
        59144: "linea", 130: "unichain", 5042: "arc", 4663: "robinhood"}.items()}
}


def chains_status():
    if CHAINS_CACHE[1] and time.time() - CHAINS_CACHE[0] < 60:
        return CHAINS_CACHE[1]
    try:
        with open(CFG["chains"]) as f:
            rows = json.load(f)["chains"]
    except (OSError, ValueError, KeyError):
        rows = []
    out = []
    # realtimeAgeS reads the same source as /api/public/em/freshness (newest collected block's timestamp), so the
    # chain list and freshness agree; the manifests directory mtime (collector wall clock) is only the fallback.
    try:
        fr = (fetch_json(CFG["invscreen"], "/freshness?chains=" + ",".join(r.get("id", "") for r in rows if r.get("id")), timeout=8) or {}).get("chains") or {}
    except Exception:
        fr = {}
    for r in rows:
        cid = r.get("id", "")
        data = any(os.path.isdir(CHAIN_DIRS[k] % cid) for k in ("charts", "map"))
        rt = (fr.get(cid) or {}).get("realtime") or {}
        age, age_basis = None, None
        if rt.get("time"):
            age, age_basis = max(0, int(time.time() - rt["time"])), "freshness realtime: newest collected block timestamp"
        else:
            try:   # the directory's own mtime moves when a manifest is added (no scan of 500k entries)
                age, age_basis = int(time.time() - os.stat(CHAIN_DIRS["realtime"] % cid).st_mtime), "collector manifest mtime (fallback)"
            except OSError:
                age = None
        status = ("live" if age is not None and age < 3600 else "history-only") if data else "coming"
        capabilities = dict(r.get("capabilities") or {})
        chart_db = CHAIN_DIRS["charts"] % cid + "/candles.duckdb"
        chart_markets = CHAIN_DIRS["charts"] % cid + "/markets_export.parquet"
        capabilities["charts"] = os.path.isfile(chart_db) and os.path.isfile(chart_markets)
        out.append({**r, "capabilities": capabilities, "status": status, "registryStatus": r.get("status"), "hasData": data, "realtimeAgeS": age,
                    "realtimeBlock": rt.get("block"), "realtimeAgeBasis": age_basis})
    res = {"schema": "chains/1", "chains": out, "rule": "live = Economic Machine data and real time under 1 h old; history-only = data "
           "without fresh real time; coming = no Economic Machine data yet"}
    CHAINS_CACHE[:] = [time.time(), res]
    return res


# Cross-chain reads (serve_xc.py, XC-API.md): same-owner links, bridge legs matched across chains, chain-to-chain flows.
XC_ROUTES = {"/api/public/em/wallet-xc": (("address", "limit"), 120), "/api/public/em/xc-flows": (("window", "classes"), 300),
             "/api/public/em/l1-senders": (("chain", "wallets"), 300), "/api/public/em/xc-health": ((), 60)}
XC_CLASS = r"(?:exact|structural|heuristic|conflict|one_side)"
XC_PARAM = {"address": r"0x[0-9a-f]{40}", "limit": r"[1-9][0-9]{0,2}", "window": r"7d|30d|90d|all", "classes": XC_CLASS + r"(?:," + XC_CLASS + r"){0,4}",
            "chain": r"[a-z][a-z0-9]{1,15}", "wallets": r"0x[0-9a-f]{40}(?:,0x[0-9a-f]{40}){0,49}"}


def xc_route(path, qs):
    keys, ttl = XC_ROUTES[path]
    if set(qs) - set(keys):
        raise HTTPError(400, "Unexpected parameter.")
    q = {}
    for k in keys:
        v = (qs.get(k) or "").lower()
        if not v:
            continue
        if not re.fullmatch(XC_PARAM[k], v):
            raise HTTPError(400, "Invalid %s." % k)
        q[k] = v
    if path == "/api/public/em/wallet-xc" and "address" not in q:
        raise HTTPError(400, "address is required.")
    up = path + ("?" + urllib.parse.urlencode(q) if q else "")
    return CACHE.get("xc:" + up, ttl, lambda: fetch_json(CFG["xc"], up, timeout=30)), ttl


# Unified search (SEARCH-API.md, search/1): wallets, tokens, protocols and transactions across the registry chains.
SEARCH_PARAM = {"q": r"[^\x00-\x1f]{1,120}", "limit": r"[1-9][0-9]?", "chain": r"[a-z][a-z0-9]{1,15}"}


def search_route(qs):
    if set(qs) - set(SEARCH_PARAM):
        raise HTTPError(400, "Unexpected parameter.")
    q = {k: v for k, v in qs.items() if v}
    if "q" not in q or any(not re.fullmatch(SEARCH_PARAM[k], v) for k, v in q.items()):
        raise HTTPError(400, "Invalid search.")
    up = "/api/public/em/search?" + urllib.parse.urlencode(q)
    return CACHE.get("s:" + up, 30, lambda: fetch_json(CFG["search"], search_up(up), timeout=10))


# Named institutions (entities/1, entity/1: research labels, OKX first) and wallet-pnl-xc, from the same service as search.
ENTITY_PARAM = {"q": r"[^\x00-\x1f]{1,100}", "limit": r"[1-9][0-9]{0,2}", "chain": r"[a-z][a-z0-9]{1,15}", "type": r"[a-z_]{2,20}",
                "id": r"[a-z0-9][a-z0-9_.-]{0,63}", "grade": r"official|strong|candidate", "role": r"[a-z_]{2,32}", "cursor": r"[A-Za-z0-9_=-]{1,200}",
                "sort": r"[a-z_]{2,20}", "address": r"0x[0-9a-fA-F]{40}"}
ENTITY_ROUTES = {"/api/public/em/entities": ("q", "limit", "chain", "type"), "/api/public/em/entity": ("id", "chain", "grade", "role", "limit", "cursor", "sort"),
                 "/api/public/em/wallet-pnl-xc": ("address", "chain", "limit")}   # wallet-pnl/1 across chains (same service as search)


# The investigation screen's reads (em-invscreen, INVESTIGATE-SCREEN-NEEDS.md): holders at a moment, one wallet's trades on one
# token, how it got each token, the same address on every chain, links around one wallet, a token's launch. Each answer carries
# `available` (false + reason = "Preparing" on the page) and the block / tx / time behind every number.
INVSCREEN_PARAM = {"market": r"[a-z][a-z0-9]{1,15}:0x[0-9a-f]{40}", "address": r"0x[0-9a-f]{40}", "chain": r"[a-z][a-z0-9]{1,15}", "snaps": r"[01]",
                   "limit": r"[1-9][0-9]{0,2}", "t": r"[0-9]{9,10}", "block": r"[0-9]{1,12}", "interval": r"1m|5m|15m|1h",
                   "from": r"[0-9]{9,10}", "to": r"[0-9]{9,10}", "minusd": r"[0-9]{1,7}(\.[0-9]{1,4})?",
                   "a": r"0x[0-9a-f]{40}", "b": r"0x[0-9a-f]{40}", "window": r"10m|1h|1d|7d|30d|90d|1y|all", "direction": r"in|out", "hops": r"[1-3]",
                   "chains": r"[a-z][a-z0-9]{1,15}(,[a-z][a-z0-9]{1,15}){0,15}"}
INVSCREEN_ROUTES = {"/api/public/em/holders-at": (("market", "snaps", "limit", "t", "block"), 300), "/api/public/em/wallet-trades": (("address", "market"), 60),
                    "/api/public/em/acquisition": (("address", "market", "chain"), 120), "/api/public/em/address-chains": (("address",), 120),
                    "/api/public/em/wallet-links": (("chain", "address"), 120), "/api/public/em/token-launch": (("market",), 3600),
                    "/api/public/em/launch-candles": (("market", "interval", "from", "to", "minUsd"), 600),   # launch-day bars from raw swaps
                    # P0 (2026-10-20): one line's transactions, a token's first-hour linked buying, how fresh each chain's data is, and the
                    # trace graph (public name trace-graph: /api/public/em/trace is the flow-map block's galaxy trace)
                    "/api/public/em/edge-evidence": (("chain", "a", "b"), 300), "/api/public/em/bundle-summary": (("market", "window"), 600),
                    "/api/public/em/freshness": (("chains",), 60),
                    "/api/public/em/trace-graph": (("chain", "address", "direction", "hops", "chains", "window", "from", "to"), 300)}
INVSCREEN_UP = {"/api/public/em/trace-graph": "/trace"}


def invscreen_route(path, qs):
    keys, ttl = INVSCREEN_ROUTES[path]
    if set(qs) - set(keys):
        raise HTTPError(400, "Unexpected parameter.")
    q = {k: v.lower() for k, v in qs.items() if v}
    if any(not re.fullmatch(INVSCREEN_PARAM[k.lower()], v) for k, v in q.items()):
        raise HTTPError(400, "Invalid parameter.")
    if path == "/api/public/em/holders-at":
        mid = q.get("market", "")
        if not MARKET.fullmatch(mid):
            raise HTTPError(400, "Use a registered chain and a valid token address.")
        chain = mid.split(":", 1)[0]
        # EMV2's enabled readers intercept this route before reaching here.
        # The legacy invscreen reader has analytics only for these two chains.
        # Mapping coverage is not proof that a holder ledger is served. Do not
        # forward another registered chain into its obsolete two-chain parser.
        if chain not in ("arbitrum", "robinhood"):
            name = next((r.get("name", chain) for r in REGISTRY if r["id"] == chain), chain)
            return {"schema": "holders-at/1", "market": mid, "chain": chain,
                    "available": False, "complete": False, "snaps": [],
                    "code": "holder_history_not_served", "retryable": False,
                    "reason": "Historical holder snapshots for %s are not available yet." % name}, 30
    up = INVSCREEN_UP.get(path, "/" + path.rsplit("/", 1)[1]) + "?" + urllib.parse.urlencode(sorted(q.items()))
    return CACHE.get("i:" + up, ttl, lambda: fetch_json(CFG["invscreen"], up, timeout=25)), ttl


def entity_route(path, qs):
    if set(qs) - set(ENTITY_ROUTES[path]):
        raise HTTPError(400, "Unexpected parameter.")
    q = {k: v for k, v in qs.items() if v}
    if any(not re.fullmatch(ENTITY_PARAM[k], v) for k, v in q.items()):
        raise HTTPError(400, "Invalid parameter.")
    up = path + "?" + urllib.parse.urlencode(sorted(q.items()))
    return CACHE.get("e:" + up, 300, lambda: fetch_json(CFG["search"], search_up(up), timeout=15))


def chain_ids():
    return {c["id"] for c in chains_status()["chains"]} or {"arbitrum", "robinhood"}


# ------------------------------------------------------------------------------------------------
# Money-flow map (wallet map): flowmap and wallet venues from em-charts, ego / trace from the galaxy and the label engine,
# live particles from one shared poller of the galaxy's decoded live actions (SSE plus a polling fallback).

FLOW_WINDOWS = ("1h", "24h", "7d")
FLOW_QUOTES = {"USDG", "USDC", "USDT", "USDT0", "USD₮0", "DAI", "USDC.E", "USDE", "PYUSD", "ETH", "WETH", "WBTC", "CBBTC", "FRAX"}
FLOW_ACTIONS = {"bridge_out": ("bridge", 1), "bridge_in": ("bridge", -1), "lp_add": ("lp", 1), "lp_remove": ("lp", -1), "lp_collect": ("lp", -1),
                "stake": ("stake", 1), "unstake": ("stake", -1), "claim": ("stake", -1), "perp_increase": ("perp", 1), "perp_decrease": ("perp", -1),
                "supply": ("lending", 1), "supply_collateral": ("lending", 1), "withdraw": ("lending", -1), "withdraw_collateral": ("lending", -1),
                "borrow": ("borrow", -1), "repay": ("borrow", 1), "liquidated": ("liquidation", -1)}
FLOW_DEX_FAMILIES = {"univ4", "univ3", "univ2", "pancake_v3", "algebra_npm", "univ3_npm", "algebra"}
EDGE_OF_CATEGORY = {"cex_hot_wallet": "cex", "cex_deposit": "cex", "bridge": "bridge", "dex_router": "swap", "dex_pool": "swap", "lending": "lending"}
GLSERVE = {"robinhood": (8710, 8712), "arbitrum": (8711, 8713), "arc": (8714, 8716),   # (full, delta) glserve per graph-served chain
           "linea": (8717,), "unichain": (8719,)}   # full core only until their delta cores (8718, 8721) run


def qid(chain, node):
    """Chain-qualified node id (<chain>:<kind>:<id>); ids from older payloads ('v:…') get the chain prefix."""
    return node if not node or node.startswith(chain + ":") else "%s:%s" % (chain, node)


def need_chain(qs):
    c = qs.get("chain")
    if c not in CHAINS and c not in GLSERVE:   # galaxy chains, plus chains served by a graph-loop core only
        raise HTTPError(400, "chain must be one of: %s." % ", ".join(sorted(set(CHAINS) | set(GLSERVE))))
    return c


def need_window(qs):
    w = qs.get("window") or "24h"
    if w not in FLOW_WINDOWS:
        raise HTTPError(400, "window must be 1h, 24h or 7d.")
    return w


def flow_map(chain, window):
    return charts("/api/public/em/flowmap?" + urllib.parse.urlencode({"chain": chain, "window": window}), ttl=60)


def flow_alias(chain):
    try:
        return flow_map(chain, "24h").get("alias") or {}
    except HTTPError:
        return {}


def flow_class(cat, alias, chain):
    cats = alias.get("classes") or {}
    if cat in cats:
        return qid(chain, cats[cat])
    return qid(chain, alias.get("contractDefault", "c:contract")) if cat else qid(chain, "c:retail")


def flow_particle(it, alias, chain):
    """One decoded live action -> {from, to} on the flow map (the same venue rule as flowmap.live_venue_sql in em-charts)."""
    act, fam, proto = it.get("action"), it.get("family"), it.get("protocol")
    key = proto or ("dex_other" if fam in FLOW_DEX_FAMILIES else fam or "other")
    venues = alias.get("venues") or {}
    if act == "swap":
        typ = "swap"
        sold = (it.get("symbol") or "").upper()
        d = 1 if sold in FLOW_QUOTES else -1   # paying a quote (USD, ETH) into the pool is money in
    elif act in FLOW_ACTIONS:
        typ, d = FLOW_ACTIONS[act]
        if fam == "erc4626":
            typ = "vault"
    else:
        return None
    venue = qid(chain, venues.get(key) or venues.get(proto or "") or ("v:other-dexs" if typ in ("swap", "lp") else "v:other-protocols"))
    cls = flow_class(it.get("wallet_category"), alias, chain)
    usd = r2(it.get("usd"))
    if not usd or usd <= 0:
        return None
    p = {"id": it.get("id"), "t": it.get("time"), "chain": chain, "type": typ, "usd": usd, "from": cls if d > 0 else venue, "to": venue if d > 0 else cls,
         "wallet": it.get("wallet"), "tx": it.get("tx_hash"), "text": (it.get("text") or "")[:160]}
    if typ == "bridge":
        p["via"] = qid(chain, "x:chains")
    return p


class FlowLive:
    """One upstream poll per chain (every 1.5 s) into a ring of mapped particles; SSE clients and /flowmap/live read the ring."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.ring = {c: [] for c in CHAINS}
        self.cursor = {c: None for c in CHAINS}
        self.started = False
        self.listeners = 0

    def start(self):
        with self.lock:
            if self.started:
                return
            self.started = True
        threading.Thread(target=self.run, name="flow-live", daemon=True).start()

    def run(self):
        while True:
            for chain in CHAINS:
                try:
                    self.poll(chain)
                except Exception as e:   # upstream hiccup: keep the ring, try again next tick
                    log("flow live %s: %r" % (chain, e))
            time.sleep(1.5)

    def poll(self, chain):
        q = {"chain": chain, "limit": 50}
        if self.cursor[chain] is not None:
            q["since"] = self.cursor[chain]
        d = fetch_json(CFG["galaxy"], "/api/live/recent?" + urllib.parse.urlencode(q), timeout=5)
        items = sorted(d.get("items") or [], key=lambda x: x.get("id") or 0)
        if self.cursor[chain] is None:   # first poll: start from now, no backlog burst
            self.cursor[chain] = max([x.get("id") or 0 for x in items] or [0])
            return
        if not items:
            return
        alias = flow_alias(chain)
        out = [p for p in (flow_particle(x, alias, chain) for x in items) if p]
        self.cursor[chain] = max(self.cursor[chain], max(x.get("id") or 0 for x in items))
        if out:
            with self.cond:
                r = self.ring[chain]
                r.extend(out)
                del r[:-400]
                self.cond.notify_all()

    def since(self, chain, after):
        with self.lock:
            r = list(self.ring[chain])
        return [p for p in r if (p.get("id") or 0) > after][-120:]

    def head(self, chain):
        with self.lock:
            r = self.ring[chain]
            return r[-1]["id"] if r else (self.cursor[chain] or 0)


FLOW_LIVE = FlowLive()
FLOW_STREAMS = threading.BoundedSemaphore(150)


def flow_ego(qs):
    """Who one wallet moves money with: DEX venues in the window (em-charts tape), decoded actions in the window (galaxy activity)
    and its largest counterparties since first seen (the full map, labelled). Node ids match the flow map (v:…), wallets are w:<address>."""
    addr, chain, window = need_addr(qs), need_chain(qs), need_window(qs)
    lim = min(24, max(4, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 14
    lo = int(time.time()) - {"1h": 3600, "24h": 86400, "7d": 7 * 86400}[window]
    card = None
    try:
        card = tool_wallet({"address": addr, "chain": chain})
    except HTTPError:
        pass
    center = qid(chain, "w:" + addr)
    nodes = {center: {"id": center, "chain": chain, "entity_id": "addr:" + addr, "kind": "wallet", "address": addr, "name": (card or {}).get("name") or (card or {}).get("categoryName") or "Wallet",
                      "category": (card or {}).get("category"), "center": True}}
    edges = {}

    def edge(target, typ, fwd, back, events=0, since=None):
        e = edges.setdefault((target, typ), {"id": "%s>%s>%s" % (center, target, typ), "source": center, "target": target, "type": typ,
                                              "src_chain": chain, "dst_chain": chain, "fwdUsd": 0.0, "backUsd": 0.0, "events": 0})
        e["fwdUsd"] += fwd or 0.0
        e["backUsd"] += back or 0.0
        e["events"] += events or 0
        if since:
            e["scope"] = since
    sources = []
    try:
        v = charts("/api/public/em/flowmap/wallet?" + urllib.parse.urlencode({"chain": chain, "address": addr, "window": window}), ttl=30)
        for x in v.get("venues") or []:
            nid = qid(chain, x["node"])
            nodes.setdefault(nid, {"id": nid, "chain": chain, "entity_id": venue_entity(chain, nid, x.get("name")), "kind": "venue", "group": x.get("kind"),
                                   "name": x.get("name"), "tokens": [t for t in (x.get("tokens") or []) if t.get("symbol")]})
            edge(nid, "swap", x.get("fwdUsd"), x.get("backUsd"), x.get("events"))
        sources.append("em-charts tape")
    except HTTPError:
        pass
    hit = find_wallet(addr, chain)
    alias = flow_alias(chain)
    if hit and hit.get("in_map"):
        try:
            act = galaxy("activity/%s/%d?limit=40" % (chain, int(hit["node_id"])), ttl=60)
            for a in act.get("actions") or []:
                if (a.get("time") or 0) < lo or a.get("action") not in FLOW_ACTIONS:
                    continue
                typ, d = FLOW_ACTIONS[a["action"]]
                key = a.get("protocol") or "other"
                node = qid(chain, (alias.get("venues") or {}).get(key) or "v:" + re.sub(r"[^a-z0-9]+", "-", (pname(key) or key).lower()).strip("-"))
                nodes.setdefault(node, {"id": node, "chain": chain, "entity_id": venue_entity(chain, node, human(key) or pname(key)), "kind": "venue",
                                        "group": "lending" if typ in ("lending", "borrow", "liquidation") else typ, "name": human(key) or pname(key)})
                u = r2(a.get("usd")) or 0.0
                edge(node, typ, u if d > 0 else 0.0, u if d < 0 else 0.0, 1)
            sources.append("galaxy decoded actions")
        except HTTPError:
            pass
        try:
            nb = galaxy("neighbors/%s/%d?limit=%d" % (chain, int(hit["node_id"]), lim), ttl=120)
            for n in nb.get("neighbors") or []:
                a = (n.get("address") or "").lower()
                if not ADDR.match(a) or a == addr:
                    continue
                cat = n.get("category") or "unknown"
                nid = qid(chain, "w:" + a)
                nodes.setdefault(nid, {"id": nid, "chain": chain, "entity_id": "addr:" + a, "kind": "wallet", "address": a, "category": cat,
                                       "name": human(n.get("entity_name")) or CATEGORY.get(cat, "Wallet"), "assets": (n.get("assets") or [])[:3]})
                edge(nid, EDGE_OF_CATEGORY.get(cat, "transfer"), r2(n.get("out_usd")), r2(n.get("in_usd")), n.get("n"), since="map")
            sources.append("galaxy map counterparties (since first seen)")
        except HTTPError:
            pass
    if not any(e.get("scope") == "map" for e in edges.values()):
        # not among the drawn wallets (or no map edges): its counterparties from the label engine's full graph (transfer counts, no USD)
        for port in GLSERVE[chain]:
            try:
                code, _, body = fetch(("127.0.0.1", port), "/tools/neighbors?" + urllib.parse.urlencode({"addr": addr, "dir": "both", "limit": lim}), timeout=3)
            except HTTPError:
                continue
            if code != 200:
                continue
            for n in (json.loads(body).get("neighbors") or []):
                a = (n.get("ref") or "").lower()
                if not ADDR.match(a) or a == addr or a == "0x0000000000000000000000000000000000000000":
                    continue
                cat = n.get("category") or "unknown"
                nid = qid(chain, "w:" + a)
                nodes.setdefault(nid, {"id": nid, "chain": chain, "entity_id": "addr:" + a, "kind": "wallet", "address": a, "category": cat,
                                       "name": human(n.get("entity")) or CATEGORY.get(cat, "Wallet")})
                e = edges.setdefault((nid, "graph"), {"id": "%s>%s>graph" % (center, nid), "source": center, "target": nid, "src_chain": chain, "dst_chain": chain,
                                                      "type": EDGE_OF_CATEGORY.get(cat, "transfer"), "fwdUsd": 0.0, "backUsd": 0.0, "events": 0,
                                                      "countOnly": True, "scope": "graph", "direction": {"out": "sent", "in": "received"}.get(n.get("dir"), "both ways")})
                e["events"] += n.get("n") or 0
            sources.append("label engine graph counterparties (transfer counts)")
            break
    out_edges = []
    for e in edges.values():
        e["fwdUsd"], e["backUsd"] = r2(e["fwdUsd"]) or 0.0, r2(e["backUsd"]) or 0.0
        e["usd"] = r2(e["fwdUsd"] + e["backUsd"]) or 0.0
        e["netUsd"] = r2(e["fwdUsd"] - e["backUsd"]) or 0.0
        if e["usd"] > 0 or e.get("countOnly"):
            out_edges.append(e)
    out_edges.sort(key=lambda e: (-(e["usd"] or 0), -(e.get("events") or 0)))
    keep = {center} | {e["target"] for e in out_edges[:lim * 2]}
    return {"kind": "ego", "schema": "flowmap/2", "chain": chain, "chains": [chain], "address": addr, "window": window, "from": lo, "to": int(time.time()), "wallet": card,
            "nodes": [n for k, n in nodes.items() if k in keep], "edges": out_edges[:lim * 2], "sources": sources,
            "coverage": {"notes": ["venues: DEX trades in the window where this wallet is the transaction sender, and its decoded actions",
                                   "counterparties: the largest transfer partners since the wallet was first seen (scope 'map')"]}}


def venue_entity(chain, nid, name):
    if not name or "·" in name or nid.endswith((":v:other-dexs", ":v:other-protocols")):
        return "venue:%s:%s" % (chain, nid.split(":v:", 1)[-1])   # unidentified venues and groupings stay chain-scoped
    return "protocol:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def glserve_path(chain, a, b, depth=6):
    for port in GLSERVE[chain]:
        try:
            code, _, body = fetch(("127.0.0.1", port), "/tools/path?" + urllib.parse.urlencode(
                {"from": a, "to": b, "max_depth": depth, "undirected": 1}), timeout=4)
        except HTTPError:
            continue
        if code == 200:
            d = json.loads(body)
            if d.get("found") or port == GLSERVE[chain][-1]:
                return d
    return None


def flow_trace(qs):
    """Follow the money: a path between two wallets (label engine, typed BFS over the full graph), or, with only 'from',
    the largest outflows for up to 3 hops (the map's counterparties)."""
    chain, a = need_chain(qs), need_addr(qs, "from")
    if qs.get("to"):
        b = need_addr(qs, "to")
        d = CACHE.get("trace:%s:%s:%s" % (chain, a, b), 300, lambda: glserve_path(chain, a, b))
        if not d:
            raise HTTPError(503, "The label engine is not available; try again shortly.")
        hops = d.get("path") or []
        nodes, edges = [], []
        for i, h in enumerate(hops):
            ref = (h.get("ref") or "").lower()
            nodes.append({"id": qid(chain, "w:" + ref), "chain": chain, "entity_id": "addr:" + ref, "kind": "wallet", "address": ref, "category": h.get("category"),
                          "name": CATEGORY.get(h.get("category") or "unknown", "Wallet"), "confidence": h.get("confidence")})
            if i:
                edges.append({"id": "%s>%s" % (nodes[i - 1]["id"], nodes[i]["id"]), "source": nodes[i - 1]["id"], "target": nodes[i]["id"],
                              "src_chain": chain, "dst_chain": chain,
                              "type": {"erc20": "transfer", "native": "transfer", "dex_swap": "swap", "bridge": "bridge"}.get(h.get("edge_in"), "transfer"),
                              "edge": h.get("edge_in"), "hop": i})
        return {"kind": "trace", "schema": "flowmap/2", "mode": "path", "chain": chain, "from": a, "to": b, "found": bool(d.get("found")), "hops": d.get("hops"),
                "nodes": nodes, "edges": edges, "visited": d.get("visited")}
    hops = min(3, max(1, int(qs["hops"]))) if str(qs.get("hops", "")).isdigit() else 2
    fan = (3, 2, 2)
    W = lambda x: qid(chain, "w:" + x)
    nodes = {W(a): {"id": W(a), "chain": chain, "entity_id": "addr:" + a, "kind": "wallet", "address": a, "center": True}}
    edges, frontier = [], [a]
    for depth in range(hops):
        nxt = []
        for x in frontier:
            if chain not in CHAINS:   # graph-loop-only chain (no galaxy stars): the label engine's outgoing counterparties, by transfer count
                nb = glserve_neighbors(chain, x, 16, "out") or {}
                outs = [{"address": n.get("ref"), "category": n.get("category"), "entity_name": n.get("entity"), "out_n": n.get("n"), "count_only": True}
                        for n in nb.get("neighbors") or [] if n.get("dir") == "out" and W((n.get("ref") or "").lower()) not in nodes][:fan[depth]]
            else:
                hit = find_wallet(x, chain)
                if not hit or not hit.get("in_map"):
                    continue
                nb = galaxy("neighbors/%s/%d?limit=16" % (chain, int(hit["node_id"])), ttl=120)
                outs = sorted([n for n in nb.get("neighbors") or [] if (n.get("out_usd") or 0) > 0 and W((n.get("address") or "").lower()) not in nodes],
                              key=lambda n: -(n.get("out_usd") or 0))[:fan[depth]]
            for n in outs:
                y = (n.get("address") or "").lower()
                if not ADDR.match(y) or W(y) in nodes:   # a route never loops back to a wallet already on it
                    continue
                cat = n.get("category") or "unknown"
                nodes.setdefault(W(y), {"id": W(y), "chain": chain, "entity_id": "addr:" + y, "kind": "wallet", "address": y, "category": cat,
                                        "name": human(n.get("entity_name")) or CATEGORY.get(cat, "Wallet")})
                edges.append({"id": "%s>%s" % (W(x), W(y)), "source": W(x), "target": W(y), "src_chain": chain, "dst_chain": chain,
                              "type": EDGE_OF_CATEGORY.get(cat, "transfer"),
                              "fwdUsd": r2(n.get("out_usd")), "usd": r2(n.get("out_usd")), "events": n.get("out_n"), "hop": depth + 1,
                              "assets": (n.get("assets") or [])[:3]})
                if n.get("count_only"):
                    edges[-1].update(fwdUsd=0.0, usd=0.0, countOnly=True)   # transfer counts, no USD (as the ego view's graph edges)
                if cat not in ("cex_hot_wallet", "cex_deposit", "dex_pool", "dex_router", "bridge", "lending", "token_contract"):
                    nxt.append(y)   # hubs end the route: money into an exchange, pool or bridge is not followed further
        frontier = nxt
    return {"kind": "trace", "schema": "flowmap/2", "mode": "follow", "chain": chain, "from": a, "hops": hops, "nodes": list(nodes.values()), "edges": edges,
            "coverage": {"notes": ["largest outflows per hop from the map's counterparties (since first seen); exchanges, pools, routers and "
                                   "bridges end a route"] + (["%s: the label engine's outgoing counterparties ranked by transfer count (no USD)" % chain]
                                                             if chain not in CHAINS else [])}}


def live_chain(qs):
    chain=opt_chain(qs)
    if chain is None:
        raise HTTPError(400, "A registered chain is required.")
    return chain


def flow_route(handler, path, qs, ip):
    if path in INV_ROUTES:
        return investigate_route(handler, path, qs, ip)
    if path in TRADER_ROUTES:
        return traders_route(handler, path, qs, ip)
    if path == "/api/public/em/flowmap":
        request_limit(handler, ip, "data")
        chain, window = need_chain(qs), need_window(qs)
        return handler.send_json(200, flow_map(chain, window), cache="public, max-age=60")
    if path == "/api/public/em/flowmap/wallet":
        request_limit(handler, ip, "data")
        q = {"chain": need_chain(qs), "address": need_addr(qs), "window": need_window(qs)}
        return handler.send_json(200, charts("/api/public/em/flowmap/wallet?" + urllib.parse.urlencode(q), ttl=30), cache="public, max-age=30")
    if path == "/api/public/em/ego":
        request_limit(handler, ip, "tools")
        key = "ego:" + urllib.parse.urlencode(sorted(qs.items()))
        return handler.send_json(200, CACHE.get(key, 30, lambda: flow_ego(qs)), cache="public, max-age=30")
    if path == "/api/public/em/trace":
        request_limit(handler, ip, "tools")
        key = "trace:" + urllib.parse.urlencode(sorted(qs.items()))
        return handler.send_json(200, CACHE.get(key, 120, lambda: flow_trace(qs)), cache="public, max-age=60")
    if path == "/api/public/em/flowmap/live":
        request_limit(handler, ip, "data")
        chain = live_chain(qs)
        if chain not in CHAINS:
            return handler.send_json(200, flow_unavailable(chain), cache="no-store")
        FLOW_LIVE.start()
        after = int(qs["since"]) if str(qs.get("since", "")).isdigit() else FLOW_LIVE.head(chain)
        items = FLOW_LIVE.since(chain, after)
        return handler.send_json(200, {"chain": chain, "items": items, "cursor": max([p["id"] for p in items] or [after])}, cache="no-store")
    if path == "/api/public/em/holders/clusters":
        request_limit(handler, ip, "tools")
        key = "clusters:" + urllib.parse.urlencode(sorted(qs.items()))
        return handler.send_json(200, CACHE.get(key, 600, lambda: holder_clusters(qs)), cache="public, max-age=300")
    if path == "/api/public/em/flowmap/stream":
        LIMIT.take(ip, "stream")
        chain = live_chain(qs)
        return flow_stream(handler, chain)
    return None


def flow_unavailable(chain):
    return {"chain":chain,"available":False,"complete":False,"code":"live_flow_not_served",
            "reason":"Live flow particles are not served for this chain.","items":[],"cursor":None}


def flow_stream(handler, chain):
    """SSE: 'particles' events with the newest mapped live actions, a comment heartbeat every 15 s; closes after 5 minutes (the client
    reconnects with Last-Event-ID semantics through ?since)."""
    if chain not in CHAINS:
        return handler.send_json(200, flow_unavailable(chain), cache="no-store")
    if not FLOW_STREAMS.acquire(blocking=False):
        raise HTTPError(503, "Too many live map viewers; the map falls back to polling.", retry=10)
    FLOW_LIVE.start()
    try:
        handler.send_response(200)
        for k, v in {**handler.cors(), "Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no",
                     "Connection": "close"}.items():
            handler.send_header(k, v)
        handler.end_headers()
        handler.close_connection = True
        cursor, beat, end = FLOW_LIVE.head(chain), time.time(), time.time() + 300
        handler.wfile.write(b"retry: 3000\n\n")
        handler.wfile.flush()
        while time.time() < end:
            with FLOW_LIVE.cond:
                FLOW_LIVE.cond.wait(timeout=2)
            items = FLOW_LIVE.since(chain, cursor)
            if items:
                cursor = items[-1]["id"]
                data = json.dumps({"chain": chain, "items": items, "cursor": cursor}, separators=(",", ":"))
                handler.wfile.write(("id: %d\nevent: particles\ndata: %s\n\n" % (cursor, data)).encode())
                handler.wfile.flush()
                beat = time.time()
            elif time.time() - beat > 15:
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                beat = time.time()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        FLOW_STREAMS.release()


# ------------------------------------------------------------------------------------------------
# Holder clusters (Bubblemaps-style): a token's top holders, the links between them and the groups they form.

HOLDERS_DB = "/srv/skew/em-charts/data/{chain}/analytics/holders_top.sqlite3"
FUNDER_OK = {"eoa_user", "unknown", "eoa_whale", None}
FUNDER_MAX_OUT = 60          # a wallet that funded more addresses than this is a faucet / distributor, not a shared owner


def glserve_neighbors(chain, addr, limit=48, direction="both"):
    for port in GLSERVE[chain]:
        try:
            code, _, body = fetch(("127.0.0.1", port), "/tools/neighbors?" + urllib.parse.urlencode({"addr": addr, "dir": direction, "limit": limit}), timeout=3)
        except HTTPError:
            continue
        if code == 200:
            return json.loads(body)
    return None


def glserve_label(chain, addr):
    for port in GLSERVE[chain]:
        try:
            code, _, body = fetch(("127.0.0.1", port), "/labels?" + urllib.parse.urlencode({"addr": addr}), timeout=2)
        except HTTPError:
            continue
        if code == 200:
            d = json.loads(body)
            if not d.get("error"):
                return d
    return None


def holder_clusters(qs):
    """Top holders of one market with their share of supply, the links between holder wallets and the clusters they form:
    transfer  the two wallets moved tokens or ETH between each other (label-engine graph, all time)
    deposit   both sent to the same exchange deposit address (deposit addresses belong to one exchange customer)
    funder    both received ETH from the same ordinary wallet that funded at most FUNDER_MAX_OUT addresses
    Venues (pools, lenders, bridges, vaults), exchanges and the issuer are listed but never linked or clustered."""
    mid = (qs.get("market") or "").lower()
    if not MARKET.match(mid):
        raise HTTPError(400, "market must be <arbitrum|robinhood>:<0x token address>.")
    chain, token = mid.split(":", 1)
    lim = min(100, max(10, int(qs["limit"]))) if str(qs.get("limit", "")).isdigit() else 60
    try:
        db = sqlite3.connect("file:%s?mode=ro" % HOLDERS_DB.format(chain=chain), uri=True, timeout=5)
        rows = db.execute("SELECT rank, holder, amount, pct, usd, class, bucket, entity, role FROM holders WHERE market = ? ORDER BY rank LIMIT ?",
                          (mid, lim)).fetchall()
        sup = db.execute("SELECT supply, holders FROM supply WHERE market = ?", (mid,)).fetchone()
        upd = (db.execute("SELECT value FROM meta WHERE key = 'updated_at'").fetchone() or [None])[0]
        db.close()
    except sqlite3.Error:
        raise HTTPError(503, "Holder data is being built; retry in a few minutes.")
    if not rows:
        raise HTTPError(404, "No holder data for this market.")
    em = CATALOG.by_id.get(mid) or {}
    holders, wallets = [], {}
    for rank, h, amount, pct, usd_, cl, bucket, ent, role in rows:
        nid = qid(chain, "w:" + h)
        x = {"id": nid, "chain": chain, "entity_id": "addr:" + h, "address": h, "rank": rank, "amount": r2(amount), "pct": round((pct or 0) * 100, 4),
             "usd": r2(usd_), "class": cl, "bucket": bucket, "role": role, "name": human(ent), "cluster": None}
        holders.append(x)
        if role == "wallet":
            wallets[h] = x
        elif not x["name"]:
            lab = glserve_label(chain, h)
            if lab and lab.get("entity"):
                x["name"], x["nameSource"] = human(lab["entity"]) or lab["entity"], lab.get("source")
    links, seen = [], set()
    via_deposit, via_funder = defaultdict(set), defaultdict(set)
    for h, x in wallets.items():
        nb = glserve_neighbors(chain, h)
        if not nb:
            continue
        node = nb.get("node") or {}
        x["category"] = node.get("category")
        x["categoryName"] = CATEGORY.get(node.get("category") or "unknown")
        if not x["name"]:
            lab = glserve_label(chain, h)
            if lab and lab.get("entity"):
                x["name"], x["nameSource"] = lab["entity"], lab.get("source")
        for n in nb.get("neighbors") or []:
            y = (n.get("ref") or "").lower()
            if not ADDR.match(y) or y == h or y == "0x0000000000000000000000000000000000000000":
                continue
            if y in wallets:
                key = tuple(sorted((h, y)))
                if key not in seen:
                    seen.add(key)
                    links.append({"source": qid(chain, "w:" + key[0]), "target": qid(chain, "w:" + key[1]), "kind": "transfer", "n": n.get("n"),
                                  "edge": n.get("edge"), "evidence": "%s transfer%s (%s)" % (n.get("n"), "" if n.get("n") == 1 else "s", n.get("edge"))})
            elif n.get("category") == "cex_deposit" and n.get("dir") == "out":
                via_deposit[y].add(h)
            elif n.get("dir") == "in" and n.get("edge") == "native" and n.get("category") in FUNDER_OK:
                via_funder[y].add(h)
    for via, members, kind in [(v, m, "deposit") for v, m in via_deposit.items()] + [(v, m, "funder") for v, m in via_funder.items()]:
        if len(members) < 2:
            continue
        if kind == "funder":
            info = glserve_neighbors(chain, via, limit=1) or {}
            if (info.get("out_degree") or 0) > FUNDER_MAX_OUT:
                continue
        ms = sorted(members)
        for y in ms[1:]:
            links.append({"source": qid(chain, "w:" + ms[0]), "target": qid(chain, "w:" + y), "kind": kind, "via": via,
                          "evidence": ("both sent to exchange deposit address " if kind == "deposit" else "both received ETH from ") + via})
    parent = {h: h for h in wallets}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for l in links:
        a, b = find(l["source"].rsplit(":", 1)[1]), find(l["target"].rsplit(":", 1)[1])
        if a != b:
            parent[b] = a
    groups = defaultdict(list)
    for h in wallets:
        groups[find(h)].append(wallets[h])
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ids = {m["id"] for m in members}
        kinds = sorted({l["kind"] for l in links if l["source"] in ids})
        pct = sum(m["pct"] for m in members)
        clusters.append({"members": [m["id"] for m in sorted(members, key=lambda m: m["rank"])], "size": len(members), "pct": round(pct, 4),
                         "usd": r2(sum(m["usd"] or 0 for m in members)), "kinds": kinds})
    clusters.sort(key=lambda c: -c["pct"])
    for i, c in enumerate(clusters, 1):
        c["id"] = "%s:cluster:%d" % (mid, i)
        words = {"transfer": "direct transfers", "deposit": "a shared exchange deposit address", "funder": "a shared funder"}
        c["label"] = "%d linked wallets hold %.2f%% of supply · %s" % (c["size"], c["pct"], ", ".join(words[k] for k in c["kinds"]))
        for mid_ in c["members"]:
            for h in holders:
                if h["id"] == mid_:
                    h["cluster"] = c["id"]
    top_pct = sum(h["pct"] for h in holders)
    return {"kind": "holder_clusters", "schema": "clusters/1", "market": mid, "chain": chain, "token": token, "symbol": (em.get("row") or {}).get("symbol"),
            "supply": r2(sup[0]) if sup else None, "holderCount": sup[1] if sup else None, "updatedAt": int(upd) if upd else None,
            "holders": holders, "links": links, "clusters": clusters,
            "summary": {"topPct": round(top_pct, 2), "walletPct": round(sum(h["pct"] for h in wallets.values()), 2),
                        "clusteredPct": round(sum(c["pct"] for c in clusters), 2), "clusters": len(clusters),
                        "largestCluster": clusters[0]["label"] if clusters else None},
            "coverage": {"notes": ["holders: balances from the map snapshot plus every transfer since (the Economy panel's token state), top %d" % lim,
                                   "links: label-engine graph (all-time transfers, up to 48 neighbours per wallet); shared exchange deposit addresses; "
                                   "shared ETH funder that funded at most %d addresses" % FUNDER_MAX_OUT,
                                   "venues, exchanges and the issuer are shown but never linked or clustered"]}}


# ------------------------------------------------------------------------------------------------
# Range investigation (chart drag -> what happened): proxied to em-investigate on loopback (raw fills, transfers, cluster evidence).

INVESTIGATE = ("127.0.0.1", 8898)
INV_ROUTES = {"/api/public/em/investigate": ("/investigate", ("market", "from", "to", "limit"), 120),
              "/api/public/em/cluster": ("/cluster", ("cluster",), 120),
              "/api/public/em/cluster/first-entry": ("/cluster/first-entry", ("market", "cluster", "wallets"), 600),
              "/api/public/em/protocols": ("/protocols", ("chain", "sort", "limit"), 60),
              "/api/public/em/protocol": ("/protocol", ("chain", "id"), 60),
              "/api/public/em/smart-money": ("/smart-money", ("chain", "window", "limit"), 300),
              "/api/public/em/search": ("/search", ("q", "limit", "chain"), 60)}
INV_PARAM = {"market": (r"(%s):0x[0-9a-f]{40}" % "|".join(EVM_CHAINS), "market must be <chain>:<0x token address> with a registry chain."),
             "from": (r"\d{1,13}", "from must be a number."), "to": (r"\d{1,13}", "to must be a number."), "limit": (r"\d{1,3}", "limit must be a number."),
             "cluster": (r"k[0-9a-f]{10}", "cluster is an id from investigate (k + 10 hex)."),
             "wallets": (r"0x[0-9a-f]{40}(,0x[0-9a-f]{40}){0,19}", "wallets is up to 20 comma-separated 0x addresses."),
             "chain": ("|".join(EVM_CHAINS), "chain must be a registry chain id."),
             "sort": (r"liquidity|volume_24h|volume_7d|users_7d", "sort must be liquidity, volume_24h, volume_7d or users_7d."),
             "window": (r"7d|30d|all", "window must be 7d, 30d or all."), "id": (r"[a-z0-9][a-z0-9-]{0,63}", "id is a protocol id from /protocols."),
             "address": (r"0x[0-9a-f]{40}", "address is a 0x wallet address."),
             "q": (r"[^\x00-\x1f]{1,100}", "q is 1-100 printable characters.")}
# per-token top traders and one wallet's PnL: em-charts reads (traders_api), validated here and passed through
TRADER_ROUTES = {"/api/public/em/top-traders": (("market", "window", "limit"), 120), "/api/public/em/wallet-pnl": (("chain", "address"), 120)}
INV_PASS = (400, 404, 422, 429, 503)   # investigate answers the client should see as is (too heavy, busy, budget spent)
INV_KEYS = ("detail", "error", "anchor", "fills", "maxFills", "suggestedMaxSeconds", "budgetSeconds")


class _Upstream(Exception):
    def __init__(self, code, body):
        super().__init__(code)
        self.code, self.body = code, body


def check_params(qs, keys):
    if set(qs) - set(keys):
        raise HTTPError(400, "Unexpected parameter.")
    for k, v in qs.items():
        if not re.fullmatch(INV_PARAM[k][0], (v or "").lower()):
            raise HTTPError(400, INV_PARAM[k][1])


def traders_route(handler, path, qs, ip):
    keys, ttl = TRADER_ROUTES[path]
    check_params(qs, keys)
    if "limit" in qs and int(qs["limit"]) > 100:
        raise HTTPError(400, "limit is at most 100.")
    request_limit(handler, ip, "data")
    q = urllib.parse.urlencode(sorted((k, v.lower()) for k, v in qs.items()))
    return handler.send_json(200, clean(charts(path + "?" + q, ttl=ttl)), cache="public, max-age=60")


def investigate_route(handler, path, qs, ip):
    up, keys, ttl = INV_ROUTES[path]
    check_params(qs, keys)
    request_limit(handler, ip, "data" if path == "/api/public/em/search" else "tools")   # a search box sends a request per keystroke
    q = urllib.parse.urlencode(sorted(qs.items()))

    def load():
        code, _, body = fetch(INVESTIGATE, up + "?" + q, timeout=40)   # em-investigate answers within its own 20 s budget
        d = json.loads(body)
        if code != 200:
            raise _Upstream(code, d)
        return d
    try:
        return handler.send_json(200, CACHE.get("inv:" + path + "?" + q, ttl, load, wait=40), cache="public, max-age=60")
    except _Upstream as e:
        if e.code not in INV_PASS:
            raise HTTPError(502, "Investigation is unavailable.")
        body = {k: e.body[k] for k in INV_KEYS if k in e.body} or {"detail": "Investigation is unavailable."}
        return handler.send_json(e.code, body, cache="no-store", extra={"Retry-After": "5"} if e.code in (429, 503) else None)

FLOW_PATHS = ('/api/public/em/flowmap', '/api/public/em/flowmap/wallet', '/api/public/em/flowmap/live', '/api/public/em/flowmap/stream', '/api/public/em/ego', '/api/public/em/trace', '/api/public/em/holders/clusters', '/api/public/em/investigate', '/api/public/em/cluster', '/api/public/em/cluster/first-entry', '/api/public/em/protocols', '/api/public/em/protocol', '/api/public/em/smart-money', '/api/public/em/top-traders', '/api/public/em/wallet-pnl', '/api/public/em/search')


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
    ap.add_argument("--xc", default="127.0.0.1:8905")
    ap.add_argument("--search", default="127.0.0.1:8901")
    ap.add_argument("--invscreen", default="127.0.0.1:8906")
    ap.add_argument("--chains", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "registry", "chains.json"))
    ap.add_argument("--protocol-logos", default=None, help="protocol logo index (default: <static>/assets/em/protocol-logos/index.json)")
    ap.add_argument("--static", default=None)
    ap.add_argument("--allow-origin", action="append", default=[])
    ap.add_argument("--account-config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "account-bridge.json"))
    ap.add_argument("--search-paths", choices=("bridge", "investigate"), default="bridge",
                    help="investigate: --search is em-investigate itself (/search, /entities, /entity, /wallet-pnl-xc)")
    ap.add_argument("--logs", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "::1"):
        raise SystemExit("The bridge listens on loopback only; nginx publishes it.")
    CFG.update(mode=a.mode, console=hostport(a.console), galaxy=hostport(a.galaxy), charts=hostport(a.charts), resolver=hostport(a.resolver), xc=hostport(a.xc), search=hostport(a.search), invscreen=hostport(a.invscreen),
               galaxy_db=a.galaxy_db, focus=a.focus, icons=a.icons, evm_catalog=a.evm_catalog, chains=a.chains, static=a.static, origins={"https://ticking.dev", *a.allow_origin},
               protocol_logos=a.protocol_logos, self_host=a.host, self_port=a.port, account_config=a.account_config,
               search_paths=a.search_paths)
    if a.mode == "staging" and not a.static:
        raise SystemExit("--static is required in staging mode")
    os.makedirs(a.logs, exist_ok=True)
    LOG = open(os.path.join(a.logs, "bridge.log"), "a", buffering=1)
    ACCESS = open(os.path.join(a.logs, "access.log"), "a", buffering=1)
    mimetypes.add_type("image/webp", ".webp")
    mimetypes.add_type("font/woff2", ".woff2")
    threading.Thread(target=CATALOG.refresh, daemon=True).start()
    if os.environ.get("EM_BRIDGE_NO_SUMMARY_BUILD") != "1":
        threading.Thread(target=SUMMARIES.build, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    srv.request_queue_size = 64
    log("listening %s:%d mode=%s" % (a.host, a.port, a.mode))
    srv.serve_forever()


# ------------------------------------------------------------------------------------------------
# EMV2 shadow (emv2/bridge/apply_bridge_emv2.py; design: design/EM-ENGINE-V2.md §8 API, §10 dual run). Per route and chain the
# bridge answers from the current services (old), answers the same and compares emv2-serve in the background (shadow), or
# answers from emv2-serve and falls back to the current services on any V2 failure (new).
# No emv2-bridge.json next to this file (or $EM_EMV2_CONFIG) = every route old: each request takes exactly the path it took
# before this block. The file is reread every 30 s, so switching a route needs no restart:
#   {"serve": "127.0.0.1:8740", "default": "old", "routes": {"holders-at": {"arc": "shadow", "*": "old"}, "trace": "shadow"},
#    "sample": 1.0, "min_gap_ms": 0, "timeout_ms": 2000, "queue": 128, "log_max_mb": 64, "log_keep": 5, "align_old": true}
# Shadow never touches the answer: the old body is sent first, then a bounded queue (dropped when full) asks emv2-serve the same
# path and query and appends one JSONL line per comparison to <logs>/emv2-shadow.jsonl (rotated by size). Integers and strings
# must match exactly, other numbers within a relative 1e-6, lists in order; time-of-answer fields are not compared.
# Time: V2 phase 1 is built to the map's last block, the current services add daily deltas. Every V2 answer carries `as_of`
# (block and time, or header X-EMV2-As-Of: <block>:<time>). Routes with a time parameter (holders-at block, trace-graph to)
# are asked of the current upstream again at that point (align_old); elsewhere old list items after as_of are left out. A
# difference left over while the old answer shows activity after as_of is `time_skew`, counted apart and never critical.

import queue as _emv2_queue
import random as _emv2_random

EMV2_PATH = os.environ.get("EM_EMV2_CONFIG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "emv2-bridge.json")
EMV2 = {"loaded": 0.0, "cfg": None, "q": None, "lock": threading.Lock(), "last": {}, "stats": defaultdict(int), "stats_at": time.time()}
EMV2_MODES = ("old", "shadow", "new")
EMV2_ROUTES = {"/api/public/em/holders-at": "holders-at", "/api/public/em/wallet-trades": "wallet-trades",
               "/api/public/em/acquisition": "acquisition", "/api/public/em/address-chains": "address-chains",
               "/api/public/em/trace-graph": "trace-graph", "/api/public/em/tools/wallet": "tools/wallet",
               "/api/public/em/tools/holders": "tools/holders", "/api/public/em/tools/trace": "tools/trace"}
EMV2_NEIGHBORS = re.compile(r"/api/public/em/g/neighbors/([a-z][a-z0-9]{1,15})/\d{1,9}")
EMV2_CHAIN = re.compile(r"[a-z][a-z0-9]{1,15}")
EMV2_IGNORE = {"generated_at", "generatedAt", "as_of_now", "now", "served_at", "servedAt", "cached", "cache", "cache_age_s", "age_s",
               "took_ms", "elapsed_ms", "latency_ms", "request_id", "engine", "build", "build_id", "tier", "logo", "logos", "logo_url",
               "logoUrl", "icon", "icon_url", "credit", "logoCredit", "locked", "elapsed_ms", "as_of", "inputHash", "dataVersion"}
# Per route: fields whose source differs by design in V2 (left out), and phase-1 gaps where V2 answers null (counted apart).
EMV2_SKIP = {"holders-at": re.compile(r"(^|\.)snaps\[\d+\]\.source$"), "address-chains": re.compile(r"(^|\.)kindBasis$"),
             "trace": re.compile(r"(^|\.)asOf\[\d+\]\.source$"), "trace-graph": re.compile(r"^notes(\[\d+\])?$"),
             "path": re.compile(r"^(visited|max_depth|budget_exhausted)$|^edges\[\d+\]\.(edge|type)$|^nodes\[\d+\]\.(confidence|type|entity|hub)$")}
EMV2_GAPS = {"holders-at": re.compile(r"(^|\.)class$"), "address-chains": re.compile(r"(^|\.)entity$")}
EMV2_CRITICAL = ("status", "missing", "type", "length", "int", "raw", "value")
EMV2_BLOCK_KEYS = ("block", "blockNumber", "block_number", "lastBlock", "last_block")
EMV2_TIME_KEYS = ("time", "ts", "timestamp", "lastTime", "last_time", "lastSeen", "last_seen")
EMV2_ALIGN = {"/api/public/em/holders-at": ("block", ("block", "t")), "/api/public/em/trace-graph": ("to", ("to",))}


def emv2_cfg():
    if time.time() - EMV2["loaded"] > 30:
        cfg = None
        try:
            with open(EMV2_PATH) as f:
                raw = json.load(f)
            routes = raw.get("routes") or {}
            modes = [m for r in routes.values() for m in (r.values() if isinstance(r, dict) else [r])] + [raw.get("default", "old")]
            if all(m in EMV2_MODES for m in modes):
                cfg = {"addr": hostport(raw.get("serve", "127.0.0.1:8740")), "default": raw.get("default", "old"), "routes": routes,
                       "sample": min(1.0, max(0.0, float(raw.get("sample", 1.0)))), "gap": max(0.0, float(raw.get("min_gap_ms", 0)) / 1000),
                       "timeout": min(5.0, max(0.1, float(raw.get("timeout_ms", 2000)) / 1000)),
                       "align_old": bool(raw.get("align_old", True)), "queue": min(1024, max(1, int(raw.get("queue", 128)))),
                       "log_max": min(1024, max(1, int(raw.get("log_max_mb", 64)))) << 20, "log_keep": min(20, max(1, int(raw.get("log_keep", 5))))}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            cfg = None
        EMV2.update(loaded=time.time(), cfg=cfg)
    return EMV2["cfg"]


def emv2_route(path, qs):
    """(route, chain) for a request this block may handle, else None. chain is '*' when the route spans chains."""
    m = EMV2_NEIGHBORS.fullmatch(path)
    if m:
        return "neighbors", m.group(1)
    if path == "/api/public/em/trace":
        return ("path" if qs.get("to") else "trace"), (qs.get("chain") or "?").lower()
    route = EMV2_ROUTES.get(path)
    if not route:
        return None
    if route == "address-chains":
        return route, "*"
    chain = (qs.get("chain") or "").lower()
    if not chain and ":" in (qs.get("market") or ""):
        chain = qs["market"].split(":", 1)[0].lower()
    return route, (chain if EMV2_CHAIN.fullmatch(chain or "") else "?")


def emv2_mode(cfg, route, chain):
    r = cfg["routes"].get(route)
    if isinstance(r, dict):
        return r.get(chain) or r.get("*") or cfg["default"]
    return r or cfg["default"]


def emv2_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def emv2_diff(a, b, path, out, seen):
    """Field-by-field comparison of the old answer `a` with the V2 answer `b`; appends (kind, path, old, new) to out."""
    if seen[0] > 400_000:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k in EMV2_IGNORE:
                continue
            p = "%s.%s" % (path, k) if path else str(k)
            if k not in b:
                out.append(("missing", p, a[k], None))
            elif k not in a:
                out.append(("extra", p, None, b[k]))
            else:
                emv2_diff(a[k], b[k], p, out, seen)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(("length", path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            emv2_diff(x, y, "%s[%d]" % (path, i), out, seen)
        return
    seen[0] += 1
    if emv2_num(a) and emv2_num(b):
        if a == b:
            return
        if isinstance(a, float) or isinstance(b, float):
            if abs(a - b) <= 1e-6 * max(abs(a), abs(b)):
                return
            out.append(("value", path, a, b))
        else:
            out.append(("int", path, a, b))
        return
    if type(a) is not type(b) and not (a is None or b is None) or (a is None) != (b is None):
        out.append(("type", path, a, b))
    elif a != b:
        out.append(("raw", path, a, b))


def emv2_short(v):
    s = json.dumps(v, default=str)
    return s if len(s) <= 120 else s[:117] + "..."


def emv2_write(rec):
    cfg = EMV2["cfg"] or {"log_max": 64 << 20, "log_keep": 5}
    where = str(getattr(LOG, "name", ""))   # the bridge's --logs directory once main() opened bridge.log; '<stderr>' before
    fn = os.path.join(os.path.dirname(os.path.abspath(where)) if where and not where.startswith("<") else os.path.dirname(os.path.abspath(__file__)),
                      "emv2-shadow.jsonl")
    line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
    with EMV2["lock"]:
        try:
            if os.path.exists(fn) and os.path.getsize(fn) + len(line) > cfg["log_max"]:
                for i in range(cfg["log_keep"] - 1, 0, -1):
                    if os.path.exists("%s.%d" % (fn, i)):
                        os.replace("%s.%d" % (fn, i), "%s.%d" % (fn, i + 1))
                os.replace(fn, fn + ".1")
            with open(fn, "a") as f:
                f.write(line)
        except OSError as e:
            EMV2["stats"]["log_error"] += 1
            if EMV2["stats"]["log_error"] == 1:
                log("emv2 shadow log %r" % e)


def emv2_ask(cfg, target):
    t0 = time.perf_counter()
    c = http.client.HTTPConnection(*cfg["addr"], timeout=cfg["timeout"])
    try:
        c.request("GET", target, headers={"Accept": "application/json", "X-EMV2-Shadow": "1"})
        r = c.getresponse()
        body = r.read(20_000_000)
        return r.status, r.getheader("X-EMV2-Build", ""), body, (time.perf_counter() - t0) * 1000, r.getheader("X-EMV2-As-Of", "")
    finally:
        c.close()


def emv2_asof(new, header, chain=None):
    """(block, time) of the V2 answer: its `as_of` field (int block, {block, time}, or {<chain>: {block, time}} on answers that
    span chains), else the X-EMV2-As-Of header."""
    a = new.get("as_of") if isinstance(new, dict) else None
    if isinstance(a, dict) and "block" not in a and isinstance(a.get(chain), dict):
        a = a[chain]
    if isinstance(a, dict):
        b, t = a.get("block"), a.get("time")
    elif emv2_num(a):
        b, t = a, None
    else:
        parts = (header or "").split(":")
        b, t = (parts + [None, None])[:2]
    try:
        return (int(b) if b not in (None, "") else None), (int(t) if t not in (None, "") else None)
    except (TypeError, ValueError):
        return None, None


def emv2_after(item, blk, t):
    """True when a dict row records activity after the V2 point (a block or a time past it)."""
    if not isinstance(item, dict):
        return False
    for k in EMV2_BLOCK_KEYS:
        if blk is not None and emv2_num(item.get(k)) and item[k] > blk:
            return True
    for k in EMV2_TIME_KEYS:
        if t is not None and emv2_num(item.get(k)) and item[k] > t:
            return True
    return False


def emv2_trim(obj, blk, t, n):
    """Copy of the old answer without list rows after the V2 point; n[0] counts the rows left out."""
    if isinstance(obj, dict):
        return {k: emv2_trim(v, blk, t, n) for k, v in obj.items()}
    if isinstance(obj, list):
        keep = []
        for x in obj:
            if emv2_after(x, blk, t):
                n[0] += 1
            else:
                keep.append(emv2_trim(x, blk, t, n))
        return keep
    return obj


def emv2_old_at(job, blk, t):
    """The current upstream's answer at the V2 point for routes that take one (holders-at block, trace-graph to), or None."""
    spec = EMV2_ALIGN.get(job["path"])
    if not spec or job.get("qs") is None:
        return None
    param, own = spec
    if any(job["qs"].get(k) for k in own):
        return None   # the request already named its own point in time: the old answer is at that point
    val = blk if param == "block" else t
    if val is None:
        return None
    try:
        data, _ = invscreen_route(job["path"], dict(job["qs"], **{param: str(val)}))
        return data
    except (HTTPError, OSError, ValueError):
        return None


def emv2_covered(cfg):
    """Chains emv2-serve has loaded (its /health `chains`), refreshed every 60 s; None when unknown."""
    now = time.time()
    if now - EMV2.get("cov_at", 0) > 60:
        cov = None
        try:
            code, _, body, _, _ = emv2_ask(cfg, "/health")
            d = json.loads(body) if code == 200 else {}
            cov = set(d.get("chains") or {}) if isinstance(d.get("chains"), dict) else None
        except (OSError, http.client.HTTPException, ValueError):
            cov = None
        EMV2.update(cov=cov, cov_at=now)
    return EMV2.get("cov")


def emv2_only_covered(old, covered, n):
    """address-chains limited to the chains V2 has loaded (rows `chains[].chain`); n[0] counts rows left out."""
    if not isinstance(old, dict) or not isinstance(old.get("chains"), list) or covered is None:
        return old
    keep = [c for c in old["chains"] if not isinstance(c, dict) or c.get("chain") in covered]
    n[0] += len(old["chains"]) - len(keep)
    return dict(old, chains=keep)


def emv2_drop_xc(obj, chain, n):
    """trace-graph without bridge legs (links kind=bridge, nodes on other chains): V2 phase 1 has no xc legs."""
    if not isinstance(obj, dict) or not isinstance(obj.get("links"), list):
        return obj
    links = [l for l in obj["links"] if not (isinstance(l, dict) and l.get("kind") == "bridge")]
    nodes = [x for x in obj.get("nodes") or [] if not (isinstance(x, dict) and x.get("chain") not in (None, chain))]
    n[0] += len(obj["links"]) - len(links) + len(obj.get("nodes") or []) - len(nodes)
    return dict(obj, links=links, nodes=nodes) if n[0] else obj


def emv2_compare(job):
    cfg = EMV2["cfg"]
    if not cfg:
        return
    rec = {"ts": round(time.time(), 3), "route": job["route"], "chain": job["chain"], "target": job["target"][:400],
           "old_code": job["code"], "old_ms": round(job["ms"], 2)}
    try:
        code, build, body, ms, asof_h = emv2_ask(cfg, job["target"])
        rec.update(new_code=code, new_ms=round(ms, 2), build=build[:80])
    except (OSError, http.client.HTTPException) as e:
        rec.update(new_code=0, verdict="v2_down", error=type(e).__name__)
        EMV2["stats"]["v2_down"] += 1
        return emv2_write(rec)
    if code == 501:
        rec["verdict"] = "unsupported"
    elif code == 504:
        rec["verdict"] = "v2_budget"
    elif code == 200 and job["code"] in (400, 404):
        rec["verdict"] = "v2_only"      # the current services do not cover this chain or market; V2 does (not a regression)
    elif code == 200 and job["code"] in (500, 502, 503, 504):
        rec["verdict"] = "old_failed"   # the current upstream failed this time; nothing to compare
    elif job["code"] == 200 and isinstance(job["obj"], dict) and job["obj"].get("available") is False:
        rec["verdict"] = "old_unavailable"   # the current service answers "Preparing" for this chain or route; nothing to compare
    elif code != job["code"]:
        rec.update(verdict="critical", critical=1, diffs=[["status", "", job["code"], code]])
    elif code != 200:
        rec["verdict"] = "same_status"
    else:
        try:
            new = json.loads(body)
        except ValueError:
            rec.update(verdict="critical", critical=1, diffs=[["type", "", "json", "not json"]])
            return emv2_write(rec)
        blk, t = emv2_asof(new, asof_h, job["chain"])
        old, align, after = job["obj"], "none", [0]
        if blk is not None or t is not None:
            rec["as_of"] = [blk, t]
            at = emv2_old_at(job, blk, t) if cfg.get("align_old", True) else None
            if at is not None:
                old, align = at, "param"
            else:
                old, align = emv2_trim(job["obj"], blk, t, after), "trim"
        xc, outside = [0], [0]
        if job["route"] == "trace-graph":
            old = emv2_drop_xc(old, job["chain"], xc)
        if job["chain"] == "*":
            old = emv2_only_covered(old, emv2_covered(cfg), outside)
            rec["outside_v2"] = outside[0]
            if outside[0] and not (old or {}).get("chains") and not (new.get("chains") if isinstance(new, dict) else None):
                rec["verdict"] = "outside_v2"   # every chain the current answer lists is one V2 has not loaded yet
                EMV2["stats"]["outside_v2"] += 1
                return emv2_write(rec)
        if job["route"] == "path" and isinstance(new, dict) and isinstance(old, dict) and old.get("found"):
            # V2 does not cross hubs (degree > 20,000: routers, exchanges, pools). When the old path runs through one that V2 names,
            # the difference is semantic: V2 answers "only via a hub" (not found) or a longer path that avoids it.
            hubs = {b.get("address") for b in new.get("blockedByHub") or [] if isinstance(b, dict)}
            mids = {n.get("address") for n in (old.get("nodes") or [])[1:-1] if isinstance(n, dict)}
            worse = (not new.get("found")) or (isinstance(new.get("hops"), int) and isinstance(old.get("hops"), int) and new["hops"] > old["hops"])
            if worse and hubs & mids:
                rec.update(verdict="via_hub", critical=0, hub=sorted(hubs & mids)[:3], old_hops=old.get("hops"), new_hops=new.get("hops"))
                EMV2["stats"]["via_hub"] += 1
                return emv2_write(rec)
        if job["route"] == "path" and isinstance(new, dict) and isinstance(old, dict):
            # shortest paths are not unique and the two engines may pick different middle nodes: compare what is determined,
            # found and hops, and (below) that V2's own path is a chain of edges from `from` to `to`
            ok = isinstance(new.get("nodes"), list) and isinstance(new.get("edges"), list)
            if new.get("found") and ok:
                ns = [n.get("id") for n in new["nodes"] if isinstance(n, dict)]
                es = [(e.get("source"), e.get("target")) for e in new["edges"] if isinstance(e, dict)]
                chain_ok = len(ns) == len(es) + 1 and all(es[i] == (ns[i], ns[i + 1]) for i in range(len(es))) and \
                    ns[0].endswith(":" + str(new.get("from"))) and ns[-1].endswith(":" + str(new.get("to")))
                if not chain_ok:
                    rec.update(verdict="critical", critical=1, diffs=[["value", "path", "chain of edges", "broken"]])
                    EMV2["stats"]["critical"] += 1
                    return emv2_write(rec)
            if new.get("found") and old.get("found") and isinstance(new.get("hops"), int) and isinstance(old.get("hops"), int) and new["hops"] < old["hops"]:
                # V2 found a shorter path and its edge chain was just checked: not a regression (the old engine's typed search skipped it)
                rec.update(verdict="match", critical=0, v2_shorter=1, old_hops=old["hops"], new_hops=new["hops"])
                EMV2["stats"]["match"] += 1
                return emv2_write(rec)
            old = {"found": bool(old.get("found")), "hops": old.get("hops")}
            new = {"found": bool(new.get("found")), "hops": new.get("hops")}
        out, seen = [], [0]
        emv2_diff(old, new, "", out, seen)
        if outside[0]:
            out = [d for d in out if d[1] != "available"]   # V2's own flag reflects only the chains it has loaded
        skip, gaps = EMV2_SKIP.get(job["route"]), EMV2_GAPS.get(job["route"])
        if skip:
            out = [d for d in out if not skip.search(d[1])]
        if gaps:
            out = [("known_gap",) + tuple(d[1:]) if d[3] is None and gaps.search(d[1]) else d for d in out]
        crit = [d for d in out if d[0] in EMV2_CRITICAL]
        skew = bool(crit) and align == "trim" and after[0] > 0
        verdict = "time_skew" if skew else "critical" if crit else "xc_missing" if xc[0] else "match"
        rec.update(leaves=seen[0], critical=0 if skew else len(crit), time_skew=len(crit) if skew else 0, extra=len(out) - len(crit),
                   known_gap=sum(1 for d in out if d[0] == "known_gap"), xc_missing=xc[0], align=align, after_as_of=after[0], verdict=verdict,
                   diffs=[[k, p, emv2_short(x), emv2_short(y)] for k, p, x, y in (crit or out)[:20]])
    EMV2["stats"][rec["verdict"]] += 1
    emv2_write(rec)


def emv2_worker():
    while True:
        job = EMV2["q"].get()
        try:
            emv2_compare(job)
        except Exception as e:   # a comparison bug must never reach the bridge
            EMV2["stats"]["compare_error"] += 1
            emv2_write({"ts": round(time.time(), 3), "route": job.get("route"), "chain": job.get("chain"), "verdict": "compare_error",
                        "error": repr(e)[:200]})
        finally:
            EMV2["q"].task_done()


def emv2_submit(cfg, job):
    now = time.time()
    with EMV2["lock"]:
        if EMV2["q"] is None:
            EMV2["q"] = _emv2_queue.Queue(maxsize=cfg["queue"])
            threading.Thread(target=emv2_worker, name="emv2-shadow", daemon=True).start()
        key = (job["route"], job["chain"])
        if cfg["gap"] and now - EMV2["last"].get(key, 0) < cfg["gap"]:
            EMV2["stats"]["skipped_gap"] += 1
            return
        EMV2["last"][key] = now
        stats_due = now - EMV2["stats_at"] > 300
        if stats_due:
            snap = dict(EMV2["stats"], queue=EMV2["q"].qsize())
            EMV2["stats"].clear()
            EMV2["stats_at"] = now
    if stats_due:
        emv2_write({"ts": round(now, 3), "kind": "stats", **snap})
    try:
        EMV2["q"].put_nowait(job)
    except _emv2_queue.Full:
        EMV2["stats"]["dropped"] += 1


def emv2_new(handler, cfg, route, chain, target):
    """Answer from emv2-serve; False (nothing sent) on any V2 failure so the caller falls back to the old path."""
    try:
        code, build, body, ms, _ = emv2_ask(cfg, target)
        obj = json.loads(body) if code == 200 else None
    except (OSError, http.client.HTTPException, ValueError):
        code, obj, ms, build = 0, None, 0.0, ""
    if code != 200 or not isinstance(obj, (dict, list)):
        EMV2["stats"]["new_fallback"] += 1
        emv2_write({"ts": round(time.time(), 3), "route": route, "chain": chain, "target": target[:400], "verdict": "new_fallback",
                    "new_code": code})
        return False
    handler.send_json(200, obj, cache="public, max-age=30", extra={"X-EMV2-Build": build[:80]} if build else None)
    EMV2["stats"]["new_served"] += 1
    return True


# EMV2-only routes: no current service answers them, so "old" (and no config) keeps the bridge's 404; mode "new" in
# emv2-bridge.json (routes: {"accumulators": "new"}) serves them from emv2-serve. tools/accumulators is the assistant card built
# from the same answer (design/EM-ENGINE-V2.md; FINAL-PRODUCTION-PLAN.md A.5).
EMV2_ONLY = {"/api/public/em/accumulators": "accumulators", "/api/public/em/tools/accumulators": "accumulators",
             "/api/public/em/graph": "graph", "/api/public/em/featured": "featured",
             "/api/public/em/tokens": "tokens", "/api/public/em/capabilities": "capabilities"}
EMV2_ONLY_PARAM = {"market": re.compile(r"[a-z][a-z0-9]{1,15}:0x[0-9a-f]{40}"), "window": re.compile(r"1h|24h|7d|30d|90d|all"),
                   "from": re.compile(r"[0-9]{9,13}"), "to": re.compile(r"[0-9]{9,13}"), "limit": re.compile(r"[1-9][0-9]?"),
                   "address": re.compile(r"0x[0-9a-f]{40}"), "entity": re.compile(r"[^\x00-\x1f]{1,120}"), "chain": re.compile(r"[a-z][a-z0-9]{1,15}"),
                   "hops": re.compile(r"[12]"), "min_usd": re.compile(r"[0-9]{1,15}(\.[0-9]{1,6})?"),
                   "symbol": re.compile(r"[^\x00-\x1f&#?/\\]{1,40}")}
EMV2_FEATURED = os.environ.get("EM_EMV2_FEATURED", "/srv/skew/emv2/featured")
EMV2_XC_GRADE = {"exact": "proven", "structural": "matched", "heuristic": "inferred"}


def emv2_featured(handler, qs):
    """Static home snapshot (tools/emv2_featured.py, daily): <dir>/<chain>.json or index.json. No upstream call: zero wait."""
    chain = (qs.get("chain") or "").lower()
    if chain and not EMV2_ONLY_PARAM["chain"].fullmatch(chain):
        return handler.send_json(400, {"detail": "chain is a registry id."})
    path = os.path.join(EMV2_FEATURED, (chain or "index") + ".json")
    try:
        st = os.stat(path)
        cached = EMV2["last"].get(path)
        if not cached or cached[0] != st.st_mtime_ns:
            with open(path) as f:
                cached = (st.st_mtime_ns, json.load(f))
            EMV2["last"][path] = cached
    except (OSError, ValueError):
        return handler.send_json(404, {"detail": "The featured snapshot for this chain is being prepared."})
    return handler.send_json(200, cached[1], cache="public, max-age=300")


EMV2_XC_WAIT_S = float(os.environ.get("EM_EMV2_XC_WAIT_S", "0.12"))
EMV2_XC_PENDING = {}
EMV2_XC_LOCK = threading.Lock()


def emv2_xc_get(up):
    """em-xc wallet-xc through the 300 s cache, waiting at most EMV2_XC_WAIT_S: a first ask that em-xc answers slowly (it took
    200 ms - 1.1 s, 2026-10-21) returns the graph without bridges and the fetch finishes in the background for the next ask.
    Returns (answer or None, pending)."""
    key = "emv2xc:" + up
    with EMV2_XC_LOCK:
        job = EMV2_XC_PENDING.get(key)
        if job is None:
            job = (threading.Event(), {})
            EMV2_XC_PENDING[key] = job

            def run(done=job[0], box=job[1]):
                try:
                    box["v"] = CACHE.get(key, 300, lambda: fetch_json(CFG["xc"], up, timeout=2))
                except Exception as e:   # noqa: BLE001 - reported to the caller
                    box["e"] = e
                finally:
                    done.set()
                    with EMV2_XC_LOCK:
                        EMV2_XC_PENDING.pop(key, None)
            threading.Thread(target=run, daemon=True).start()
    if not job[0].wait(EMV2_XC_WAIT_S):
        return None, True
    if "e" in job[1]:
        raise job[1]["e"]
    return job[1].get("v"), False


def emv2_xc_merge(obj, address):
    """Cross-chain bridges for the asked wallet from em-xc (matched legs) added to a graph/1 answer as kind "bridge" edges."""
    try:
        up = "/wallet-xc?" + urllib.parse.urlencode({"address": address, "limit": 20})
        xc, pending = emv2_xc_get(up)
    except Exception:   # the graph answer stands without bridges
        obj.setdefault("notes", []).append("Cross-chain bridges are temporarily unavailable.")
        return obj
    if pending:
        obj["xcPending"] = True
        obj.setdefault("notes", []).append("Cross-chain bridges are still loading for this wallet; ask again in a moment to include them.")
        return obj
    ids = {n.get("id") for n in obj.get("nodes") or []}
    names = {}
    try:
        names = {int(c.get("chainId")): c.get("id") for c in chains_status().get("chains") or [] if c.get("chainId")}
    except Exception:
        pass
    added = 0
    for leg in ((xc or {}).get("bridges") or {}).get("legs") or []:
        sc, dc = names.get(leg.get("src_chain")), names.get(leg.get("dst_chain"))
        sw, dw = (leg.get("src_wallet") or "").lower(), (leg.get("dst_wallet") or "").lower()
        if not (sc and dc and EMV2_ONLY_PARAM["address"].fullmatch(sw) and EMV2_ONLY_PARAM["address"].fullmatch(dw)):
            continue
        a, b = sc + ":" + sw, dc + ":" + dw
        for nid, ch, ad in ((a, sc, sw), (b, dc, dw)):
            if nid not in ids:
                ids.add(nid)
                obj.setdefault("nodes", []).append({"id": nid, "address": ad, "chain": ch, "entity": None, "entityGrade": None, "category": "unknown",
                                                    "categoryName": "Unclassified", "kind": "wallet", "isContract": None, "isHub": False, "stop": False,
                                                    "hop": 1, "held": None, "markKey": None, "mark": None, "chainMark": None, "firstSeen": None,
                                                    "lastSeen": None, "via": "xc"})
        obj.setdefault("edges", []).append({"source": a, "target": b, "chain": sc + "->" + dc, "kind": "bridge", "bridge": leg.get("bridge"),
                                            "asset": {"symbol": None, "address": leg.get("src_asset"), "decimals": None}, "amountRaw": None,
                                            "amount": leg.get("amount") if isinstance(leg.get("amount"), (int, float)) else None,
                                            "usd": leg.get("usd") if isinstance(leg.get("usd"), (int, float)) else None,
                                            "usdReason": None if isinstance(leg.get("usd"), (int, float)) else "bridge leg without a USD value",
                                            "n": 1, "firstTime": leg.get("src_ts"), "lastTime": leg.get("dst_ts"), "grade": EMV2_XC_GRADE.get(leg.get("class"), "unknown"),
                                            "matchClass": leg.get("class"), "hop": 1})
        added += 1
    obj["xcLegs"] = added
    return obj


def emv2_card(a, qs):
    """tools/accumulators card: who bought (net) and who sold (net) a token over a window, numbers only."""
    def row(r):
        who = r.get("entity") or (r.get("category") if r.get("category") not in (None, "unknown") else None)
        return {"address": r.get("address"), "label": who, "kind": r.get("kind"), "grade": r.get("grade"), "side": r.get("side"),
                "delta": r.get("delta"), "deltaRaw": r.get("deltaRaw"), "balanceEnd": r.get("balanceEnd"), "pctOfSupply": r.get("pctOfSupply"),
                "isContract": r.get("isContract")}
    sym = a.get("symbol") or (a.get("market") or "").split(":")[-1][:10]
    w = a.get("window") or {}
    buyers, sellers = [row(r) for r in a.get("accumulators") or []], [row(r) for r in a.get("distributors") or []]
    span = qs.get("window") or "%sd" % max(1, round((int(w.get("to") or 0) - int(w.get("from") or 0)) / 86400))
    if a.get("available") is False:   # held (independent RPC check failed) or preparing: say why, never "no buyer"
        msg = "%s: %s" % (sym, a.get("reason") or "the balances of this token are being prepared.")
    elif buyers:
        b = buyers[0]
        msg = "%s: the largest net buyer over %s is %s%s, +%s (%s%% of supply)." % (
            sym, span, (b["address"] or "")[:6] + "…" + (b["address"] or "")[-4:], " (%s)" % b["label"] if b["label"] else "",
            "{:,.4g}".format(b["delta"] or 0), b["pctOfSupply"] if b["pctOfSupply"] is not None else "?")
    else:
        msg = "%s: no wallet's balance rose over %s." % (sym, span)
    return {"kind": "accumulators", "title": "Who is accumulating · " + sym, "market": a.get("market"), "symbol": a.get("symbol"),
            "network": (a.get("market") or "").split(":")[0], "window": w, "method": a.get("method"), "complete": a.get("complete"),
            "buyers": buyers, "sellers": sellers, "message": msg, "available": a.get("available", False), "reason": a.get("reason"),
            "held": bool(a.get("held")), "reasonKo": a.get("reasonKo"),
            "note": "Exact net balance change per address from the token's transfer history (Economic Machine V2); pools, routers and "
                    "exchanges are included and labelled by kind."}


def emv2_only(handler, cfg, path, qs):
    if path == "/api/public/em/featured":
        return emv2_featured(handler, qs)
    q = {}
    for k, v in qs.items():
        rx = EMV2_ONLY_PARAM.get(k)
        if rx is None or not rx.fullmatch((v or "").lower()):
            return handler.send_json(400, {"detail": "Invalid parameter: " + k})
        q[k] = v if k == "entity" else v.lower()
    if path == "/api/public/em/graph":
        if ("address" in q) == ("entity" in q):
            return handler.send_json(400, {"detail": "give exactly one of address or entity."})
        # window=24h/7d/30d: emv2-serve answers exact hop-1 token flows inside the window from the ledger (totalsScope "window");
        # on a chain without a ledger it answers 400 reason window_totals_preparing itself, passed on below
        target = "/api/public/em/graph?" + urllib.parse.urlencode(sorted(q.items()))
    elif path == "/api/public/em/capabilities":
        if q:
            return handler.send_json(400, {"detail": "capabilities takes no parameters."})
        target = "/api/public/em/capabilities"
    elif path == "/api/public/em/tokens":
        if "symbol" not in q or set(q) - {"symbol", "chain", "limit"}:
            return handler.send_json(400, {"detail": "give symbol (and optionally chain, limit)."})
        q["symbol"] = qs.get("symbol")   # symbols are matched case-insensitively by emv2-serve; keep the asked spelling
        target = "/api/public/em/tokens?" + urllib.parse.urlencode(sorted(q.items()))
    elif "market" not in q:
        return handler.send_json(400, {"detail": "market must be <chain>:<0x token address>."})
    else:
        target = "/api/public/em/accumulators?" + urllib.parse.urlencode(sorted(q.items()))
    try:
        code, build, body, ms, _ = emv2_ask(cfg, target)
        obj = json.loads(body) if code in (200, 400, 501) else None
    except (OSError, http.client.HTTPException, ValueError):
        code, obj, build = 0, None, ""
    EMV2["stats"]["only_" + str(code)] += 1
    # The London reader preserves the original qualified graph generation.
    # Where that generation is pending it can return clearly scoped retained
    # current observations. Exact historical points and entity queries never
    # fall through to a different time/subject.
    if path == "/api/public/em/graph" and code in (400, 501) and q.get("address") and q.get("chain") in {"arbitrum","robinhood","arc","ethereum","base","optimism","linea","polygon","unichain"} and set(q) <= {"chain","address","window","hops","limit","min_usd"}:
        admitted = code == 501 or (isinstance(obj, dict) and obj.get("reason") == "window_totals_preparing")
        if admitted:
            try:
                args = {k:v for k,v in q.items() if k in {"chain","address","window","hops","limit","min_usd"}}
                london = fetch_json(("127.0.0.1", 18770), "/graph?" + urllib.parse.urlencode(sorted(args.items())), timeout=5)
                if not isinstance(london, dict) or london.get("schema") != "graph/1" or london.get("query", {}).get("chain") != q["chain"] or london.get("query", {}).get("address") != q["address"]:
                    raise ValueError("London graph identity mismatch")
                return handler.send_json(200, london, cache="no-store", extra={"X-Ticking-Graph-Reader":"London"})
            except (OSError, http.client.HTTPException, HTTPError, ValueError):
                return handler.send_json(503, {"detail":"The London graph source is temporarily unavailable; no other chain or historical point was substituted."})
    if code == 200 and isinstance(obj, dict):
        if path == "/api/public/em/graph":
            out = emv2_xc_merge(obj, q["address"]) if "address" in q and q.get("window", "all") == "all" and not ({"from", "to"} & set(q)) else obj
            if q.get("window", "all") != "all" or ({"from", "to"} & set(q)):
                out.setdefault("notes", []).append("All-time cross-chain bridge rows were not added to this bounded time selection.")
        else:
            out = emv2_card(obj, q) if path.endswith("/tools/accumulators") else obj
        cache = "no-store" if isinstance(out, dict) and out.get("xcPending") else "public, max-age=60"   # a bridge-less first answer is not kept
        return handler.send_json(200, out, cache=cache, extra={"X-EMV2-Build": build[:80]} if build else None)
    if code in (400, 501) and isinstance(obj, dict):
        out = {"detail": obj.get("detail") or obj.get("error") or "unsupported"}
        if obj.get("reason"):
            out["reason"] = obj["reason"]
        return handler.send_json(code, out)
    return handler.send_json(503, {"detail": "Economic Machine data is unavailable right now."})


def request_limit(handler, ip, bucket):
    # A failed new-engine read may fall back to the legacy handler in this same
    # request. Debit once, and never retain the exemption for another request.
    if getattr(handler, "_emv2_debit", None) != (ip, bucket):
        LIMIT.take(ip, bucket)


def emv2_limited(handler, path, fn):
    ip = client_ip(handler)
    REQ.ip = ip
    bucket = "tools" if path.startswith("/api/public/em/tools/") or path == "/api/public/em/trace" else "data"
    LIMIT.take(ip, bucket)
    handler._emv2_debit = (ip, bucket)
    try:
        return fn()
    finally:
        del handler._emv2_debit


_emv2_api_old = Handler.api


def _emv2_api(self, path, query, qs):
    if self.command == "GET" and path in EMV2_ONLY:
        cfg = emv2_cfg()
        if cfg and emv2_mode(cfg, EMV2_ONLY[path], "*") == "new":
            return emv2_limited(self, path, lambda: emv2_only(self, cfg, path, qs))
        return _emv2_api_old(self, path, query, qs)   # unchanged: the bridge's own 404
    hit = emv2_route(path, qs) if self.command == "GET" else None
    cfg = emv2_cfg() if hit else None
    mode = emv2_mode(cfg, *hit) if cfg else "old"
    if mode == "old":
        return _emv2_api_old(self, path, query, qs)
    route, chain = hit
    target = path + ("?" + query if query else "")
    if mode == "new":
        def new_or_fallback():
            if emv2_new(self, cfg, route, chain, target):
                return None
            return _emv2_api_old(self, path, query, qs)
        return emv2_limited(self, path, new_or_fallback)
    if mode == "shadow" and cfg["sample"] < 1.0 and _emv2_random.random() >= cfg["sample"]:
        return _emv2_api_old(self, path, query, qs)
    cap, send = {}, self.send_json

    def capture(code, obj, *a, **k):
        if "code" not in cap:
            cap.update(code=code, obj=obj)
        return send(code, obj, *a, **k)

    self.send_json = capture
    t0 = time.perf_counter()
    try:
        return _emv2_api_old(self, path, query, qs)
    except HTTPError as e:
        cap.setdefault("code", e.code)
        cap.setdefault("obj", None)
        raise
    finally:
        del self.send_json
        if mode == "shadow" and "code" in cap:
            locked = isinstance(cap["obj"], dict) and "locked" in cap["obj"]
            if locked:
                EMV2["stats"]["skipped_gated"] += 1
            else:
                emv2_submit(cfg, {"route": route, "chain": chain, "target": target, "path": path, "qs": dict(qs), "code": cap["code"],
                                  "obj": cap["obj"], "ms": (time.perf_counter() - t0) * 1000})


Handler.api = _emv2_api


from agent_wallet_reads import install as _install_agent_wallet_reads
_install_agent_wallet_reads(globals())

if __name__ == "__main__":
    sys.exit(main())
