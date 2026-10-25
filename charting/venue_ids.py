"""Venue identities for the money-flow map: who a 'univ3_fork:0x…' / 'aave_v3_fork:0x…' registry key really is.

Sources, in order (each name records its source and evidence):
  docs      factories listed in a protocol's official deployment docs (URL kept with the entry)
  onchain   the venue's own on-chain metadata: position-manager NFT name (V3 / Algebra), LP token name (V2 pairs),
            aToken / cToken name (lenders). Used only when it names a distinct brand; generic names ('Uniswap V3 Positions NFT-V1',
            'Algebra Positions NFT-V2', 'Uniswap V2') and big-brand names on a factory that is not the brand's own deployment
            are treated as unidentified.
  evidence  our own graph and chain evidence for a venue the steps above leave unnamed (evidence_pass, both chains together):
            deployer  the factory's creator (graph-loop 'by') also created a venue already named by docs/on-chain on either chain
            address   the same contract address is a named venue on the other chain (CREATE2 / same deployer nonce)
            code      the factory runtime code is identical or >= 99% byte-identical to a named venue's factory whose code is not the
                      canonical Uniswap / Algebra code (an unmodified fork proves nothing)
            uri       the position manager's tokenURI / contractURI JSON names a brand
            A name needs at least one of these; otherwise the venue keeps its descriptive fallback name. No third-party name lists.
  fallback  a stable descriptive name with the factory / pool address, e.g. 'V3 DEX · 0x1f7d…' ('Unidentified V3-style DEX' on hover).
Writes data/<chain>/analytics/venue_ids.json; flowmap.build reads it. usage: venue_ids.py <chain> [--force]
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

ROOT = "/srv/skew/em-charts"
DIMS = "/srv/skew/em-galaxy/live/dims_{chain}.sqlite"
RPC = {"robinhood": "https://rpc.mainnet.chain.robinhood.com", "arbitrum": "https://arb1.arbitrum.io/rpc", "arc": "https://arc.drpc.org"}
UNISWAP_DOCS = "https://developers.uniswap.org/docs/protocols/v3/deployments/v3-robinhood-chain-deployments"
DOCS = {   # factory prefix -> (name, source)
    "robinhood": {"0x1f7d7550": ("Uniswap V3", UNISWAP_DOCS)},
    "arbitrum": {},
    "arc": {},
}
# brands whose own deployments are known: a fork claiming one of these names on another factory stays unidentified
OWN_FACTORIES = {"pancake": {"0x0bfbcf9f", "0x02a84c1b"}, "sushiswap": {"0x1af415a1", "0xe51960f1"}, "uniswap": {"0x1f7d7550", "0x1f98431c"},
                 "ramses": {"0xaa2cd747"}}
GENERIC = re.compile(r"^(uniswap v[234]( positions?.*| lp.*)?|algebra positions.*|v4 positions.*|positions nft.*|lp token.*|uniswap v2)$", re.I)
KIND = {"univ3_fork": ("V3 DEX", "Unidentified V3-style DEX"), "univ2_fork": ("V2 DEX", "Unidentified V2-style DEX"),
        "univ4_fork": ("V4 DEX", "Unidentified Uniswap V4-style DEX"), "algebra_fork": ("Algebra DEX", "Unidentified Algebra-style DEX"),
        "aave_v3_fork": ("Aave-style lender", "Unidentified Aave V3-style lender"), "aave_v2_fork": ("Aave V2-style lender", "Unidentified Aave V2-style lender"),
        "morpho_blue_fork": ("Morpho-style lender", "Unidentified Morpho Blue-style lender"),
        "compound_v2_fork": ("Compound-style lender", "Unidentified Compound V2-style lender")}
H = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 em-flowmap venue-ids"}
GLSERVE = {"robinhood": (8710, 8712), "arbitrum": (8711, 8713), "arc": (8714, 8716)}
OTHER = {"robinhood": "arbitrum", "arbitrum": "robinhood", "arc": "arbitrum"}


def call(chain, to, data):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}).encode()
    for _ in range(2):
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(RPC[chain], data=body, headers=H), timeout=15))
            return r.get("result")
        except Exception:
            time.sleep(1)
    return None


def text(h):
    if not h or len(h) < 130:
        return None
    b = bytes.fromhex(h[2:])
    try:
        off = int.from_bytes(b[:32], "big")
        n = int.from_bytes(b[off:off + 32], "big")
        s = b[off + 32:off + 32 + n].decode("utf8", "replace").strip()
        return s or None
    except Exception:
        return None


def addr(h):
    return "0x" + h[-40:] if h and len(h) >= 66 and int(h, 16) else None


def brand(name, factory, kind):
    """A distinct brand from an on-chain name, or None when it is generic or claims a brand on someone else's factory."""
    if not name:
        return None
    n = re.sub(r"\s+", " ", name).strip()
    n = re.sub(r"\s+(volatile|stable)\b.*$", "", n, flags=re.I)           # solidly-style pair names
    n = re.sub(r"\s+(v\d+\s+)?(liquidity|position)s?\s*#\d+.*$", "", n, flags=re.I)   # 'Sinjoh V3 liquidity #1'
    if GENERIC.match(n):
        return None
    b = re.sub(r"\b(v\d)\s*(positions?|position nft|non fungible position|lp|nft).*$", r"\1", n, flags=re.I)
    b = re.sub(r"\s*(cl|conc(entrated)?|concentrated liquidity|slipstream|clmm)?\s*(positions?|position|non fungible position|conc liq|liquidity)\b.*$", "",
               b, flags=re.I).strip()
    b = re.sub(r"\s*(lps?|lp token|liquidity provider token|interest bearing.*)$", "", b, flags=re.I).strip()
    b = re.sub(r"\s*:.*$", "", b)                        # 'Impossible Swap LPs: IDIA/WETH' -> 'Impossible Swap'
    b = re.sub(r"\s*(lps?|lp token|erc20)$", "", b, flags=re.I).strip()
    if not b or len(b) > 32 or GENERIC.match(b) or re.search(r"[-/]|\d{2,}|_", b) or (b.isupper() and len(b) > 12):
        return None                                        # pair names ('DAI-NYAN'), market names, IMPLEMENTATION_NAMES
    if b.lower() == "pancake":
        b = "PancakeSwap V2" if kind == "univ2_fork" else "PancakeSwap"
    low = b.lower()
    for key, own in OWN_FACTORIES.items():
        if key in low and factory and factory[:10] not in own:
            return None
    return b.replace("Pancake V3", "PancakeSwap V3")


