"""Money-flow map core (re-imported by flowmap.py when this file changes). Money-flow map for Ticking's wallet map: entity nodes and the USD that moved between them, per chain and window (1h/24h/7d).

Built at the end of analytics.refresh from the same tables as the per-market flows (so the numbers reconcile):
  dex       trc  (deep tape + hot fills, wallet class per trade)      wallet class <-> DEX protocol (pool -> protocol from the dims)
  lend      levc (Morpho Blue / Aave v3 family events)                  wallet class <-> lending protocol
  live      live.db decoded actions (bridges, LP, perps, staking, vaults, other lenders; swaps and Morpho/Aave are taken above)
  xfer      follower transfer chunks: deposits into / withdrawals from exchange wallets, stock-token mints and burns
Node ids are chain-qualified, <chain>:<kind>:<id> with kind c (wallet class), v (venue), x (outside the chain), i (issuer), w (wallet), and every
node carries a chain-agnostic entity_id (class:<group>, protocol:<slug> for a named protocol, exchange:<name>, addr:<0x…>; an unidentified venue
stays chain-scoped: venue:<chain>:<registry key>). Edges carry src_chain / dst_chain (a bridge pass-through goes to dst_chain 'external' until
bridge legs are matched across chains). schema 'flowmap/2'; a later chain=all view adds nodes of several chains to one payload.
Edge direction: source = wallet class, target = venue; fwd = money into the venue (buys, deposits, bridge out, repay),
back = money out of it (sells, withdrawals, bridge in, borrow). usd = fwd + back with token-to-token swaps counted once.
Results: analytics.sqlite3 tables flowmap(window, json) and flow_pools(pool, venue) (read by emapi / server.py).
"""
import json
import os
import re
import time

LIVE_DB = "/srv/skew/em-galaxy/live/live.db"
DIMS = "/srv/skew/em-galaxy/live/dims_{chain}.sqlite"
LABELS = "/srv/skew/em-graphloop/export/{chain}/labels.parquet"
WIN = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
MAX_VENUES = 26
MIN_VENUE_SHARE = 0.001   # venues below 0.1% of the window's venue flow fold into "Other DEXs" / "Other protocols"
ZERO = "0x0000000000000000000000000000000000000000"

CLASS_NAME = {"retail": "Retail wallets", "whale": "Whales", "mev": "MEV bots", "contract": "Contract wallets", "exchange": "Exchanges"}
CLASS_HINT = {"retail": "EOAs below the whale threshold of the token they move", "whale": "EOAs holding >= $100K and >= 0.1% of a token's supply",
              "mev": "Arbitrage and sandwich bots (label engine)", "exchange": "Exchange hot wallets and their deposit addresses",
              "contract": "Safes, routers, aggregators and other contracts that move money for themselves (not protocol venues)"}
VENUE_IDS = "/srv/skew/em-charts/data/{chain}/analytics/venue_ids.json"
BASE = {
    "uniswap_v4": "Uniswap V4", "uniswap_v3": "Uniswap V3", "uniswap_v2": "Uniswap V2", "univ3": "Uniswap V3", "univ2": "Uniswap V2",
    "sushiswap_v2": "SushiSwap V2", "sushiswap_v3": "SushiSwap V3", "camelot_v2": "Camelot V2", "camelot_v3": "Camelot V3",
    "pancakeswap_v3": "PancakeSwap V3", "pancake_v3": "PancakeSwap V3", "ramses_v2": "Ramses V2", "traderjoe": "Trader Joe",
    "morpho_blue": "Morpho Blue", "aave_v3": "Aave V3", "aave_v2": "Aave V2", "compound_v3": "Compound V3", "comet": "Compound V3",
    "dolomite": "Dolomite", "silo_v1": "Silo", "silo_v2": "Silo", "radiant": "Radiant", "fluid": "Fluid",
    "gmx_v2": "GMX V2", "gmx_v1": "GMX V1", "cctp_v1": "Circle CCTP", "cctp_v2": "Circle CCTP", "relay": "Relay", "oft": "LayerZero OFT",
    "arbitrum_bridge": "Arbitrum Bridge", "robinhood_bridge": "Robinhood Bridge", "across": "Across", "stargate": "Stargate",
    "ethena": "Ethena", "erc4626": "ERC-4626 vaults", "merkl": "Merkl", "pendle": "Pendle", "lido": "Lido", "dex_other": "Other DEXs",
}
FORK = {"univ4_fork": "Uniswap V4 fork", "univ3_fork": "Uniswap V3 fork", "univ2_fork": "Uniswap V2 fork", "algebra_fork": "Algebra fork",
        "aave_v3_fork": "Aave V3 fork", "aave_v2_fork": "Aave V2 fork", "morpho_blue_fork": "Morpho Blue fork", "univ3_npm": "Uniswap V3 fork",
        "algebra_npm": "Algebra fork"}
# forks whose factory is a project's published deployment address (checked with factory() on a pool of the fork)
KNOWN_FACTORY = {"0x0bfbcf9f": "PancakeSwap V3", "0x02a84c1b": "PancakeSwap V2", "0xaa2cd747": "Ramses V2", "0x1af415a1": "SushiSwap V3"}
VKIND_OF_TYPE = {"swap": "dex", "lp": "dex", "lending": "lending", "borrow": "lending", "liquidation": "lending", "bridge": "bridge",
                 "perp": "perp", "stake": "stake", "vault": "vault", "cex": "exchange", "issuance": "issuer"}
