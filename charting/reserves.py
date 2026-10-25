"""Raw token balances held by DEX pools (and by Uniswap V4-style PoolManagers) at one block, for the protocol directory's liquidity.
Spawned by directory_core at most every 30 minutes, under nice. It reads <rt>/reserve_targets.json and writes <rt>/reserves.json:
  pools     {pool: {"balances": {token: raw}}}     ERC-20 balanceOf(pool) for the pool's two tokens (v2/v3/Algebra-style pools)
  managers  {venue key: {"manager", "balances"}}   balanceOf(PoolManager) for the priced tokens of that venue's top V4 pools; ETH via eth_getBalance
  lenders   {venue key: {holder: {token: raw}}}    tokens held by the lending contracts of the venues in targets.lending, from the econ
                                                   dimension tables: Comets (base + the venue's position assets), aTokens (their
                                                   underlying), Morpho Blue singletons (loan + collateral tokens), cTokens (their underlying;
                                                   native ETH by eth_getBalance). If the econ database can't be read, the previous run's
                                                   lenders are kept with their own block (lendersBlock).
usage: reserves.py <chain> <rt dir>
"""
import json
import os
import sys
import time
import urllib.request

import duckdb

ECON = "/srv/skew/em-data/econ"
ETH_LIKE = {"0x0000000000000000000000000000000000000000", "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}
RPC = {"robinhood": ["https://robinhood-rpc.publicnode.com", "https://robinhood.drpc.org", "https://rpc.mainnet.chain.robinhood.com"],
       "arbitrum": ["https://arbitrum-one-rpc.publicnode.com", "https://arbitrum.drpc.org", "https://arb1.arbitrum.io/rpc"]}
ZERO = "0x0000000000000000000000000000000000000000"
MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11"   # Multicall3, same address on both chains
CHUNK = 200                                                 # balance reads per eth_call


def rpc(chain, method, params):
    """One JSON-RPC request; tries each endpoint of the chain in turn, twice."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(2 * len(RPC[chain])):
        url = RPC[chain][attempt % len(RPC[chain])]
        try:
            req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "em-charts reserves"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
            if "result" in d:
                return d["result"]
            raise RuntimeError(str(d.get("error"))[:120])
        except Exception as e:
            print(time.strftime("%H:%M:%S"), "rpc retry", attempt, url.split("/")[2], repr(e)[:120], flush=True)
            time.sleep(1 + 2 * (attempt // len(RPC[chain])))
    return None


def w(x):
    return int(x).to_bytes(32, "big")


def balances(chain, asks, block):
    """Raw balances for (holder, token) pairs at one block through Multicall3.tryAggregate(false, calls); ETH via getEthBalance.
    Returns one int (or None when unreadable) per pair."""
    out = []
    for i in range(0, len(asks), CHUNK):
        part = asks[i:i + CHUNK]
        calls = [(MULTICALL, bytes.fromhex("4d2301cc" + h[2:].rjust(64, "0"))) if t == ZERO else
                 (t, bytes.fromhex("70a08231" + h[2:].rjust(64, "0"))) for h, t in part]
        n, offs, body = len(calls), b"", b""
        for tgt, data in calls:
            offs += w(32 * n + len(body))
            body += w(int(tgt, 16)) + w(64) + w(len(data)) + data + b"\0" * (-len(data) % 32)
        r = rpc(chain, "eth_call", [{"to": MULTICALL, "data": "0xbce38bd7" + (w(0) + w(64) + w(n) + offs + body).hex()}, block])
        if not r or len(r) < 66:
            out += [None] * n
            continue
        b = bytes.fromhex(r[2:])
        rd = lambda o: int.from_bytes(b[o:o + 32], "big")
        base = rd(0) + 32
        if rd(base - 32) != n:
            out += [None] * n
            continue
        for j in range(n):
            e = base + rd(base + 32 * j)
            ok, bo = rd(e), e + rd(e + 32)
            ln = rd(bo)
            out.append(int.from_bytes(b[bo + 32:bo + 32 + ln], "big") if ok and ln == 32 else None)
        time.sleep(0.2)
    return out


def lender_asks(chain, tg, priced):
    """(venue key, holder, token) for the token-holding contracts of the lending venues."""
    keys = set(tg.get("lending") or [])
    assets = {k: set(v) for k, v in (tg.get("lendAssets") or {}).items()}
    if not keys:
        return []
    c = duckdb.connect()
    c.execute(f"ATTACH '{ECON}/{chain}/econ.duckdb' AS e (READ_ONLY)")
    have = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog = 'e'").fetchall()}
    q = lambda tab, sql: c.execute(sql).fetchall() if tab in have else []
    asks = []
    for comet, proto, base in q("dim_comets", "SELECT lower(comet), protocol, lower(base_token) FROM e.dim_comets"):
        if proto in keys:
            asks += [(proto, comet, t) for t in sorted(({base} | assets.get(proto, set())) & priced)]
    if "dim_aave_pools" in have:
        for atok, und, proto in q("dim_aave_tokens", """SELECT lower(t.token), lower(t.underlying), p.protocol FROM e.dim_aave_tokens t
                                                      JOIN e.dim_aave_pools p ON p.pool = t.pool WHERE t.kind = 'atoken'"""):
            if proto in keys and und in priced:
                asks.append((proto, atok, und))
    for morpho, proto, tok in q("dim_morpho_markets", """SELECT DISTINCT lower(morpho), protocol, lower(tok) FROM (
                                    SELECT morpho, protocol, loan_token AS tok FROM e.dim_morpho_markets
                                    UNION ALL SELECT morpho, protocol, collateral_token FROM e.dim_morpho_markets)"""):
        if proto in keys and tok in priced and tok != ZERO:
            asks.append((proto, morpho, tok))
    for ct, proto, und in q("dim_ctokens", "SELECT lower(ctoken), protocol, lower(underlying) FROM e.dim_ctokens"):
        u = ZERO if (und or ZERO) in ETH_LIKE else und
        if proto in keys and u in priced:
            asks.append((proto, ct, u))
    c.close()
    return list(dict.fromkeys(asks))


def main(chain, rt):
    tg = json.load(open(f"{rt}/reserve_targets.json"))
    c = duckdb.connect()
    c.execute(f"ATTACH '/srv/skew/em-charts/data/{chain}/candles.duckdb' AS cd (READ_ONLY)")
    want = [p["pool"] for p in tg["pools"]]
    pair = {}
    for pool, tok, quote in c.execute(f"SELECT pool, token, quote FROM cd.pools WHERE pool IN ({','.join(repr(p) for p in want) or 'NULL'})").fetchall():
        pair.setdefault(pool, set()).update([tok, quote])
    c.close()
    priced = set(tg.get("priced") or [])
    asks = []                               # (holder kind, key, holder address, token)
    for p in tg["pools"]:
        toks = pair.get(p["pool"]) or set()
        if len(p["pool"]) == 66:            # V4 pool id: tokens live in the venue's PoolManager
            m = (tg.get("managers") or {}).get(p["venue"])
            for t in toks & priced if m else ():
                asks.append(("manager", p["venue"], m, t))
        elif len(toks) == 2 and ZERO not in toks:
            for t in toks:
                asks.append(("pool", p["pool"], p["pool"], t))
    asks = list(dict.fromkeys(asks))
    try:
        lerr, lasks = None, lender_asks(chain, tg, priced)
    except Exception as e:                  # e.g. the econ database is locked by a refresh: keep the previous lenders below
        lerr, lasks = repr(e)[:200], []
    asks += [("lender", v, h, t) for v, h, t in lasks]
    block = rpc(chain, "eth_blockNumber", [])
    if not block:
        sys.exit("no block number")
    res = balances(chain, [(h, t) for _, _, h, t in asks], block)
    out = {"chain": chain, "block": int(block, 16), "fetchedAt": int(time.time()), "pools": {}, "managers": {}, "lenders": {}, "missing": 0,
           "lendersBlock": int(block, 16), "lendersFetchedAt": int(time.time())}
    for (kind, key, holder, tok), r in zip(asks, res):
        if r is None:
            out["missing"] += 1
            continue
        if kind == "pool":
            out["pools"].setdefault(key, {"balances": {}})["balances"][tok] = str(r)
        elif kind == "lender":
            out["lenders"].setdefault(key, {}).setdefault(holder, {})[tok] = str(r)
        else:
            out["managers"].setdefault(key, {"manager": holder, "balances": {}})["balances"][tok] = str(r)
    out["pools"] = {k: v for k, v in out["pools"].items() if len(v["balances"]) == 2}   # a pool with an unreadable side is not counted
    if lerr:
        try:
            prev = json.load(open(f"{rt}/reserves.json"))
        except (OSError, ValueError):
            prev = {}
        if prev.get("lenders"):
            out.update({k: prev[k] for k in ("lenders", "lendersBlock", "lendersFetchedAt") if k in prev})
        else:
            out.pop("lenders")              # the directory then says "on-chain holdings not fetched yet"
        out["lendersError"] = lerr
    json.dump(out, open(f"{rt}/reserves.json.tmp", "w"))
    os.replace(f"{rt}/reserves.json.tmp", f"{rt}/reserves.json")
    print(time.strftime("%H:%M:%S"), chain, "block", out["block"], "pools", len(out["pools"]), "managers", len(out["managers"]),
          "lenders", sum(len(v) for v in (out.get("lenders") or {}).values()), "missing", out["missing"], ("lenders error " + lerr) if lerr else "", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