def resolve(chain, keys):
    d = sqlite3.connect(f"file:{DIMS.format(chain=chain)}?mode=ro", uri=True)
    npm_by_factory = {}
    for (a,) in d.execute("SELECT address FROM npm").fetchall():
        f = addr(call(chain, a, "0xc45a0155"))
        nm = text(call(chain, a, "0x06fdde03"))
        if f and nm:
            npm_by_factory.setdefault(f[:10], []).append((a, nm))
    out = {}
    for key in keys:
        base, _, suf = key.partition(":")
        if base not in KIND or not suf:
            continue
        short_name, hover = KIND[base]
        rec = {"key": key, "kind": base, "source": "fallback"}
        factory = None
        if base in ("univ3_fork", "univ2_fork", "algebra_fork"):
            p = d.execute("SELECT pool FROM pools WHERE protocol = ? LIMIT 1", (key,)).fetchone()
            factory = addr(call(chain, p[0], "0xc45a0155")) if p else None
            rec["factory"] = factory
            doc = DOCS.get(chain, {}).get((factory or suf)[:10])
            if doc:
                rec.update(name=doc[0], source="docs", evidence=doc[1])
            elif base == "univ2_fork" and p:
                nm = text(call(chain, p[0], "0x06fdde03"))
                b = brand(nm, factory, base)
                rec["evidence"] = f"pair {p[0]} name() = {nm!r}"
                if b:
                    rec.update(name=b, source="onchain")
            else:
                for npm, nm in npm_by_factory.get((factory or suf)[:10], []):
                    b = brand(nm, factory, base)
                    rec["evidence"] = f"position manager {npm} name() = {nm!r}"
                    if b:
                        rec.update(name=b, source="onchain")
                        break
        elif base in ("aave_v3_fork", "aave_v2_fork"):
            pool = d.execute("SELECT address FROM contracts WHERE protocol = ? AND role = 'pool' LIMIT 1", (key,)).fetchone()
            rec["pool"] = pool[0] if pool else None
            tok = None
            if pool:
                try:
                    import duckdb
                    q = duckdb.connect().execute(f"SELECT token FROM '{ROOT}/data/{chain}/analytics/dims/aave_tokens.parquet' WHERE pool = ? AND kind = 'atoken' LIMIT 1",
                                                 [pool[0]]).fetchone()
                    tok = q[0] if q else None
                except Exception:
                    tok = None
            if tok:
                nm = text(call(chain, tok, "0x06fdde03"))
                rec["evidence"] = f"aToken {tok} name() = {nm!r}"
                b = brand(re.sub(r"^(Aave\s+)?", "", nm or "").split(" ")[0] if nm and not nm.lower().startswith("aave") else None, None, base)
                if b:
                    rec.update(name=b, source="onchain")
        elif base == "compound_v2_fork":
            try:
                import duckdb
                ct = duckdb.connect().execute(f"SELECT ctoken FROM '{ROOT}/data/{chain}/analytics/dims/ctokens.parquet' WHERE protocol = ? LIMIT 1",
                                              [key]).fetchone()
            except Exception:
                ct = None
            if ct:
                nm = text(call(chain, ct[0], "0x06fdde03"))
                rec["evidence"] = f"market {ct[0]} name() = {nm!r}"
                b = brand((nm or "").split(" ")[0] if nm else None, None, base)
                if b and b.lower() not in ("compound", "c"):
                    rec.update(name=b, source="onchain")
        if "name" not in rec:
            ref = (factory or rec.get("pool") or suf)
            rec.update(name=f"{short_name} · {ref[:6]}…", hover=f"{short_name} (unidentified) · {'factory' if factory else 'contract'} {ref}")
        out[key] = rec
    return out