LIVE_ACTIONS = {  # action -> (edge type, direction)
    "bridge_out": ("bridge", 1), "bridge_in": ("bridge", -1), "lp_add": ("lp", 1), "lp_remove": ("lp", -1), "lp_collect": ("lp", -1),
    "stake": ("stake", 1), "unstake": ("stake", -1), "claim": ("stake", -1), "perp_increase": ("perp", 1), "perp_decrease": ("perp", -1),
    "supply": ("lending", 1), "supply_collateral": ("lending", 1), "withdraw": ("lending", -1), "withdraw_collateral": ("lending", -1),
    "borrow": ("borrow", -1), "repay": ("borrow", 1), "liquidated": ("liquidation", -1)}
LEVC_FAMILIES = ("morpho_blue", "aave_v3")   # live.db lending of these families is already in levc
DEX_FAMILIES = {"univ4", "univ3", "univ2", "pancake_v3", "algebra_npm", "univ3_npm", "algebra"}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def live_venue_sql(a="a"):
    """Venue key of a live.db action; the same rule is used for live particles (em_bridge.py flow_particle)."""
    fams = ",".join("'%s'" % f for f in sorted(DEX_FAMILIES))
    return (f"CASE WHEN {a}.protocol IS NOT NULL AND {a}.protocol <> '' THEN {a}.protocol "
            f"WHEN {a}.family IN ({fams}) THEN 'dex_other' ELSE coalesce({a}.family, 'other') END")


def class_sql(cat, wc="wc"):
    """Wallet class from a galaxy category (live.db) with the analytics class table as fallback; matches flows' classes."""
    return (f"CASE WHEN {cat} = 'mev_bot' OR {wc}.class = 'mev' THEN 'mev' "
            f"WHEN {cat} IN ('cex_hot_wallet', 'cex_deposit') OR {wc}.class = 'exchange' THEN 'exchange' "
            f"WHEN {cat} = 'eoa_whale' OR {wc}.class = 'whale_label' THEN 'whale' "
            f"WHEN coalesce({cat}, 'eoa_user') IN ('eoa_user', 'unknown') AND coalesce({wc}.class, 'eoa') IN ('eoa', 'unknown') THEN 'retail' "
            f"ELSE 'contract' END")


FALLBACK = {"univ3_fork": ("V3 DEX", "Unidentified V3-style DEX"), "univ2_fork": ("V2 DEX", "Unidentified V2-style DEX"),
            "univ4_fork": ("V4 DEX", "Unidentified Uniswap V4-style DEX"), "algebra_fork": ("Algebra DEX", "Unidentified Algebra-style DEX"),
            "aave_v3_fork": ("Aave-style lender", "Unidentified Aave V3-style lender"), "aave_v2_fork": ("Aave V2-style lender", "Unidentified Aave V2-style lender"),
            "morpho_blue_fork": ("Morpho-style lender", "Unidentified Morpho Blue-style lender"),
            "compound_v2_fork": ("Compound-style lender", "Unidentified Compound V2-style lender")}


def resolve_new_venues(chain, missing, log=print):
    """New fork venues get identified in the background (venue_ids.py: docs, on-chain names, DefiLlama facts), at most every 6 hours;
    until then they show their descriptive fallback name."""
    if not missing:
        return
    import subprocess
    import sys
    stamp = VENUE_IDS.format(chain=chain) + ".run"
    if os.path.exists(stamp) and time.time() - os.path.getmtime(stamp) < 6 * 3600:
        return
    open(stamp, "w").write(",".join(missing[:50]))
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venue_ids.py")
    subprocess.Popen(["nice", "-n", "15", sys.executable, script, chain], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=open(VENUE_IDS.format(chain=chain) + ".log", "a"), stderr=subprocess.STDOUT)
    log(f"{chain} flowmap: identifying {len(missing)} new venues in the background")


def venue_identities(chain):
    try:
        return json.load(open(VENUE_IDS.format(chain=chain)))
    except (OSError, ValueError):
        return {}


def venue_names(keys_gross, ids=None):
    """{venue key: (display name, hover, source)}. Identities from venue_ids.py (docs / on-chain names) first; an unidentified fork gets
    a stable descriptive name with its contract prefix ('V3 DEX · 0x1f7d…'), never an ordinal."""
    ids = ids or {}
    out = {}
    for key, _ in keys_gross:
        k = (key or "other").lower()
        base, _, suffix = k.partition(":")
        rec = ids.get(key)
        if rec and rec.get("name"):
            src = rec.get("source")
            hover = rec.get("hover") or (rec["name"] + (" · from Uniswap's deployment docs" if src == "docs" else " · named by its own on-chain contracts"
                                                       if src == "onchain" else ""))
            out[key] = (rec["name"], hover, src)
        elif base in FALLBACK and suffix:
            short, long_ = FALLBACK[base]
            out[key] = (f"{short} · {suffix[:6]}…", f"{long_} (contract {suffix}…)", "fallback")
        else:
            out[key] = (_legacy_name(key), None, "registry")
    return out


def _legacy_name(key):
    names, forks = {}, {}
    for key, _ in [(key, 0)]:
        k = (key or "other").lower()
        base, _, suffix = k.partition(":")
        if k in BASE:
            names[key] = BASE[k]
        elif base in BASE and not suffix:
            names[key] = BASE[base]
        elif suffix and suffix[:10] in KNOWN_FACTORY:
            names[key] = KNOWN_FACTORY[suffix[:10]]
        elif base in FORK:
            label = FORK[base]
            n = forks[label] = forks.get(label, 0) + 1
            names[key] = label if n == 1 else f"{label} {n}"
        elif base in BASE:
            names[key] = BASE[base]
        else:
            names[key] = " ".join(w.upper() if re.fullmatch(r"v\d", w) else w.capitalize() for w in re.split(r"[_\s]+", base) if w) or "Other protocol"
    return names[key]


