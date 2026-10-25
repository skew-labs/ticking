"""Protocol directory core, imported and reloaded by flowmap_core.build on every refresh (no em-charts restart).

One row per protocol entity per chain: liquidity, volume, active wallets and the naming evidence. Each protocol also gets a detail
record: top wallets by role, net flows by wallet class, its pools and lending markets. Everything is built from the same refresh
tables as the flow map (fe, trc, fm_pools, px), so protocol volume reconciles with the trade tape and with the per-market flows.
Results go to the analytics.sqlite3 table directory(key, json, updated_at), under the keys 'protocols', 'protocol:<id>' and
'reconciliation'.

Liquidity is never estimated:
  DEX      tokens held by the pool contracts (for Uniswap V4-style venues, by the PoolManager) at one block, valued at Ticking prices.
           A pool counts only when every token in it is priced and its sides are not wildly mismatched. Raw balances come from
           reserves.py (RPC balanceOf for the top pools by 7-day volume), which is spawned at most every 30 minutes.
  guard    a holding counts at its price when the token is an anchor (WETH, USDC, ...), econ prices it without DEX trades
           (Chainlink, ERC-4626, peg), or its price tracks $1 or an anchor (stablecoins, LSTs, LRTs, wrapped BTC). A DEX-priced holding counts only up to VOL_MULT x the token's 30-day DEX volume (econ's
           rule), or LIQUID_MULT x for tokens with at least LIQUID_VOL of volume: a memecoin posted as collateral by the billion is
           not worth amount x last trade. Excluded holdings are listed in liquidity.excluded with the reason.
  lending  tokens held by the protocol's contracts at one block (Compound V3 Comets, Aave-style aTokens, Morpho Blue singletons,
           Compound V2-style cTokens), valued at Ticking prices: by construction supplied + collateral - borrowed. Read by reserves.py
           from the econ dimension tables. The decoded positions are kept as the breakdown; when they disagree with the chain by more
           than DECODED_TOL the row carries a caution, because then the per-wallet amounts are not reliable.
  others   null, with the reason
"""
import json
import os
import re
import subprocess
import sys
import time

ZERO = "0x0000000000000000000000000000000000000000"
EM = "/srv/skew/em-data"
TOP_POOLS = 150
RESERVES_EVERY = 1800
MISMATCH = 10.0          # a pool whose base side is worth >10x its quote side is mispriced, not liquid
DECODED_TOL = 0.25       # decoded lending positions vs on-chain holdings
VOL_MULT = 5             # a DEX-priced holding counts only up to this many times the token's 30-day DEX volume (econ's rule) ...
LIQUID_VOL = 250_000     # ... or up to LIQUID_MULT times for tokens with at least this 30-day DEX volume (a real market price)
LIQUID_MULT = 50
TOP_WALLETS = 15
KIND = {"swap": "dex", "lp": "dex", "lending": "lending", "borrow": "lending", "liquidation": "lending", "bridge": "bridge", "perp": "perp",
        "stake": "stake", "vault": "vault", "issuance": "issuer"}