def main():
    chain = sys.argv[1]
    dst = f"{ROOT}/data/{chain}/analytics/venue_ids.json"
    old = json.load(open(dst)) if os.path.exists(dst) and "--force" not in sys.argv else {}
    d = sqlite3.connect(f"file:{DIMS.format(chain=chain)}?mode=ro", uri=True)
    keys = {r[0] for r in d.execute("SELECT DISTINCT protocol FROM pools WHERE protocol LIKE '%fork:%'").fetchall()}
    keys |= {r[0] for r in d.execute("SELECT DISTINCT protocol FROM contracts WHERE protocol LIKE '%fork:%'").fetchall()}
    res = sqlite3.connect(f"file:{ROOT}/data/{chain}/analytics/analytics.sqlite3?mode=ro", uri=True)
    try:   # only venues that actually carry flow (the flow map's venue table), plus anything already resolved
        live = set(json.loads(res.execute("SELECT json FROM flowmap WHERE window = 'venues'").fetchone()[0]))
        keys &= live
    except Exception:
        pass
    todo = sorted(k for k in keys if k not in old)
    new = resolve(chain, todo) if todo else {}
    out = {**old, **new}
    tmp = dst + ".tmp"
    json.dump(out, open(tmp, "w"), indent=1, sort_keys=True)
    os.replace(tmp, dst)
    srcs = {}
    for r in out.values():
        srcs[r["source"]] = srcs.get(r["source"], 0) + 1
    print(chain, "venue ids", len(out), srcs)
    for r in sorted(new.values(), key=lambda r: r["key"]):
        print(" ", r["key"], "->", r["name"], "|", r["source"], "|", r.get("evidence", ""))





# ------------------------------------------------------------------------------------------------ evidence pass (both chains)
NAMES_DB = "/srv/skew/em-galaxy/live/names.sqlite"
CANONICAL = {"robinhood": ["0x1f7d7550b1b028f7571e69a784071f0205fd2efa"],                       # Uniswap V3 (docs)
             "arbitrum": ["0x1f98431c8ad98523631ae4a59f267346ea31f984", "0xf1d7cc64fb4452f05c498126312ebe29f30fbcf9"], "arc": []}   # Uniswap V3 / V2
ANCHOR_ROLES = ("factory", "pool_manager", "position_manager", "router", "pool", "singleton", "vault_factory")
GENERIC_DEPLOYERS = {"0x4e59b44847b379578588920ca78fbf26c0b4956c", "0x914d7fec6aac8cd542e72bca78b30650d45643d7", "0x0000000000ffe8b47b3e2130213b802212439497",
                     "0xba5ed099633d3b313e4d5f7bdc1305d3c28ba5ed", "0x13b0d85ccb8bf860b6b79af3029fca081ae9bef2"}   # CREATE2 proxies, Safe singleton factory, CreateX
BIG_BRANDS = ("uniswap", "sushiswap", "pancake", "ramses", "camelot", "aave", "morpho", "curve", "balancer")


def rpc(chain, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for _ in range(2):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(RPC[chain], data=body, headers=H), timeout=20)).get("result")
        except Exception:
            time.sleep(1)
    return None


_code = {}


def code(chain, a):
    k = (chain, a)
    if k not in _code:
        c = rpc(chain, "eth_getCode", [a, "latest"])
        _code[k] = c if c and c != "0x" else None
    return _code[k]


def creator(chain, a):
    for port in GLSERVE[chain]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/tools/feature_pack?addr={a}", timeout=5) as r:
                by = (json.load(r).get("facts") or {}).get("by")
                if by:
                    return by.lower()
        except Exception:
            continue
    return None