def ensure_exchange_set(c, chain, cd):
    src = LABELS.format(chain=chain)
    dst = f"{cd}/exchange_set.parquet"
    stamp = dst + ".src"
    if not os.path.exists(src):
        return None
    m = str(int(os.path.getmtime(src)))
    if not os.path.exists(dst) or not os.path.exists(stamp) or open(stamp).read() != m:
        c.execute(f"""COPY (SELECT address, max(CASE WHEN category = 'cex_hot_wallet' THEN 1 ELSE 0 END)::UTINYINT AS hot
                            FROM read_parquet('{src}') WHERE category IN ('cex_hot_wallet', 'cex_deposit') AND confidence >= 0.6
                              AND coalesce(state, '') <> 'disputed' GROUP BY address)
                      TO '{dst}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
        os.replace(dst + ".tmp", dst)
        open(stamp, "w").write(m)
    return dst


XFER_PAIRS = """
WITH t AS ({parts}),
tk AS (SELECT t.bn, t.fa, t.ta, t.a, q.token, q.decimals, q.stock FROM t JOIN fm_tokens q ON q.tb = t.token WHERE t.a IS NOT NULL AND t.a > 0),
ad AS (SELECT fa AS b FROM tk UNION SELECT ta FROM tk),
ex AS (SELECT address, hot FROM read_parquet('{exset}') WHERE address IN (SELECT b FROM ad))
SELECT tk.bn, tk.token,
       CASE WHEN ef.address IS NULL AND et.address IS NOT NULL THEN 'cex_in' WHEN ef.address IS NOT NULL AND et.address IS NULL THEN 'cex_out'
            WHEN tk.stock AND tk.fa = from_hex('{zero}') THEN 'mint' WHEN tk.stock AND tk.ta = from_hex('{zero}') THEN 'burn' END AS kind,
       '0x' || lower(hex(CASE WHEN ef.address IS NULL AND et.address IS NOT NULL THEN tk.fa WHEN ef.address IS NOT NULL THEN tk.ta
                              WHEN tk.fa = from_hex('{zero}') THEN tk.ta ELSE tk.fa END)) AS wallet,
       tk.a / 10 ^ tk.decimals AS amount
FROM tk LEFT JOIN ex ef ON ef.address = tk.fa LEFT JOIN ex et ON et.address = tk.ta
WHERE ((ef.address IS NULL) <> (et.address IS NULL)) OR (tk.stock AND (tk.fa = from_hex('{zero}') OR tk.ta = from_hex('{zero}')))
"""
ERC20_PART = """SELECT block_number AS bn, token, "from" AS fa, "to" AS ta, {amt} AS a FROM read_parquet([{files}]) WHERE standard = 'ERC20'"""
NATIVE_PART = """SELECT block_number AS bn, from_hex('{zero}') AS token, "from" AS fa, "to" AS ta, {amt} AS a FROM read_parquet([{files}])
                 WHERE status = 1 AND "to" IS NOT NULL AND value IS NOT NULL"""


def xfer_cache(c, chain, cd, files_tt, files_tx, amt, exset, keep_from_block, log=print):
    """Per follower chunk: exchange deposits/withdrawals (ERC-20 and ETH) and stock mints/burns, cached per chunk so each refresh
    only does new chunks. Chunks older than the 7-day window are removed."""
    by_tag = {}
    for i, files in enumerate((files_tt, files_tx)):
        for f in files:
            by_tag.setdefault(re.search(r"(\d+-\d+)\.parquet$", f).group(1), [None, None])[i] = f
    for f in __import__("glob").glob(f"{cd}/fx_*.parquet"):
        b = int(re.search(r"-(\d+)\.parquet$", f).group(1))
        if b < keep_from_block:
            os.remove(f)
    todo = [(tag, tt, tx) for tag, (tt, tx) in sorted(by_tag.items())
            if int(tag.split("-")[1]) >= keep_from_block and not os.path.exists(f"{cd}/fx_{tag}.parquet")]
    for i in range(0, len(todo), 40):   # one query per 40 chunks: the exchange set is scanned once per batch, probed by their addresses
        batch = todo[i:i + 40]
        tts, txs = [x[1] for x in batch if x[1]], [x[2] for x in batch if x[2]]
        parts = []
        if tts:
            parts.append(ERC20_PART.format(amt=amt.format(c="amount"), files=",".join("'%s'" % f for f in tts)))
        if txs:
            parts.append(NATIVE_PART.format(amt=amt.format(c="value"), files=",".join("'%s'" % f for f in txs), zero=ZERO[2:]))
        if not parts:
            continue
        c.execute("CREATE OR REPLACE TEMP TABLE fx_batch AS " + XFER_PAIRS.format(parts=" UNION ALL ".join(parts), exset=exset, zero=ZERO[2:]))
        for tag, tt, tx in batch:
            a, b = map(int, tag.split("-"))
            dst = f"{cd}/fx_{tag}.parquet"
            c.execute(f"COPY (SELECT * FROM fx_batch WHERE bn BETWEEN {a} AND {b}) TO '{dst}.tmp' (FORMAT parquet)")
            os.replace(dst + ".tmp", dst)
    return len(todo)


def pool_protocols(c, dims):
    """Retain trades when a chain has no pool registry; expose missing coverage."""
    import sqlite3
    db = sqlite3.connect("file:" + dims + "?mode=ro", uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    finally:
        db.close()
    missing = [name for name in ("pools", "v4pools") if name not in tables]
    joins, values = [], []
    if "pools" in tables:
        joins.append("LEFT JOIN fmd.pools d ON d.pool = p.pool")
        values.append("d.protocol")
    if "v4pools" in tables:
        joins.append("LEFT JOIN fmd.v4pools v ON v.pool_id = p.pool")
        values.append("v.protocol")
    value = "coalesce(" + ",".join(values) + ")" if len(values) > 1 else (values[0] if values else "NULL::VARCHAR")
    c.execute(f"ATTACH '{dims}' AS fmd (TYPE sqlite, READ_ONLY)")
    try:
        c.execute("CREATE OR REPLACE TEMP TABLE fm_pools AS WITH p AS (SELECT DISTINCT pool FROM trc) "
                  + "SELECT p.pool, any_value(" + value + ") AS protocol FROM p " + " ".join(joins) + " GROUP BY p.pool")
    finally:
        c.execute("DETACH fmd")
    return {"available": bool(values), "complete": not missing, "missingTables": missing}


def build(c, chain, now, out, res, log=print, rt_files=None, tail_files=None, amt=None, snap=None):
    """Called by analytics.refresh with its DuckDB connection (tables trc, levc, cls, whales, px, mk, bm already built)."""
    t0 = time.time()
    lo7 = now - WIN["7d"]
    cd = f"{out}/rt"
    os.makedirs(cd, exist_ok=True)
    c.execute("INSTALL sqlite; LOAD sqlite")
    # pool -> protocol for the pools traded in the window (v2/v3-style pools and Uniswap V4 pool ids)
    dims = DIMS.format(chain=chain)
    protocol_coverage = pool_protocols(c, dims)
    # tokens valued here: served markets with a price, plus USD anchors and native ETH at the WETH price
    c.execute("""CREATE OR REPLACE TEMP TABLE fm_tokens AS
      SELECT from_hex(substr(p.token, 3)) AS tb, p.token, coalesce(mk.decimals, 18) AS decimals, p.price, coalesce(p.kind = 'stock', false) AS stock,
             coalesce(p.symbol, '') AS symbol
      FROM px p LEFT JOIN mk ON mk.token = p.token WHERE p.price IS NOT NULL""")
    weth = c.execute("""SELECT price FROM px WHERE upper(symbol) = 'WETH' AND price IS NOT NULL ORDER BY price DESC LIMIT 1""").fetchone()
    if weth and not c.execute(f"SELECT 1 FROM fm_tokens WHERE token = '{ZERO}'").fetchone():
        c.execute(f"INSERT INTO fm_tokens VALUES (from_hex('{ZERO[2:]}'), '{ZERO}', 18, {float(weth[0])}, false, 'ETH')")
    # exchange deposits / withdrawals and stock mints / burns from the follower chunks (and the Blob-gap tail)
    xf_note = None
    exset = ensure_exchange_set(c, chain, cd)
    if exset and rt_files and amt:
        lob = c.execute(f"SELECT coalesce(max(first_block), 0) FROM bm WHERE minute <= {lo7}").fetchone()[0]
        tt = [f for f in (tail_files(chain, "token_transfers", lob) + rt_files(chain, "token_transfers", lob))]
        tx = [f for f in (tail_files(chain, "transactions", lob) + rt_files(chain, "transactions", lob))]
        n = xfer_cache(c, chain, cd, tt, tx, amt, exset, lob, log)
        if n:
            log(f"{chain} flowmap transfer chunks cached {n}")
    fx = [f for f in __import__("glob").glob(f"{cd}/fx_*.parquet")]
    # one table of money movements: (source, time, wallet, class, venue key, type, dir, usd, weight, tx, token)
    c.execute("""CREATE OR REPLACE TEMP TABLE fe(src VARCHAR, time BIGINT, wallet VARCHAR, cls VARCHAR, venue VARCHAR, type VARCHAR, dir TINYINT,
                 usd DOUBLE, w DOUBLE, tx VARCHAR, token VARCHAR)""")
    c.execute(f"""INSERT INTO fe SELECT 'dex', t.time, t.wallet, t.wclass, coalesce(p.protocol, 'dex_other'), 'swap',
                         CASE WHEN t.side = 'B' THEN 1 ELSE -1 END, t.usd, 1.0 / count(*) OVER (PARTITION BY t.tx, t.log_index), t.tx, t.token
                  FROM trc t LEFT JOIN fm_pools p ON p.pool = t.pool WHERE t.time >= {lo7} AND t.usd > 0""")
    c.execute(f"""INSERT INTO fe SELECT 'lend', l.time, l.wallet, l.wclass, l.protocol,
                         CASE WHEN l.kind IN ('borrow', 'repay') THEN 'borrow' WHEN l.kind = 'liquidate' THEN 'liquidation' ELSE 'lending' END,
                         CASE WHEN l.kind IN ('supply_collateral', 'supply', 'repay') THEN 1 ELSE -1 END, l.usd, 1.0, NULL, l.asset
                  FROM levc l WHERE l.time >= {lo7} AND l.usd > 0 AND l.protocol IS NOT NULL""")
    live_from = None
    if os.path.exists(LIVE_DB):
        c.execute(f"ATTACH '{LIVE_DB}' AS fmlv (TYPE sqlite, READ_ONLY)")
        typ = "CASE a.action " + " ".join(f"WHEN '{k}' THEN '{v[0]}'" for k, v in LIVE_ACTIONS.items()) + " END"
        dr = "CASE a.action " + " ".join(f"WHEN '{k}' THEN {v[1]}" for k, v in LIVE_ACTIONS.items()) + " END"
        acts = ",".join("'%s'" % k for k in LIVE_ACTIONS)
        fam = ",".join("'%s'" % f for f in LEVC_FAMILIES)
        c.execute(f"""INSERT INTO fe SELECT 'live', a.time, lower(a.wallet), {class_sql('a.wallet_category')}, {live_venue_sql('a')},
                             CASE WHEN a.family = 'erc4626' THEN 'vault' ELSE {typ} END, {dr}, a.usd, 1.0,
                             lower(a.tx_hash), lower(a.asset)
                      FROM fmlv.actions a LEFT JOIN cls wc ON wc.address = lower(a.wallet)
                      WHERE a.chain = '{chain}' AND a.time >= {lo7} AND a.usd > 0 AND a.action IN ({acts})
                        AND NOT (a.family IN ({fam}) AND a.action IN ('supply', 'supply_collateral', 'withdraw', 'withdraw_collateral', 'borrow',
                                                                    'repay', 'liquidated'))""")
        live_from = c.execute(f"SELECT min(time) FROM fmlv.actions WHERE chain = '{chain}'").fetchone()[0]
        c.execute("DETACH fmlv")
    if fx:
        c.execute(f"""INSERT INTO fe
          WITH x AS (SELECT x.*, b.minute AS time FROM read_parquet([{','.join("'%s'" % f for f in fx)}]) x ASOF JOIN bm b ON x.bn >= b.first_block)
          SELECT 'xfer', x.time, x.wallet, {class_sql('NULL')}, CASE WHEN x.kind IN ('cex_in', 'cex_out') THEN '@exchange' ELSE '@issuer' END,
                 CASE WHEN x.kind IN ('cex_in', 'cex_out') THEN 'cex' ELSE 'issuance' END,
                 CASE WHEN x.kind IN ('cex_in', 'burn') THEN 1 ELSE -1 END, x.amount * q.price, 1.0, NULL, q.token
          FROM x JOIN fm_tokens q ON q.token = x.token LEFT JOIN cls wc ON wc.address = x.wallet
          WHERE x.time >= {lo7} AND x.kind IS NOT NULL AND x.amount * q.price > 0""")
    c.execute("DELETE FROM fe WHERE usd IS NULL OR usd > 1e10 OR isnan(usd)")   # unpriced or absurd prints never reach the map
    # venue display names (forks numbered by 7d volume) -> node ids; small venues fold into "Other DEXs" / "Other protocols"
    keys = c.execute("""SELECT venue, sum(usd * w), any_value(type) FROM fe WHERE venue NOT LIKE '@%' GROUP BY venue""").fetchall()
    ids = venue_identities(chain)
    resolve_new_venues(chain, [k for k, _, _ in keys if ":" in (k or "") and k.split(":")[0] in FALLBACK and k not in ids], log)
    named = venue_names([(k, g) for k, g, _ in keys], ids)
    vkind = dict(c.execute("""SELECT venue, arg_max(type, s) FROM (SELECT venue, type, sum(usd * w) AS s FROM fe GROUP BY ALL) GROUP BY venue""").fetchall())
    vmap = {k: {"node": "v:" + slug(named[k][0]), "name": named[k][0], "hover": named[k][1], "source": named[k][2],
                "kind": VKIND_OF_TYPE.get(vkind.get(k), "other")} for k, _, _ in keys}
    vmap["dex_other"] = {"node": "v:other-dexs", "name": "Other DEXs", "kind": "dex"}
    c.execute("CREATE OR REPLACE TEMP TABLE fm_vmap(venue VARCHAR, node VARCHAR, name VARCHAR, kind VARCHAR)")
    if vmap:
        c.executemany("INSERT INTO fm_vmap VALUES (?, ?, ?, ?)", [(k, v["node"], v["name"], v["kind"]) for k, v in vmap.items()])
    c.execute("INSERT INTO fm_vmap VALUES ('@exchange', 'c:exchange', 'Exchanges', 'exchange'), ('@issuer', 'i:issuer', 'Stock token mint & redeem', 'issuer')")
    sym = {t: s for t, s in c.execute("SELECT token, symbol FROM fm_tokens").fetchall() if s and not s.lower().startswith("0x")}
    sym[ZERO] = "ETH"
    hot_wallets = c.execute(f"SELECT count(*) FROM read_parquet('{exset}') WHERE hot = 1").fetchone()[0] if exset else None
    results = {}
    for wname, wsec in WIN.items():
        lo = now - wsec
        results[wname] = window_json(c, chain, wname, lo, now, vmap, sym, live_from, snap, hot_wallets)
    # reconciliation with the per-market flows written by this same refresh (DEX classes and lending)
    for wname, js in results.items():
        js["reconciliation"] = reconcile(c, res, wname, now - WIN[wname])
        js["coverage"]["protocolDimensions"] = protocol_coverage
        if protocol_coverage["missingTables"]:
            js["coverage"]["notes"].append("Missing protocol dimensions remain unidentified DEX venues; trade amounts are retained.")
    res.executescript("""CREATE TABLE IF NOT EXISTS flowmap(window TEXT PRIMARY KEY, json TEXT, updated_at INTEGER);
                         CREATE TABLE IF NOT EXISTS flow_pools(pool TEXT PRIMARY KEY, venue TEXT);""")
    res.execute("BEGIN")
    res.execute("DELETE FROM flowmap")
    res.executemany("INSERT INTO flowmap VALUES (?, ?, ?)", [(w, json.dumps(js, separators=(",", ":")), int(time.time())) for w, js in results.items()]
                    + [("venues", json.dumps({k: {**v, "node": f"{chain}:{v['node']}"} for k, v in vmap.items()}, separators=(",", ":")), int(time.time()))])
    res.execute("DELETE FROM flow_pools")
    res.executemany("INSERT INTO flow_pools VALUES (?, ?)", c.execute("SELECT pool, coalesce(protocol, 'dex_other') FROM fm_pools").fetchall())
    res.execute("COMMIT")
    try:
        write_holders(c, chain, out)
    except Exception as e:   # the flow map never fails because of the holder table
        log(f"{chain} holders_top failed: {e!r}")
    try:   # protocol directory: directory_core.py, reloaded here so its edits apply without a restart
        import importlib
        import directory_core
        importlib.reload(directory_core)
        named_src = lambda ks: {k: {"source": v[2]} for k, v in venue_names([(k, 0) for k in ks], ids).items()}
        directory_core.build(c, chain, now, out, res, ids, lambda ks: venue_names([(k, 0) for k in ks], ids),
                             lambda e: entity_of(chain, {"kind": "venue", "id": "v:" + e["id"], "name": e["name"], "keys": e["keys"]}, named_src(e["keys"])),
                             log)
    except Exception as e:   # nor because of the directory
        if res.in_transaction:
            res.rollback()
        log(f"{chain} directory failed: {e!r}")
    log(f"{chain} flowmap {round(time.time() - t0, 1)}s nodes {[len(results[w]['nodes']) for w in WIN]} edges {[len(results[w]['edges']) for w in WIN]}")
    return results


def window_json(c, chain, wname, lo, now, vmap, sym, live_from, snap, hot_wallets=None):
    c.execute(f"""CREATE OR REPLACE TEMP TABLE fw AS
      SELECT f.*, 'c:' || f.cls AS cnode, m.node AS vnode, m.name AS vname, m.kind AS vkind, f.usd * f.w AS du
      FROM fe f JOIN fm_vmap m ON m.venue = f.venue WHERE f.time >= {lo}""")
    # fold small venues (never classes, exchanges or the issuer) into "Other DEXs" / "Other protocols"
    vtot = c.execute("SELECT sum(du) FROM fw WHERE vnode LIKE 'v:%'").fetchone()[0] or 0.0
    keep = {r[0] for r in c.execute(f"""SELECT vnode FROM fw WHERE vnode LIKE 'v:%' GROUP BY vnode HAVING sum(du) >= {vtot * MIN_VENUE_SHARE}
                                          ORDER BY sum(du) DESC LIMIT {MAX_VENUES}""").fetchall()}
    keep.add("v:other-dexs")
    c.execute("CREATE OR REPLACE TEMP TABLE fw_keep(node VARCHAR)")
    c.executemany("INSERT INTO fw_keep VALUES (?)", [(k,) for k in keep])
    c.execute("""UPDATE fw SET vnode = CASE WHEN vkind = 'dex' THEN 'v:other-dexs' ELSE 'v:other-protocols' END,
                              vname = CASE WHEN vkind = 'dex' THEN 'Other DEXs' ELSE 'Other protocols' END
                 WHERE vnode LIKE 'v:%' AND vnode NOT IN (SELECT node FROM fw_keep)""")
    edges = c.execute("""SELECT cnode, vnode, type, sum(du) FILTER (WHERE dir > 0), sum(du) FILTER (WHERE dir < 0), sum(du), sum(w),
                                count(DISTINCT wallet) FROM fw WHERE cnode <> vnode GROUP BY ALL HAVING sum(du) >= 1 ORDER BY 6 DESC""").fetchall()
    vnodes = c.execute("""SELECT vnode, any_value(vname), arg_max(vkind, du), sum(du) FILTER (WHERE dir > 0), sum(du) FILTER (WHERE dir < 0), sum(du),
                                 sum(w), count(DISTINCT wallet), list(DISTINCT venue) FROM fw WHERE vnode NOT LIKE 'c:%' GROUP BY vnode""").fetchall()
    cnodes = c.execute("""SELECT cnode, sum(du) FILTER (WHERE dir > 0), sum(du) FILTER (WHERE dir < 0), sum(du), sum(w), count(DISTINCT wallet)
                          FROM fw GROUP BY cnode""").fetchall()
    exch = c.execute("""SELECT sum(du) FILTER (WHERE dir > 0), sum(du) FILTER (WHERE dir < 0), sum(du), sum(w), count(DISTINCT wallet)
                        FROM fw WHERE vnode = 'c:exchange'""").fetchone()
    topw = {}
    for node, wallet, cls, g, net in c.execute("""
        SELECT node, wallet, cls, g, net FROM (
          SELECT node, wallet, any_value(cls) AS cls, sum(du) AS g, sum(du * dir) AS net,
                 row_number() OVER (PARTITION BY node ORDER BY sum(du) DESC) AS rk
          FROM (SELECT vnode AS node, wallet, cls, du, dir FROM fw UNION ALL SELECT cnode, wallet, cls, du, dir FROM fw WHERE vnode <> cnode)
          WHERE wallet IS NOT NULL GROUP BY node, wallet) WHERE rk <= 8 ORDER BY node, rk""").fetchall():
        topw.setdefault(node, []).append({"address": wallet, "class": cls, "usd": round(g, 2), "netUsd": round(net, 2)})
    topt = {}
    for node, token, g in c.execute("""
        SELECT node, token, g FROM (
          SELECT node, token, sum(du) AS g, row_number() OVER (PARTITION BY node ORDER BY sum(du) DESC) AS rk
          FROM (SELECT vnode AS node, token, du FROM fw UNION ALL SELECT cnode, token, du FROM fw WHERE vnode <> cnode)
          WHERE token IS NOT NULL GROUP BY node, token) WHERE rk <= 5 ORDER BY node, rk""").fetchall():
        if sym.get(token):
            topt.setdefault(node, []).append({"token": token, "symbol": sym[token], "usd": round(g, 2)})
    r2 = lambda v: round(v or 0.0, 2)
    nodes = []
    for node, fwd, back, g, ev, wallets in cnodes:
        key = node[2:]
        if node == "c:exchange" and exch:   # exchanges are also a venue (deposits / withdrawals)
            fwd, back, g, ev, wallets = (fwd or 0) + (exch[1] or 0), (back or 0) + (exch[0] or 0), (g or 0) + (exch[2] or 0), (ev or 0) + (exch[3] or 0), wallets
        if node == "c:exchange":
            wallets = None
        nodes.append({"id": node, "kind": "class", "group": key, "name": CLASS_NAME.get(key, key.title()), "hover": CLASS_HINT.get(key), "wallets": wallets,
                      **({"labelledWallets": hot_wallets} if node == "c:exchange" else {}),
                      "outUsd": r2(fwd), "inUsd": r2(back), "usd": r2(g), "events": round(ev or 0), "top": topw.get(node, []), "tokens": topt.get(node, [])})
    if exch and not any(n["id"] == "c:exchange" for n in nodes):
        nodes.append({"id": "c:exchange", "kind": "class", "group": "exchange", "name": "Exchanges", "hover": CLASS_HINT["exchange"], "wallets": None,
                      "labelledWallets": hot_wallets, "outUsd": r2(exch[1]),
                      "inUsd": r2(exch[0]), "usd": r2(exch[2]), "events": round(exch[3] or 0), "top": topw.get("c:exchange", []),
                      "tokens": topt.get("c:exchange", [])})
    bridges = []
    for node, name, kind, fwd, back, g, ev, wallets, venues in vnodes:
        if node == "c:exchange":
            continue
        ident = next((vmap[v] for v in venues if v in vmap and vmap[v].get("hover")), None)
        n = {"id": node, "kind": "issuer" if node == "i:issuer" else "venue", "group": kind, "name": name, "wallets": wallets,
             **({"hover": ident["hover"], "identity": ident["source"]} if ident else {}),
             "inUsd": r2(fwd), "outUsd": r2(back), "usd": r2(g), "events": round(ev or 0), "keys": sorted(v for v in venues if v and v[0] != "@")[:12],
             "top": topw.get(node, []), "tokens": topt.get(node, [])}
        nodes.append(n)
        if kind == "bridge":
            bridges.append(n)
    out_edges = [{"id": f"{s}>{t}>{ty}", "source": s, "target": t, "type": ty, "fwdUsd": r2(f), "backUsd": r2(b), "usd": r2(g),
                  "netUsd": r2((f or 0) - (b or 0)), "events": round(ev or 0), "wallets": wl} for s, t, ty, f, b, g, ev, wl in edges]
    if bridges:   # money leaving / arriving through bridges continues to other chains (a pass-through, not counted in totals)
        nodes.append({"id": "x:chains", "kind": "external", "group": "external", "name": "Other chains", "wallets": None,
                      "inUsd": r2(sum(b["inUsd"] for b in bridges)), "outUsd": r2(sum(b["outUsd"] for b in bridges)),
                      "usd": r2(sum(b["usd"] for b in bridges)), "events": sum(b["events"] for b in bridges), "top": [], "tokens": []})
        for b in bridges:
            out_edges.append({"id": f"{b['id']}>x:chains>bridge", "source": b["id"], "target": "x:chains", "type": "bridge", "passThrough": True,
                              "fwdUsd": b["inUsd"], "backUsd": b["outUsd"], "usd": b["usd"], "netUsd": r2(b["inUsd"] - b["outUsd"]),
                              "events": b["events"], "wallets": b["wallets"]})
    by_type = dict(c.execute("SELECT type, sum(du) FROM fw GROUP BY type").fetchall())
    tot = c.execute("SELECT sum(du), sum(w), count(DISTINCT wallet) FROM fw").fetchone()
    snap_time = c.execute(f"SELECT max(minute) FROM bm WHERE first_block <= {snap}").fetchone()[0] if snap else None
    xfer_from = c.execute("SELECT min(time) FROM fe WHERE src = 'xfer'").fetchone()[0]
    venues_alias = {k: v["node"] for k, v in vmap.items()}
    kept = {n["id"] for n in nodes}
    for k, node in venues_alias.items():
        if node not in kept and node.startswith("v:"):
            venues_alias[k] = "v:other-dexs" if vmap[k]["kind"] == "dex" else "v:other-protocols"
    q = lambda i: i if i.startswith(chain + ":") else f"{chain}:{i}"
    for n in nodes:
        n["entity_id"] = entity_of(chain, n, vmap)
        n["id"], n["chain"] = q(n["id"]), chain
    for e in out_edges:
        e["source"], e["target"] = q(e["source"]), q(e["target"])
        e["id"] = f"{e['source']}>{e['target']}>{e['type']}"
        e["src_chain"], e["dst_chain"] = chain, ("external" if e.get("passThrough") else chain)
    venues_alias = {k: q(v) for k, v in venues_alias.items()}
    return {"schema": "flowmap/2", "chain": chain, "chains": [chain], "window": wname, "from": lo, "to": now, "asOf": now, "updatedAt": int(time.time()),
            "nodes": sorted(nodes, key=lambda n: -(n["usd"] or 0)), "edges": out_edges,
            "totals": {"usd": r2(tot[0]), "events": round(tot[1] or 0), "wallets": tot[2], "byType": {k: r2(v) for k, v in by_type.items()}},
            "alias": {"venues": venues_alias,
                      "classes": {k: q(v) for k, v in {"mev_bot": "c:mev", "cex_hot_wallet": "c:exchange", "cex_deposit": "c:exchange",
                                  "eoa_whale": "c:whale", "eoa_user": "c:retail", "unknown": "c:retail", "": "c:retail"}.items()},
                      "contractDefault": q("c:contract")},
            "coverage": {"notes": [
                "swaps: every DEX trade of the served tokens (deep tape + hot fills), wallet = transaction sender; token-to-token swaps counted once",
                "lending: Morpho Blue and Aave v3 (and forks) events; other lenders, vaults, bridges, LP, perps and staking: decoded live actions",
                "exchanges: ERC-20 and ETH transfers into exchange deposit / hot wallets and out of them (internal sweeps excluded), "
                "exchanges are unnamed (one node) until exchange entities are labelled",
                "classes: whales and retail as in the token flows (whales are per token); contract wallets are Safes, routers, aggregators and other "
                "contracts moving money for themselves, separate from the protocol venues (DEXs, lenders, ERC-4626 vaults, bridges)",
                "venue names: Uniswap's deployment docs, or the venue's own on-chain contract names (position NFT, LP token, aToken); "
                "unidentified venues are named by type and contract prefix"],
                "liveFrom": live_from, "liveComplete": bool(live_from) and live_from <= lo,
                "transfersFrom": xfer_from, "transfersComplete": bool(xfer_from) and xfer_from <= lo + 3600,
                "snapshotTime": snap_time}}


HOLDER_ROLE = {"dex": "venue", "lending": "venue", "perp": "venue", "vault": "venue", "bridge": "venue", "exchange": "exchange", "issuer": "issuer"}


def write_holders(c, chain, out, n=100):
    """Top holders of every served market (same balances as the token state), for the holder-cluster API in the wallet bridge.
    Plain rollback-journal SQLite, replaced atomically, so the bridge can read it without write access."""
    import sqlite3
    if os.path.exists(f"{out}/holders_top.sqlite3") and time.time() - os.path.getmtime(f"{out}/holders_top.sqlite3") < 1800:
        return
    rows = c.execute(f"""SELECT p.market, b.rk, b.holder, b.amount, b.amount / nullif(s.supply, 0), b.usd, b.class, b.bucket, b.entity_name, s.supply, s.holders
                         FROM (SELECT *, row_number() OVER (PARTITION BY token ORDER BY amount DESC) AS rk FROM balb
                               WHERE bucket <> 'burned' AND class <> 'burn' AND amount > 0) b
                         JOIN supply s ON s.token = b.token JOIN px p ON p.token = b.token
                         WHERE b.rk <= {n} AND p.market IS NOT NULL""").fetchall()
    dst = f"{out}/holders_top.sqlite3"
    if os.path.exists(dst) and time.time() - os.path.getmtime(dst) < 1800:
        return   # holder ranks move slowly; every 30 minutes is enough
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("""PRAGMA journal_mode=DELETE;
      CREATE TABLE holders(market TEXT, rank INTEGER, holder TEXT, amount REAL, pct REAL, usd REAL, class TEXT, bucket TEXT, entity TEXT,
                           role TEXT, PRIMARY KEY(market, rank));
      CREATE TABLE supply(market TEXT PRIMARY KEY, supply REAL, holders INTEGER);
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);""")
    db.executemany("INSERT INTO holders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [(m, rk, h, a, pct, usd, cl, sub, ent, HOLDER_ROLE.get(cl, "wallet")) for m, rk, h, a, pct, usd, cl, sub, ent, _, _ in rows])
    db.executemany("INSERT OR REPLACE INTO supply VALUES (?,?,?)", {(r[0], r[9], r[10]) for r in rows})
    db.executemany("INSERT INTO meta VALUES (?,?)", [("updated_at", str(int(time.time()))), ("chain", chain), ("top", str(n))])
    db.commit()
    db.close()
    os.chmod(tmp, 0o644)
    os.replace(tmp, dst)


def entity_of(chain, n, vmap):
    """Chain-agnostic identity: the same named protocol, exchange or address on two chains is one entity."""
    if n["kind"] == "class":
        return "exchange:unlabelled" if n["group"] == "exchange" else "class:" + n["group"]
    if n["kind"] == "external":
        return "external:other-chains"
    if n["kind"] == "issuer":
        return "issuer:stock-tokens"
    keys = n.get("keys") or []
    if n["id"] in ("v:other-dexs", "v:other-protocols") or keys == ["erc4626"] or keys == ["dex_other"]:
        return f"aggregate:{chain}:{n['id'][2:]}"   # groupings, not one protocol
    srcs = {vmap[k].get("source") for k in keys if k in vmap}
    if srcs and srcs <= {"fallback"}:
        return f"venue:{chain}:{keys[0]}"
    return "protocol:" + slug(n["name"])


def reconcile(c, res, wname, lo):
    """DEX: token-side USD of trc in this window for the markets in the flows table vs the sum of their class buy+sell (same refresh)."""
    try:
        rows = res.execute("SELECT market, json FROM flows WHERE window = ?", (wname,)).fetchall()
    except Exception:
        return None
    toks, fsum, lsum = [], 0.0, 0.0
    for market, js in rows:
        o = json.loads(js)
        toks.append(market.split(":", 1)[1])
        fsum += sum((x.get("buyUsd") or 0) + (x.get("sellUsd") or 0) for x in o.get("classes") or [])
        lsum += sum((o["lending"].get(k) or 0) for k in ("supplyUsd", "withdrawUsd", "borrowUsd", "repayUsd", "liquidatedUsd"))
    c.execute("CREATE OR REPLACE TEMP TABLE fm_rt(token VARCHAR)")
    c.executemany("INSERT INTO fm_rt VALUES (?)", [(t,) for t in toks])
    dex = c.execute(f"SELECT sum(usd) FROM trc WHERE time >= {lo} AND token IN (SELECT token FROM fm_rt)").fetchone()[0] or 0.0
    lend = c.execute(f"""SELECT sum(usd) FROM levc WHERE time >= {lo} AND (kind NOT IN ('borrow', 'repay') OR collateral_token IS NOT NULL)
                         AND (CASE WHEN kind IN ('borrow', 'repay') THEN collateral_token ELSE asset END) IN (SELECT token FROM fm_rt)""").fetchone()[0] or 0.0
    shown = c.execute(f"SELECT sum(usd) FROM fe WHERE src = 'dex' AND time >= {lo}").fetchone()[0] or 0.0
    pct = lambda a, b: round(100 * abs(a - b) / b, 4) if b else None
    return {"dexTokenSideUsd": round(dex, 2), "flowsClassesUsd": round(fsum, 2), "dexDiffPct": pct(dex, fsum),
            "lendingUsd": round(lend, 2), "flowsLendingUsd": round(lsum, 2), "lendingDiffPct": pct(lend, lsum),
            "mapSwapTokenSideUsd": round(shown, 2), "markets": len(rows)}