AGG = {"dex_other": "aggregate", "erc4626": "aggregate", "other": "aggregate"}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def rows(c, sql, args=None):
    cur = c.execute(sql, args or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def r2(v):
    return None if v is None else round(float(v), 2)


def spawn_reserves(chain, rt, targets, log):
    stamp = f"{rt}/reserves.run"
    if os.path.exists(stamp) and time.time() - os.path.getmtime(stamp) < RESERVES_EVERY:
        return
    json.dump(targets, open(f"{rt}/reserve_targets.json.tmp", "w"))
    os.replace(f"{rt}/reserve_targets.json.tmp", f"{rt}/reserve_targets.json")
    open(stamp, "w").write(str(int(time.time())))
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reserves.py")
    subprocess.Popen(["nice", "-n", "15", sys.executable, script, chain, rt], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=open(f"{rt}/reserves.log", "a"), stderr=subprocess.STDOUT)
    log(f"{chain} directory: fetching reserves for {len(targets['pools'])} pools and {len(targets['lending'])} lending venues in the background")


DUMP = "/srv/skew/em-flowmap-dev/dirdump"


def dump(c, chain, now):
    """Dev only: when DUMP/<chain>.flag exists, copy the refresh tables this module reads, for offline runs; the flag is removed."""
    flag = f"{DUMP}/{chain}.flag"
    if not os.path.exists(flag):
        return
    os.remove(flag)
    d = f"{DUMP}/{chain}"
    os.makedirs(d, exist_ok=True)
    for t in ("fe", "fm_pools", "fm_tokens", "px"):
        c.execute(f"COPY {t} TO '{d}/{t}.parquet' (FORMAT parquet)")
    c.execute(f"COPY (SELECT pool, token, tx, log_index, time, usd FROM trc WHERE time >= {now - 7 * 86400}) TO '{d}/trc.parquet' (FORMAT parquet)")
    open(f"{d}/now", "w").write(str(now))


def build(c, chain, now, out, res, ids, namer, entity, log=print, rt=None):
    t0 = time.time()
    rt = rt or f"{out}/rt"
    dump(c, chain, now)
    lo24, lo7 = now - 86400, now - 7 * 86400
    pos = f"{out}/positions_snap.parquet"
    try:
        static = json.load(open(f"{out}/static.json"))
    except (OSError, ValueError):
        static = {}
    fe_keys = [r[0] for r in c.execute("SELECT DISTINCT venue FROM fe WHERE venue NOT LIKE '@%'").fetchall()]
    pos_keys = [r[0] for r in c.execute(f"SELECT DISTINCT protocol FROM '{pos}' WHERE protocol IS NOT NULL").fetchall()] if os.path.exists(pos) else []
    keys = sorted(set(k for k in fe_keys + pos_keys if k))
    named = namer(keys)                                    # key -> (display name, hover, source)
    vkind = dict(c.execute("""SELECT venue, arg_max(type, s) FROM (SELECT venue, type, sum(usd * w) AS s FROM fe GROUP BY ALL) GROUP BY venue""").fetchall())
    ent = {}
    for k in keys:
        name, hover, src = named[k]
        pid = slug(name)
        e = ent.setdefault(pid, {"id": pid, "name": name, "keys": [], "kinds": [], "evidence": [], "aggregate": False})
        e["keys"].append(k)
        e["kinds"].append(KIND.get(vkind.get(k)) or ("lending" if k in pos_keys else "dex"))
        e["aggregate"] |= k.split(":")[0] in AGG
        rec = (ids or {}).get(k) or {}
        e["evidence"].append({"key": k, "source": src, "text": rec.get("evidence") or hover or
                              ("decoded through the protocol's known deployment (protocol registry)" if src == "registry" else None)})
    for e in ent.values():
        e["kind"] = max(set(e["kinds"]), key=e["kinds"].count)
        e.pop("kinds")
    pid_of = {k: pid for pid, e in ent.items() for k in e["keys"]}
    c.execute("CREATE OR REPLACE TEMP TABLE dk(venue VARCHAR, pid VARCHAR)")
    c.executemany("INSERT INTO dk VALUES (?, ?)", [(k, pid) for pid, e in ent.items() for k in e["keys"]])
    # volume (swap volume for DEXs, money moved for the rest) and active wallets
    m = {r["pid"]: r for r in rows(c, f"""
      SELECT k.pid, sum(CASE WHEN f.time >= {lo24} THEN f.usd * f.w END) AS v24, sum(f.usd * f.w) AS v7,
             sum(CASE WHEN f.src = 'dex' AND f.time >= {lo24} THEN f.usd * f.w END) AS s24, sum(CASE WHEN f.src = 'dex' THEN f.usd * f.w END) AS s7,
             count(DISTINCT CASE WHEN f.time >= {lo24} THEN f.wallet END) AS u24, count(DISTINCT f.wallet) AS u7
      FROM fe f JOIN dk k ON k.venue = f.venue WHERE f.time >= {lo7} GROUP BY 1""")}
    # prices and decimals (quality-ok Ticking prices; ETH at the WETH price)
    pxr = c.execute("SELECT token, price, kind FROM px WHERE price > 0 AND coalesce(quality, 'ok') = 'ok'").fetchall()
    px = {t: float(p) for t, p, _ in pxr}
    anchors = {t for t, _, k in pxr if k in ("anchor", "usd_anchor")} | {ZERO}
    eth = c.execute(f"SELECT price FROM fm_tokens WHERE token = '{ZERO}'").fetchone()
    if eth:
        px[ZERO] = float(eth[0])
    tm = f"{EM}/econ/{chain}/token_meta.parquet"
    meta = {t: (s, int(d) if d is not None else None) for t, s, d in c.execute(
        f"SELECT lower(token), any_value(symbol), any_value(decimals) FROM read_parquet('{tm}') GROUP BY 1").fetchall()} if os.path.exists(tm) else {}
    meta[ZERO] = ("ETH", 18)
    sym = lambda t: (meta.get(t) or (None,))[0] or (t[:8] + "…" if t else None)
    # DEX pools: 7-day volume per pool; the top pools get their reserves fetched in the background
    pools = rows(c, f"""
      WITH t AS (SELECT pool, token, usd, count(*) OVER (PARTITION BY tx, log_index) AS n FROM trc WHERE time >= {lo7} AND usd > 0)
      SELECT t.pool, coalesce(p.protocol, 'dex_other') AS venue, sum(t.usd / t.n) AS v7, list(DISTINCT t.token) AS tokens
      FROM t LEFT JOIN fm_pools p ON p.pool = t.pool GROUP BY 1, 2 ORDER BY v7 DESC""")
    dims = f"{out}/dims/v4_managers.parquet"
    mgr = {p: v for v, p in c.execute(f"SELECT manager, protocol FROM '{dims}'").fetchall()} if os.path.exists(dims) else {}
    lend_assets = {}
    if os.path.exists(pos):
        for k, a in c.execute(f"SELECT protocol, lower(asset) FROM '{pos}' WHERE usd > 0 AND protocol IS NOT NULL AND asset IS NOT NULL GROUP BY ALL").fetchall():
            lend_assets.setdefault(k, []).append(a)
    spawn_reserves(chain, rt, {"chain": chain, "pools": [{"pool": p["pool"], "venue": p["venue"]} for p in pools[:TOP_POOLS]],
                               "managers": {v: m_ for v, m_ in mgr.items()}, "priced": sorted(px), "lending": sorted(lend_assets),
                               "lendAssets": lend_assets}, log)
    try:
        R = json.load(open(f"{rt}/reserves.json"))
    except (OSError, ValueError):
        R = None
    pool_rows, liq, cover = {}, {}, {}
    val = lambda t, raw: (raw / 10 ** meta[t][1]) * px[t] if t in px and t in meta and meta[t][1] is not None else None
    # thin markets: a lending contract or PoolManager holding most of a token's supply is not worth amount x last DEX price
    vol30 = dict(c.execute(f"""SELECT token, sum(usd / n) FROM (SELECT token, usd, count(*) OVER (PARTITION BY tx, log_index) AS n FROM trc
                               WHERE time >= {now - 30 * 86400} AND usd > 0) GROUP BY 1""").fetchall())
    eps = {}                                              # econ price source and 30-day DEX volume per token
    for name in ("v3/prices_head.v3.csv", "v2/prices_head.v2.csv"):
        f = f"{EM}/econ/{chain}/{name}"
        if os.path.exists(f):
            eps = {str(t).lower(): (s, v) for t, s, v in c.execute(f"SELECT token, source, vol_30d_usd FROM read_csv('{f}', all_varchar=true)").fetchall()}
            break

    big = [px[a] for a in anchors if px.get(a, 0) >= 100]          # ETH- and BTC-like anchor prices

    def pegged(p):
        """Stablecoins and anchor derivatives (LSTs, LRTs, wrapped BTC): the price tracks $1 or an anchor, not a thin market."""
        return abs(p - 1) <= 0.1 or any(0.9 <= p / a <= 1.5 for a in big)

    def held_val(t, raw):
        """(usd, None) or (None, reason). Anchors, tokens econ prices without DEX trades (Chainlink, ERC-4626, peg) and pegged
        tokens count; any other DEX-priced holding counts up to VOL_MULT x the token's 30-day DEX volume (LIQUID_MULT x for
        liquid tokens)."""
        v = val(t, raw)
        if v is None:
            return None, "no trusted price"
        src, ev = eps.get(t, (None, None))
        if t in anchors or (src and not src.startswith("dex")) or pegged(px[t]):
            return v, None
        vol = float(ev) if ev not in (None, "", "NULL") else (vol30.get(t) or 0)
        cap = (LIQUID_MULT if vol >= LIQUID_VOL else VOL_MULT) * vol
        if v > cap:
            return None, "worth %s at the last DEX price, more than %dx its 30-day DEX volume ($%s): not counted" % (
                "${:,.0f}".format(v), LIQUID_MULT if vol >= LIQUID_VOL else VOL_MULT, "{:,.0f}".format(vol))
        return v, None
    for p in pools:
        pid = pid_of.get(p["venue"])
        r = (R or {}).get("pools", {}).get(p["pool"]) if R else None
        row = {"pool": p["pool"], "venueKey": p["venue"], "volume7dUsd": r2(p["v7"]), "liquidityUsd": None, "liquidityReason": None, "tokens": []}
        if len(p["pool"]) == 66:
            row["liquidityReason"] = "Uniswap V4-style pool: its tokens sit in the shared PoolManager, counted at protocol level"
        elif r is None:
            row["liquidityReason"] = "reserves not fetched (only the top %d pools by 7-day volume are read)" % TOP_POOLS if R else "reserves not fetched yet"
        else:
            sides = [(t, val(t, int(b))) for t, b in r["balances"].items()]
            row["tokens"] = [{"token": t, "symbol": sym(t), "amount": (int(r["balances"][t]) / 10 ** meta[t][1]) if t in meta and meta[t][1] is not None else None,
                              "usd": r2(u)} for t, u in sides]
            vals = [u for _, u in sides]
            if not sides or any(u is None for u in vals):
                row["liquidityReason"] = "a token in the pool has no trusted price"
            elif len(vals) == 2 and min(vals) > 0 and max(vals) / min(vals) > MISMATCH and max(sides, key=lambda s: s[1])[0] not in anchors:
                row["liquidityReason"] = "token values differ more than %dx: a price is stale, not counted" % MISMATCH
            else:
                row["liquidityUsd"] = r2(sum(vals))
        if not row["tokens"]:
            row["tokens"] = [{"token": t, "symbol": sym(t)} for t in p["tokens"]]
        if pid:
            pool_rows.setdefault(pid, []).append(row)
            cv = cover.setdefault(pid, {"pools": 0, "counted": 0, "v7": 0.0, "v7counted": 0.0})
            cv["pools"] += 1
            cv["v7"] += p["v7"] or 0
            if row["liquidityUsd"] is not None:
                cv["counted"] += 1
                cv["v7counted"] += p["v7"] or 0
                liq[pid] = liq.get(pid, 0.0) + row["liquidityUsd"]
    excluded = {}
    for venue, b in ((R or {}).get("managers") or {}).items():   # V4-style: the PoolManager's priced token balances
        pid = pid_of.get(venue)
        vals = []
        for t, x in b["balances"].items():
            v, why = held_val(t, int(x))
            if v is not None:
                vals.append(v)
            elif pid and why != "no trusted price":
                excluded.setdefault(pid, []).append({"holder": b["manager"], "token": t, "symbol": sym(t), "reason": why})
        if pid and vals:
            liq[pid] = liq.get(pid, 0.0) + sum(vals)
            cv = cover.setdefault(pid, {"pools": 0, "counted": 0, "v7": 0.0, "v7counted": 0.0})
            cv["manager"] = {"address": b["manager"], "tokensPriced": len(vals)}
    # lending: tokens held by the protocol contracts (reserves.py), and the decoded positions at the snapshot as the breakdown
    held, held_n, held_ok = {}, {}, {}
    for venue, hs in ((R or {}).get("lenders") or {}).items():
        pid = pid_of.get(venue)
        if not pid:
            continue
        for holder, bal in hs.items():
            held_n[pid] = held_n.get(pid, 0) + 1
            for t, x in bal.items():
                v, why = held_val(t, int(x))
                if v is not None:
                    held[pid] = held.get(pid, 0.0) + v
                    held_ok[pid] = held_ok.get(pid, 0) + 1
                elif why != "no trusted price":
                    excluded.setdefault(pid, []).append({"holder": holder, "token": t, "symbol": sym(t), "reason": why})
    lend = {}
    if os.path.exists(pos):
        for r in rows(c, f"""SELECT k.pid, p.category, sum(CASE WHEN coalesce(p.quality, 'ok') = 'ok' THEN p.usd END) AS usd,
                                    sum(CASE WHEN coalesce(p.quality, 'ok') <> 'ok' THEN p.usd END) AS flagged, count(DISTINCT p.wallet) AS wallets
                             FROM '{pos}' p JOIN dk k ON k.venue = p.protocol WHERE p.usd > 0 GROUP BY 1, 2"""):
            d = lend.setdefault(r["pid"], {"suppliedUsd": 0.0, "collateralUsd": 0.0, "borrowedUsd": 0.0, "flaggedUsd": 0.0, "positions": {}})
            col = {"supplied": "suppliedUsd", "collateral": "collateralUsd", "debt": "borrowedUsd"}.get(r["category"])
            if col:
                d[col] += r["usd"] or 0
                d["positions"][r["category"]] = r["wallets"]
            d["flaggedUsd"] += r["flagged"] or 0
    asof = static.get("econ_as_of")
    out_rows = []
    for pid, e in ent.items():
        mm = m.get(pid) or {}
        dex = e["kind"] == "dex"
        row = {"id": pid, "chain": chain, "name": e["name"], "kind": e["kind"], "aggregate": e["aggregate"], "icon": None,
               "node": f"{chain}:v:{pid}", "entity_id": entity(e),
               "volume24hUsd": r2(mm.get("s24") if dex else mm.get("v24")), "volume7dUsd": r2(mm.get("s7") if dex else mm.get("v7")),
               "flow7dUsd": r2(mm.get("v7")), "users24h": mm.get("u24") or 0, "users7d": mm.get("u7") or 0,
               "volumeBasis": "swap volume, each swap counted once (Ticking-served tokens)" if dex else "money moved through the protocol (decoded actions)",
               "liquidityUsd": None, "liquidity": None, "evidence": e["evidence"], "venueKeys": e["keys"]}
        d = lend.get(pid)
        dec = {"basis": "decoded positions at the map snapshot", "asOf": asof, "suppliedUsd": r2(d["suppliedUsd"]),
               "collateralUsd": r2(d["collateralUsd"]), "borrowedUsd": r2(d["borrowedUsd"]),
               "netUsd": r2(d["suppliedUsd"] + d["collateralUsd"] - d["borrowedUsd"]), "flaggedUsdExcluded": r2(d["flaggedUsd"]),
               "positions": d["positions"]} if d else None
        if e["kind"] == "lending" and held_ok.get(pid):
            row["liquidityUsd"] = r2(held[pid])
            row["liquidity"] = {"basis": "tokens held by the protocol's contracts at block %s, at Ticking prices (= supplied + collateral - borrowed)"
                                         % R.get("lendersBlock", R.get("block")), "asOfTime": R.get("lendersFetchedAt", R.get("fetchedAt")),
                                "contracts": held_n[pid], "balancesPriced": held_ok[pid], "decoded": dec,
                                "excluded": excluded.get(pid, [])[:10]}
            if dec and row["liquidityUsd"]:
                gap = abs(dec["netUsd"] - row["liquidityUsd"]) / row["liquidityUsd"]
                row["liquidity"]["decodedVsOnchainPct"] = round(100 * gap, 1)
                if gap > DECODED_TOL:
                    row["liquidity"]["caution"] = ("decoded positions differ from the on-chain holdings by %d%%, so the per-wallet supplied and "
                                                   "borrowed amounts for this protocol are not reliable" % round(100 * gap))
        elif dex and pid in liq:
            cv = cover.get(pid, {})
            row["liquidityUsd"] = r2(liq[pid])
            row["liquidity"] = {"basis": "tokens held by the pool contracts (V4-style: the PoolManager) at block %s, at Ticking prices" % (R or {}).get("block"),
                                "asOfTime": (R or {}).get("fetchedAt"), "poolsCounted": cv.get("counted"), "poolsTraded7d": cv.get("pools"),
                                "volumeShareCounted": round(cv["v7counted"] / cv["v7"], 4) if cv.get("v7") else None, "manager": cv.get("manager"),
                                "excluded": excluded.get(pid, [])[:10]}
        else:
            reason = ("no pool of this venue has fetched reserves with every token priced yet" if dex else
                      ("on-chain holdings not fetched yet" if R is None or "lenders" not in R else
                       "holdings read, but no held token has a trusted price" if held_n.get(pid) else
                       "no token-holding contracts known for this protocol, so its liquidity is not computed" if d else
                       "no decoded lending positions at the snapshot") if e["kind"] == "lending" else
                      "TVL is not computed for %s venues (needs per-protocol accounting)" % e["kind"])
            row["liquidity"] = {"reason": reason, **({"decoded": dec} if dec else {})}
        out_rows.append(row)
    # details: top wallets by role, net flows by wallet class, pools, lending markets
    det = {r["id"]: {"schema": "protocol/1", **{k: r[k] for k in r}, "topWallets": {}, "flowsByClass": {}, "pools": pool_rows.get(r["id"], [])[:30],
                     "markets": [], "cautions": [r["liquidity"]["caution"]] if (r["liquidity"] or {}).get("caution") else []} for r in out_rows}
    for role, where, money in (("traders", "f.src = 'dex'", "sum(f.usd * f.w)"), ("lps", "f.type = 'lp'", "sum(f.usd * f.w)")):
        for r in rows(c, f"""SELECT k.pid, f.wallet, any_value(f.cls) AS class, {money} AS tot, sum(CASE WHEN f.dir = 1 THEN f.usd * f.w END) AS in_usd,
                                    sum(CASE WHEN f.dir = -1 THEN f.usd * f.w END) AS out_usd, count(*) AS n
                             FROM fe f JOIN dk k ON k.venue = f.venue WHERE {where} AND f.time >= {lo7} GROUP BY 1, 2
                             QUALIFY row_number() OVER (PARTITION BY k.pid ORDER BY tot DESC) <= {TOP_WALLETS}"""):
            a = r["wallet"]
            det[r["pid"]]["topWallets"].setdefault(role, []).append(
                {"address": a, "id": f"{chain}:w:{a}", "entity_id": "addr:" + a, "class": r["class"], "usd7d": r2(r["tot"]),
                 **({"buyUsd": r2(r["in_usd"]), "sellUsd": r2(r["out_usd"]), "fills": r["n"]} if role == "traders" else
                    {"addedUsd": r2(r["in_usd"]), "removedUsd": r2(r["out_usd"]), "actions": r["n"]})})
    if os.path.exists(pos):
        for role, cats in (("suppliers", "('supplied', 'collateral')"), ("borrowers", "('debt')")):
            for r in rows(c, f"""SELECT k.pid, p.wallet, sum(p.usd) AS tot, list(DISTINCT p.symbol) AS assets FROM '{pos}' p JOIN dk k ON k.venue = p.protocol
                                 WHERE p.category IN {cats} AND p.usd > 0 AND coalesce(p.quality, 'ok') = 'ok' GROUP BY 1, 2
                                 QUALIFY row_number() OVER (PARTITION BY k.pid ORDER BY tot DESC) <= {TOP_WALLETS}"""):
                a = r["wallet"]
                det[r["pid"]]["topWallets"].setdefault(role, []).append({"address": a, "id": f"{chain}:w:{a}", "entity_id": "addr:" + a,
                                                                          "usd": r2(r["tot"]), "assets": [s for s in r["assets"] if s][:6], "asOf": asof})
        for r in rows(c, f"""SELECT k.pid, p.asset, any_value(p.symbol) AS symbol, sum(CASE WHEN p.category = 'supplied' THEN p.usd END) AS sup,
                                    sum(CASE WHEN p.category = 'collateral' THEN p.usd END) AS col, sum(CASE WHEN p.category = 'debt' THEN p.usd END) AS debt
                             FROM '{pos}' p JOIN dk k ON k.venue = p.protocol WHERE p.usd > 0 AND coalesce(p.quality, 'ok') = 'ok' GROUP BY 1, 2
                             QUALIFY row_number() OVER (PARTITION BY k.pid ORDER BY coalesce(sup, 0) + coalesce(col, 0) + coalesce(debt, 0) DESC) <= 30"""):
            det[r["pid"]]["markets"].append({"asset": r["asset"], "symbol": r["symbol"], "suppliedUsd": r2(r["sup"]), "collateralUsd": r2(r["col"]),
                                             "borrowedUsd": r2(r["debt"]), "asOf": asof})
    for r in rows(c, f"""SELECT k.pid, f.cls, sum(CASE WHEN f.dir = 1 AND f.time >= {lo24} THEN f.usd * f.w END) AS i24,
                                sum(CASE WHEN f.dir = -1 AND f.time >= {lo24} THEN f.usd * f.w END) AS o24,
                                sum(CASE WHEN f.dir = 1 THEN f.usd * f.w END) AS i7, sum(CASE WHEN f.dir = -1 THEN f.usd * f.w END) AS o7,
                                count(DISTINCT f.wallet) AS wallets
                         FROM fe f JOIN dk k ON k.venue = f.venue WHERE f.time >= {lo7} GROUP BY 1, 2"""):
        for w, i, o in (("24h", r["i24"], r["o24"]), ("7d", r["i7"], r["o7"])):
            det[r["pid"]]["flowsByClass"].setdefault(w, []).append({"class": r["cls"], "inUsd": r2(i or 0), "outUsd": r2(o or 0),
                                                                    "netUsd": r2((i or 0) - (o or 0)), "wallets": r["wallets"] if w == "7d" else None})
    for d in det.values():
        for w in d["flowsByClass"].values():
            w.sort(key=lambda x: -abs(x["netUsd"] or 0))
    # reconciliation: protocol rows vs the trade tape and vs the per-market flows written by this same refresh
    rec = {}
    for wname, lo in (("24h", lo24), ("7d", lo7)):
        prot = sum((r["volume24hUsd"] if wname == "24h" else r["volume7dUsd"]) or 0 for r in out_rows if r["kind"] == "dex")
        nondex_swaps = c.execute(f"""SELECT sum(f.usd * f.w) FROM fe f JOIN dk k ON k.venue = f.venue
                                     WHERE f.src = 'dex' AND f.time >= {lo} AND k.pid IN ({','.join("'" + r['id'] + "'" for r in out_rows if r['kind'] != 'dex') or "''"})""").fetchone()[0] or 0
        tape = c.execute(f"""SELECT sum(usd / n) FROM (SELECT usd, count(*) OVER (PARTITION BY tx, log_index) AS n FROM trc
                             WHERE time >= {lo} AND usd > 0)""").fetchone()[0] or 0
        fl = res.execute("SELECT json FROM flows WHERE window = ?", (wname,)).fetchall()
        toks = [json.loads(j) for (j,) in fl]
        fsum = sum((x.get("buyUsd") or 0) + (x.get("sellUsd") or 0) for o in toks for x in o.get("classes") or [])
        mk = [r[0].split(":", 1)[1] for r in res.execute("SELECT market FROM flows WHERE window = ?", (wname,)).fetchall()]
        c.execute("CREATE OR REPLACE TEMP TABLE dir_mk(token VARCHAR)")
        c.executemany("INSERT INTO dir_mk VALUES (?)", [(t,) for t in mk])
        side = c.execute(f"""SELECT sum(f.usd) FROM fe f JOIN dk k ON k.venue = f.venue WHERE f.src = 'dex' AND f.time >= {lo}
                             AND f.token IN (SELECT token FROM dir_mk)""").fetchone()[0] or 0
        pct = lambda a, b: round(100 * abs(a - b) / b, 4) if b else None
        rec[wname] = {"dexProtocolsUsd": r2(prot + nondex_swaps), "tapeUsd": r2(tape), "vsTapeDiffPct": pct(prot + nondex_swaps, tape),
                      "dexProtocolsTokenSideUsd": r2(side), "flowsClassesUsd": r2(fsum), "vsFlowsDiffPct": pct(side, fsum), "markets": len(mk)}
    res.executescript("CREATE TABLE IF NOT EXISTS directory(key TEXT PRIMARY KEY, json TEXT, updated_at INTEGER);")
    ts = int(time.time())
    res.execute("BEGIN")
    res.execute("DELETE FROM directory")
    res.executemany("INSERT INTO directory VALUES (?, ?, ?)",
                    [("protocols", json.dumps({"chain": chain, "asOf": now, "rows": out_rows, "reconciliation": rec}, separators=(",", ":")), ts)]
                    + [("protocol:" + k, json.dumps({**v, "asOf": now}, separators=(",", ":")), ts) for k, v in det.items()])
    res.execute("COMMIT")
    log(f"{chain} directory {round(time.time() - t0, 1)}s protocols {len(out_rows)} reconciliation 7d tape {rec['7d']['vsTapeDiffPct']}% flows {rec['7d']['vsFlowsDiffPct']}%")
    return rec