def similar(a, b):
    """Share of identical bytes for two runtime codes of the same length (immutables differ in a few words); 1.0 = identical."""
    if not a or not b or len(a) != len(b):
        return 0.0
    x, y = bytes.fromhex(a[2:]), bytes.fromhex(b[2:])
    return sum(1 for i in range(len(x)) if x[i] == y[i]) / max(1, len(x))


def uri_brand(chain, npm):
    """Brand from a position manager's contractURI / tokenURI JSON (data:application/json;base64,…): 'Ramses V3 - 0.05% - …' -> 'Ramses'."""
    import base64
    uris = [text(call(chain, npm, "0xe8a3d485"))]
    sup = call(chain, npm, "0x18160ddd")
    if sup and int(sup, 16):
        tid = call(chain, npm, "0x4f6ccce7" + "0" * 64)
        if tid:
            uris.append(text(call(chain, npm, "0xc87b56dd" + tid[2:].rjust(64, "0"))))
    for u in uris:
        if not u:
            continue
        doc = None
        if u.startswith("data:application/json;base64,"):
            try:
                doc = json.loads(base64.b64decode(u.split(",", 1)[1]))
            except Exception:
                doc = None
        elif u.startswith("data:application/json"):
            try:
                doc = json.loads(urllib.request.unquote(u.split(",", 1)[1]))
            except Exception:
                doc = None
        if isinstance(doc, dict):
            name = str(doc.get("name") or "")
            head = re.sub(r"\s+(v\d+\s+)?(liquidity|position|positions|lp)\b.*$", "", name.split(" - ")[0].strip(), flags=re.I).strip()
            if head and not re.match(r"^(uniswap|algebra)\b", head, re.I) and len(head) <= 32 and not re.search(r"[/_]|\d{3}", head):
                return head, f"{npm} tokenURI name = {name[:80]!r}"
    return None


def root_brand(name):
    return re.sub(r"\s+(v\d+|cl|legacy|amm|dex|lp|finance|exchange)$", "", re.sub(r"\s+(v\d+|cl)(\s+v\d+)?$", "", name, flags=re.I), flags=re.I).strip() or name


def anchors_for(chain, ids):
    """Venues named by docs / on-chain names on this chain, plus the registry's own official deployments (names.sqlite *:factory etc.)."""
    out = []
    for k, r in ids.items():
        a = r.get("factory") or r.get("pool")
        if r.get("source") in ("docs", "onchain") and a:
            out.append({"chain": chain, "key": k, "name": r["name"], "address": a.lower(), "kind": r.get("kind")})
    try:
        n = sqlite3.connect(f"file:{NAMES_DB}?mode=ro", uri=True)
        for a, nm in n.execute("SELECT address, name FROM names WHERE chain = ? AND kind = 'label'", (chain,)).fetchall():
            proto, _, role = (nm or "").partition(":")
            if role in ANCHOR_ROLES and "fork" not in proto:
                out.append({"chain": chain, "key": proto, "name": _proto_name(proto), "address": a.lower(), "kind": "registry"})
    except sqlite3.Error:
        pass
    return out


def _proto_name(p):
    words = [w for w in re.split(r"[_\s]+", p) if w]
    return " ".join(w.upper() if re.fullmatch(r"v\d", w) else {"uniswap": "Uniswap", "sushiswap": "SushiSwap", "pancakeswap": "PancakeSwap"}.get(w, w.capitalize())
                    for w in words)


def evidence_pass():
    """Second pass over both chains: venues still unnamed (fallback) get a name only from deployer / same-address / code / URI evidence."""
    ids = {}
    for ch in ("robinhood", "arbitrum", "arc"):
        p = f"{ROOT}/data/{ch}/analytics/venue_ids.json"
        ids[ch] = json.load(open(p)) if os.path.exists(p) else {}
    anchors = [a for ch in ids for a in anchors_for(ch, ids[ch])]
    for ch in ids:   # position managers named on-chain are anchors too, keyed by their creator (Ramses' NPM has no factory())
        dn = sqlite3.connect(f"file:{DIMS.format(chain=ch)}?mode=ro", uri=True)
        for (npm,) in dn.execute("SELECT address FROM npm").fetchall():
            fac = addr(call(ch, npm, "0xc45a0155"))
            b = brand(text(call(ch, npm, "0x06fdde03")), fac, "univ3_fork")   # a big-brand NFT name only counts on that brand's own factory
            if b:
                anchors.append({"chain": ch, "key": "npm:" + npm, "name": b, "address": npm.lower(), "kind": "npm", "factory": (fac or "").lower()})
    generic = set()
    for ch, lst in CANONICAL.items():
        for a in lst:
            c = code(ch, a)
            if c:
                generic.add(c)
    for a in anchors:
        a["by"] = creator(a["chain"], a["address"])
        a["code"] = code(a["chain"], a["address"])
    changes = []
    for ch in ids:
        d = sqlite3.connect(f"file:{DIMS.format(chain=ch)}?mode=ro", uri=True)
        npm_by_factory = None
        for k, r in ids[ch].items():
            if r.get("source") not in ("fallback", "defillama", "evidence"):
                continue
            before = (r.get("name"), r.get("source"))
            base, _, suf = k.partition(":")
            short_name, hover = KIND.get(base, ("Venue", "Unidentified venue"))
            f = (r.get("factory") or r.get("pool") or "").lower()
            if not f and base == "morpho_blue_fork":
                row = d.execute("SELECT address FROM contracts WHERE protocol = ? LIMIT 1", (k,)).fetchone()
                f = row[0].lower() if row else ""
            ev, bname = [], None
            if f:
                by = creator(ch, f)
                if by and by not in GENERIC_DEPLOYERS:
                    same = [a for a in anchors if a.get("by") == by and a["address"] != f and a.get("factory") != f]   # never the venue's own contracts
                    if same:
                        a = same[0]
                        bname = root_brand(a["name"])
                        ev.append({"type": "deployer", "detail": f"factory {f} and {a['name']} {a['address']} ({a['chain']}) were both created by {by}"})
                other = OTHER[ch]
                twin = next((a for a in anchors if a["chain"] == other and a["address"] == f), None)
                if twin and code(other, f):
                    bname = bname or root_brand(twin["name"])
                    ev.append({"type": "address", "detail": f"{f} is {twin['name']} on {other}"})
                c = code(ch, f)
                if c and c not in generic:
                    best = max(((similar(c, a["code"]), a) for a in anchors if a.get("code") and a["address"] != f and a["code"] not in generic),
                               key=lambda x: x[0], default=(0, None))
                    if best[1] and best[0] >= 0.99:
                        bname = bname or root_brand(best[1]["name"])
                        ev.append({"type": "code", "detail": f"factory code {round(best[0] * 100, 2)}% identical to {best[1]['name']} {best[1]['address']} ({best[1]['chain']})"})
                if base in ("univ3_fork", "algebra_fork", "univ4_fork"):
                    if npm_by_factory is None:
                        npm_by_factory = {}
                        for (npm,) in d.execute("SELECT address FROM npm").fetchall():
                            fac = addr(call(ch, npm, "0xc45a0155"))
                            if fac:
                                npm_by_factory.setdefault(fac.lower(), []).append(npm)
                    for npm in npm_by_factory.get(f, []):
                        u = uri_brand(ch, npm)
                        if u:
                            bname = u[0]   # the venue's own contracts name it: this wins over a borrowed name
                            ev.append({"type": "uri", "detail": u[1]})
                            break
            types = {e["type"] for e in ev}
            if types <= {"code"}:
                bname = None   # identical code alone proves the codebase, not the team (Algebra / Uniswap / Sushi code is reused widely)
            if bname and any(b in bname.lower() for b in BIG_BRANDS) and not types & {"deployer", "address"}:
                bname = None   # a big brand needs its own deployer or address, not just its code or a borrowed descriptor
            if bname and ev:
                name = f"{bname} · {short_name}"
                r.update(name=name, source="evidence", evidence=ev, hover=f"{name} · identified by {', '.join(sorted({e['type'] for e in ev}))}")
            else:
                ref = f or suf
                r.update(name=f"{short_name} · {ref[:6]}…", source="fallback", evidence=ev or r.get("evidence"),
                         hover=f"{short_name} (unidentified) · {'factory' if r.get('factory') else 'contract'} {ref}")
            changes.append((ch, k, before, (r["name"], r["source"]), [e["type"] for e in ev] if ev else []))
        p = f"{ROOT}/data/{ch}/analytics/venue_ids.json"
        tmp = p + ".tmp"
        json.dump(ids[ch], open(tmp, "w"), indent=1, sort_keys=True)
        os.replace(tmp, p)
    return changes


if __name__ == "__main__":
    if "--evidence" not in sys.argv:
        main()
    for ch, k, b, a, ev in evidence_pass():
        print(f"{ch:9s} {k:28s} {b[0]!s:28s} [{b[1]}] -> {a[0]:32s} [{a[1]}] {','.join(ev)}")
