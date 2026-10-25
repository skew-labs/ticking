#!/usr/bin/env python3
"""Top traders / smart money for Arbitrum One and Robinhood Chain, from our own swap data only (no LLM).

Inputs (all read-only):
  trades     em-charts wallet index data/<chain>/wallet/trades_b=<n>.parquet: every priced swap on a served market token
             (one side an anchor: USD stable / ETH / BTC / ARB), attributed to tx.from, through info.json trades_last_block;
             plus em-data/traders/<chain>/log/recent_trades.parquet: hot-store fills after the index, kept here because
             the hot store prunes after 3 days.
  prices     em-charts candles.duckdb: anchor_px (ETH/BTC/ARB USD per minute) for quote conversion, hourly token bars
             (outlier-cleaned reference) for the bad-print band, markets (kind, first trade, last price).
  balances   em-charts analytics holders_snap + xfer_gap + rt/xfer_* (balance now), positions_snap (lending collateral).
  labels     em-graphloop/export/<chain>/labels.parquet (main + delta layers): protocol, bot and contract senders excluded.
  econ v3    sheet_summary.v3 (debt now), econ_actions (borrow / liquidation history).

Method (per wallet x token, average cost; details in README.md):
  - realized PnL: each sell realizes against the average cost of swap-bought inventory. Units sold beyond that inventory
    came from transfers in (or unpriced swaps) and realize nothing (valued at the sell price): xin_q / xin_usd.
  - unrealized PnL: (price now - average cost) on min(swap inventory, balance now + lending collateral). Inventory above
    the balance left by transfer out (CEX, other wallets, LP) and is closed at cost (no PnL).
    transfer_adjusted = transfers explain >10% of sells or of bought inventory.
  - windows 7d / 30d: realized on sells inside the window + the share of unrealized PnL bought inside the window.
  - wash: a wallet's buy and sell of the same token in the same block are dropped (both legs).
  - bad prints: a fill priced outside the token's hourly reference [low/f, high*f] (f = 1.25 stock, 1.10 anchor,
    1.5 token; same hour, or within 6 h for stocks / anchors) is revalued at the hourly close.

Outputs (each file tmp + rename): em-data/traders/<chain>/{wallet_token_pnl,wallet_pnl,token_leaderboard,smart_money,
  trader_labels}.parquet + info.json; em-graphloop/export/<chain>/labels_traders.parquet (layer='traders', one row per
  wallet x label; kept out of labels.parquet, whose consumers expect one row per address).
usage: traders.py <chain> [--nibbles 0123456789abcdef] [--keep-work]
"""
import argparse
import ctypes
import fcntl
import glob
import json
import os
import shutil
import sqlite3
import sys
import time

import duckdb
import numpy as np
import pyarrow as pa

HERE = os.path.dirname(os.path.abspath(__file__))
EM = "/srv/skew/em-data"
CHARTS = "/srv/skew/em-charts"
GL = "/srv/skew/em-graphloop"
WORKROOT = "/srv/skew/em-traders/work"
FLOOR_GB = 60

# senders that are not traders: protocol contracts, routers, pools, bots, exchanges' hot wallets
EXCLUDE = ("mev_bot", "dex_router", "dex_pool", "cex_hot_wallet", "bridge", "lending", "token_contract", "nft_contract",
           "contract_other", "multisig_or_safe")
QK = {"usd": 0, "eth": 1, "btc": 2, "arb": 3}
BAND = {"stock": 1.25, "anchor": 1.10, "token": 1.5}
DAY = 86400

# leaderboard gates per window: (min USD volume in the window, min trades in the window)
WINDOW_GATE = {"all": (10_000, 5), "30d": (5_000, 3), "7d": (2_000, 2)}
# a token gets a leaderboard when its eligible traders moved at least this much (USD, window volume) and there are enough of them
TOKEN_FLOOR = {"stock": 25_000, "anchor": 250_000, "token": 100_000}
TOKEN_MIN_TRADERS = 5
LEADERBOARD_KEEP = 100
TOP_LABEL = 20
# bot-like senders that escaped the graph labels (behaviour over the whole history)
BOT_TRADES, BOT_HOLD_S, BOT_PER_DAY, BOT_WASH_SHARE, BOT_TOKENS = 2_000, 300, 300, 0.2, 300
GLITCH_TOKEN_SHARE = 0.10
MAX_FILL_USD = 20e6  # em-charts treats a larger average swap as a bogus print
MARK_SHARE, MARK_FLOOR_USD = 0.25, 1000  # marked-to-market held value <= 25% of the token's 7-day turnover (min $1k)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def free_gb():
    s = os.statvfs("/")
    return s.f_bavail * s.f_frsize / 1e9


def connect(path=":memory:"):
    c = duckdb.connect(path)
    c.execute(f"SET threads={int(os.environ.get('TR_THREADS', 4))}")
    c.execute(f"SET memory_limit='{os.environ.get('TR_MEM', '6GB')}'")
    tmp = f"{WORKROOT}/tmp/{os.getpid()}"
    os.makedirs(tmp, exist_ok=True)
    c.execute(f"SET temp_directory='{tmp}'")
    c.execute("SET preserve_insertion_order=false")
    c.execute("SET enable_progress_bar=false")
    return c


def pq(p):
    return "read_parquet('%s')" % p


def write_parquet(c, sql, dst, row_group=None):
    tmp = dst + ".tmp"
    rg = f", ROW_GROUP_SIZE {row_group}" if row_group else ""
    c.execute(f"COPY ({sql}) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd{rg})")
    os.replace(tmp, dst)


W3 = ("('0x' || substr({h}, 1, 16))::UBIGINT AS w1, ('0x' || substr({h}, 17, 16))::UBIGINT AS w2, "
      "('0x' || substr({h}, 33, 8))::UINTEGER AS w3")
HEX = "lower(printf('0x%016x%016x%08x', w1, w2, w3))"


# ---------------------------------------------------------------- kernel
class Out(ctypes.Structure):
    I = ("nb", "ns", "n30", "n7", "trips", "wins", "first_buy_t", "first_t", "last_t", "flips", "open_t")
    D = ("bq", "sq", "bu", "su", "real_all", "real30", "real7", "bu30", "su30", "bu7", "su7", "bq30", "bq7",
         "qty_end", "cost_end", "xin_q", "xin_u", "hold_sum", "first_buy_px", "max_gain", "win_sum", "loss_sum")
    _fields_ = [(k, ctypes.POINTER(ctypes.c_int64)) for k in I] + [(k, ctypes.POINTER(ctypes.c_double)) for k in D]


LIB = ctypes.CDLL(f"{HERE}/libpnl.so")
LIB.ledger.restype = ctypes.c_int64
LIB.ledger.argtypes = [ctypes.c_int64] + [ctypes.c_void_p] * 6 + [ctypes.c_int64] * 3 + [ctypes.POINTER(Out)]


def run_kernel(gid, t, bn, buy, q, u, t30, t7, G):
    arrs = {k: np.zeros(G, dtype=np.int64) for k in Out.I}
    arrs.update({k: np.zeros(G, dtype=np.float64) for k in Out.D})
    o = Out(*[arrs[k].ctypes.data_as(ctypes.POINTER(ctypes.c_int64)) for k in Out.I],
            *[arrs[k].ctypes.data_as(ctypes.POINTER(ctypes.c_double)) for k in Out.D])
    cols = [np.ascontiguousarray(gid, dtype=np.int64), np.ascontiguousarray(t, dtype=np.int64),
            np.ascontiguousarray(bn, dtype=np.int64), np.ascontiguousarray(buy, dtype=np.uint8),
            np.ascontiguousarray(q, dtype=np.float64), np.ascontiguousarray(u, dtype=np.float64)]
    r = LIB.ledger(len(gid), *[x.ctypes.data for x in cols], t30, t7, G, ctypes.byref(o))
    if r != len(gid):
        raise RuntimeError(f"kernel failed ({r})")
    return arrs


# ---------------------------------------------------------------- inputs
def sync_recent(chain, idx_last):
    """Append hot-store fills after the wallet index to the durable trade log (dedup by identity)."""
    d = f"{EM}/traders/{chain}/log"
    os.makedirs(d, exist_ok=True)
    dst = f"{d}/recent_trades.parquet"
    hot = sqlite3.connect(f"file:{CHARTS}/hot/{chain}/history.sqlite3?mode=ro", uri=True, timeout=30)
    rows = hot.execute("""SELECT f.identity, m.token, m.side, m.trader, m.usd, m.eligible, f.time, f.slot, f.event_order, f.base_volume
                          FROM fills f JOIN fill_meta m ON m.identity = f.identity
                          WHERE f.slot > ? AND m.trader IS NOT NULL AND m.usd IS NOT NULL""", (idx_last,)).fetchall()
    hot.close()
    names = ["identity", "token", "side", "trader", "usd", "eligible", "time", "slot", "event_order", "base_volume"]
    new = pa.table({n: [r[i] for r in rows] for i, n in enumerate(names)},
                   schema=pa.schema([("identity", pa.string()), ("token", pa.string()), ("side", pa.string()), ("trader", pa.string()),
                                     ("usd", pa.float64()), ("eligible", pa.int64()), ("time", pa.int64()), ("slot", pa.int64()),
                                     ("event_order", pa.int64()), ("base_volume", pa.float64())]))
    c = connect()
    c.register("new_rows", new)
    old = f"SELECT * FROM {pq(dst)} WHERE slot > {idx_last}" if os.path.exists(dst) else "SELECT * FROM new_rows WHERE false"
    write_parquet(c, f"""SELECT * FROM (SELECT * FROM new_rows UNION ALL BY NAME {old})
                         QUALIFY row_number() OVER (PARTITION BY identity ORDER BY slot) = 1""", dst)
    n, lo, hi, tmax = c.execute(f"SELECT count(*), min(slot), max(slot), max(time) FROM {pq(dst)}").fetchone()
    c.close()
    log(f"{chain} recent trades: {n} fills, blocks {lo}..{hi} (hot store {len(rows)})")
    return dst, tmax


def load_dims(c, chain, recent, now):
    deep = f"{CHARTS}/data/{chain}/candles.duckdb"
    c.execute(f"ATTACH '{deep}' AS d (READ_ONLY)")
    c.execute(f"""CREATE TABLE mk AS
      SELECT w.mid::USMALLINT AS mid, w.token, m.symbol, m.name, coalesce(m.kind, 'token') AS kind, m.quality, m.ui_multiplier,
             m.first_minute::BIGINT AS first_t, m.last_price, m.last_time, m.usd_volume
      FROM {pq(f'{CHARTS}/data/{chain}/wallet/markets.parquet')} w LEFT JOIN d.markets m ON m.token = w.token""")
    # stable-like markets (USD anchors, yield-bearing dollars): their PnL is not trading skill -> no leaderboards / labels
    c.execute("""CREATE TABLE stab AS
      SELECT token, max(close) / nullif(min(close), 0) AS rng, avg(close) AS avgc, count(*) AS n FROM d.token_bars
      WHERE period = 86400 AND time >= (SELECT max(time) FROM d.token_bars WHERE period = 86400) - 7776000000 GROUP BY token""")
    c.execute("""ALTER TABLE mk ADD COLUMN stable BOOLEAN;
                 UPDATE mk SET stable = coalesce((SELECT s.rng < 1.10 AND s.avgc BETWEEN 0.85 AND 1.35 AND s.n >= 5 FROM stab s WHERE s.token = mk.token), false)""")
    c.execute("""CREATE TABLE refpx AS SELECT token, arg_max(close, time) AS ref_close, max(time) // 1000 AS ref_t FROM d.token_bars
                 WHERE period = 3600 AND close > 0 AND count >= 3 AND volume >= 100 GROUP BY token""")
    c.execute("""ALTER TABLE mk ADD COLUMN ref_close DOUBLE; ALTER TABLE mk ADD COLUMN ref_t BIGINT;
                 UPDATE mk SET ref_close = r.ref_close, ref_t = r.ref_t FROM refpx r WHERE r.token = mk.token""")
    # 7-day USD turnover per token (deep daily bars + recent fills after the deep store): caps how much of a held
    # position is marked to market (paper gains on a thin market are not realizable)
    until = int(c.execute("SELECT value FROM d.info WHERE key = 'data_until_ms'").fetchone()[0]) // 1000
    c.execute(f"""CREATE TABLE vol7 AS SELECT token, sum(v) AS vol7 FROM (
                    SELECT token, volume AS v FROM d.token_bars WHERE period = 86400 AND time >= {(now - 7 * DAY) * 1000}
                    UNION ALL SELECT token, usd FROM {pq(recent)} WHERE time > {until} AND time >= {now - 7 * DAY}) GROUP BY token""")
    c.execute("""ALTER TABLE mk ADD COLUMN vol7 DOUBLE;
                 UPDATE mk SET vol7 = coalesce((SELECT v.vol7 FROM vol7 v WHERE v.token = mk.token), 0)""")
    kinds = " ".join(f"WHEN '{k}' THEN {v}" for k, v in QK.items())
    c.execute(f"""CREATE TABLE apx AS SELECT (CASE kind {kinds} END)::UTINYINT AS qk, minute::BIGINT AS minute, px FROM d.anchor_px
                  UNION ALL SELECT 0, 0, 1.0 ORDER BY 1, 2""")
    band = " ".join(f"WHEN '{k}' THEN {v}" for k, v in BAND.items())
    c.execute(f"""CREATE TABLE bars AS
      SELECT mk.mid, (b.time // 1000)::BIGINT AS ht, b.low, b.high, b.close, (CASE mk.kind {band} ELSE 1.5 END) AS f,
             (CASE WHEN mk.kind IN ('stock', 'anchor') THEN 21600 ELSE 3600 END) AS reach
      FROM d.token_bars b JOIN mk ON mk.token = b.token WHERE b.period = 3600 AND b.close > 0 AND b.count >= 2 ORDER BY 1, 2""")
    c.execute("DETACH d")
    c.execute(f"""CREATE TABLE bm AS SELECT minute::BIGINT AS minute, first_block::UBIGINT AS first_block
                  FROM {pq(f'{CHARTS}/data/block_minutes_{chain}.parquet')} WHERE minute > 0 ORDER BY first_block""")
    lab = f"{GL}/export/{chain}/labels.parquet"
    ex = ",".join(f"'{x}'" for x in EXCLUDE)
    c.execute(f"""CREATE TABLE ex AS SELECT {W3.format(h='hex(address)')}, any_value(category) AS category
                  FROM {pq(lab)} WHERE category IN ({ex}) GROUP BY ALL""")
    # durable recent fills, keyed like the index
    c.execute(f"""CREATE TABLE rec AS
      WITH r AS (SELECT {W3.format(h='upper(substr(r.trader, 3))')}, mk.mid, r.time::BIGINT AS t, r.slot::UBIGINT AS bn,
                        r.event_order::UINTEGER AS li, r.side = 'B' AS buy, r.base_volume AS q, r.usd, upper(substr(r.trader, 3, 1)) AS nib
                 FROM {pq(recent)} r JOIN mk ON mk.token = r.token
                 WHERE length(r.trader) = 42 AND r.base_volume > 0 AND r.usd > 0)
      SELECT r.* FROM r ANTI JOIN ex USING (w1, w2, w3)""")
    log(f"{chain} dims: {c.execute('SELECT count(*) FROM mk').fetchone()[0]} markets, "
        f"{c.execute('SELECT count(*) FROM ex').fetchone()[0]} excluded senders, {c.execute('SELECT count(*) FROM rec').fetchone()[0]} recent fills")


# ---------------------------------------------------------------- per nibble ledger
def ledger_nibble(c, chain, nib, now, work):
    f = f"{CHARTS}/data/{chain}/wallet/trades_b={nib.lower()}.parquet"
    t0 = time.time()
    c.execute(f"""CREATE OR REPLACE TEMP TABLE tr AS
      WITH src AS (SELECT w1, w2, w3, mid, bn::UBIGINT AS bn, log_index::UINTEGER AS li, buy, base_amt::DOUBLE AS q,
                          quote_amt::DOUBLE AS qa, qk FROM {pq(f)}),
      tx AS (SELECT src.* FROM src ANTI JOIN ex USING (w1, w2, w3)),
      tm AS (SELECT tx.*, b.minute AS t FROM tx ASOF JOIN bm b ON tx.bn >= b.first_block),
      tu AS (SELECT tm.w1, tm.w2, tm.w3, tm.mid, tm.t, tm.bn, tm.li, tm.buy, tm.q, tm.qa * a.px AS usd
             FROM tm ASOF JOIN apx a ON a.qk = tm.qk AND tm.t >= a.minute)
      SELECT * FROM tu WHERE q > 0 AND usd > 0
      UNION ALL SELECT w1, w2, w3, mid, t, bn, li, buy, q, usd FROM rec WHERE nib = '{nib.upper()}'""")
    # wash: a buy and a sell of the same token by the same wallet in one block
    c.execute("""CREATE OR REPLACE TEMP TABLE wash AS SELECT w1, w2, w3, mid, bn, count(*) AS n FROM tr GROUP BY ALL
                 HAVING bool_or(buy) AND bool_or(NOT buy)""")
    # bad prints (wrong price or wrong amount) leave the ledger: outside the token's hourly reference band, more than
    # 10x off the last reference close when no bar is near, or a single fill above MAX_FILL_USD
    c.execute(f"""CREATE OR REPLACE TEMP TABLE k AS
      WITH j AS (SELECT tr.*, b.ht, b.low, b.high, b.close, b.f, b.reach
                 FROM (SELECT tr.* FROM tr ANTI JOIN wash USING (w1, w2, w3, mid, bn)) tr
                 ASOF LEFT JOIN bars b ON b.mid = tr.mid AND tr.t >= b.ht)
      SELECT w1, w2, w3, mid, t, bn, li, buy, q, usd AS u,
             usd > {MAX_FILL_USD}
             OR coalesce(t - ht < reach AND (usd / q > high * f OR usd / q < low / f), false)
             OR coalesce(t - ht >= reach AND (usd / q > close * 10 OR usd / q < close / 10), false) AS glitch
      FROM j""")
    c.execute("""CREATE OR REPLACE TEMP TABLE grp AS
      SELECT k.*, (row_number() OVER (ORDER BY w1, w2, w3, mid) - 1)::BIGINT AS gid
      FROM (SELECT w1, w2, w3, mid FROM k WHERE NOT glitch GROUP BY ALL) k""")
    c.execute("CREATE OR REPLACE TEMP TABLE gl AS SELECT w1, w2, w3, mid, count(*) AS glitches FROM k WHERE glitch GROUP BY ALL")
    G = c.execute("SELECT count(*) FROM grp").fetchone()[0]
    if not G:
        return 0, 0
    a = c.execute("""SELECT g.gid, k.t, k.bn::BIGINT AS bn, k.buy::UTINYINT AS buy, k.q, k.u
                     FROM k JOIN grp g USING (w1, w2, w3, mid) WHERE NOT k.glitch ORDER BY g.gid, k.t, k.bn, k.li""").fetchnumpy()
    n = len(a["gid"])
    res = run_kernel(a["gid"], a["t"], a["bn"], a["buy"], a["q"], a["u"], now - 30 * DAY, now - 7 * DAY, G)
    del a
    keys = c.execute("""SELECT g.w1, g.w2, g.w3, g.mid, coalesce(x.glitches, 0) AS glitches, coalesce(w.n, 0) AS wash_rows FROM grp g
                        LEFT JOIN gl x USING (w1, w2, w3, mid)
                        LEFT JOIN (SELECT w1, w2, w3, mid, sum(n) AS n FROM wash GROUP BY ALL) w USING (w1, w2, w3, mid)
                        ORDER BY g.gid""").fetchnumpy()
    tbl = pa.table({**{k: keys[k] for k in ("w1", "w2", "w3", "mid", "glitches", "wash_rows")}, **res})
    work.register("part", tbl)
    if "wt" in {r[0] for r in work.execute("SHOW TABLES").fetchall()}:
        work.execute("INSERT INTO wt SELECT * FROM part")
    else:
        work.execute("CREATE TABLE wt AS SELECT * FROM part")
    work.unregister("part")
    log(f"{chain} nibble {nib}: {n} trades, {G} wallet-token groups, {round(time.time() - t0)}s")
    return n, G


# ---------------------------------------------------------------- results
def finish(c, chain, now, recent, out, info):
    an = f"{CHARTS}/data/{chain}/analytics"
    # price now: median of the last 25 fills in the last 6 h (>= 5 fills) when it agrees with the reference close
    # (x1.5 stocks / anchors, x5 tokens); otherwise the last hourly reference close with >= 3 fills. em-charts' own
    # last_price can be a single garbage print on dead tokens, so it is not used.
    c.execute(f"""CREATE TABLE px AS
      WITH r AS (SELECT r.token, r.usd / r.base_volume AS p, row_number() OVER (PARTITION BY r.token ORDER BY r.slot DESC, r.event_order DESC) AS k
                 FROM {pq(recent)} r WHERE r.eligible = 1 AND r.base_volume > 0 AND r.time >= {now - 6 * 3600}),
      h AS (SELECT token, median(p) AS p, count(*) AS n FROM r WHERE k <= 25 GROUP BY token HAVING count(*) >= 5),
      j AS (SELECT mk.mid, mk.ref_close, h.p AS live, CASE WHEN mk.kind IN ('stock', 'anchor') THEN 1.5 ELSE 5.0 END AS f FROM mk LEFT JOIN h USING (token))
      SELECT mid, CASE WHEN live IS NOT NULL AND (ref_close IS NULL OR live BETWEEN ref_close / f AND ref_close * f) THEN live ELSE ref_close END AS price_now,
             live IS NOT NULL AND (ref_close IS NULL OR live BETWEEN ref_close / f AND ref_close * f) AS live FROM j""")
    c.execute("CREATE TABLE wt AS SELECT * FROM work.wt")
    # a token whose fills disagree with its reference on >10% of trades (or that em-charts flags) has no trustworthy
    # PnL: it stays in wallet_token_pnl with price_flag, but leaves leaderboards, labels and the wallet totals
    c.execute(f"""CREATE TABLE tokq AS
      SELECT mk.mid, coalesce(sum(wt.glitches), 0) AS glitches, coalesce(sum(wt.nb + wt.ns), 0) AS fills,
             (coalesce(sum(wt.nb + wt.ns), 0) >= 100 AND coalesce(sum(wt.glitches), 0) > {GLITCH_TOKEN_SHARE} * sum(wt.nb + wt.ns))
             OR coalesce(mk.quality, 'ok') = 'flagged' AS price_flag
      FROM mk LEFT JOIN wt USING (mid) GROUP BY mk.mid, mk.quality""")
    c.execute(f"ALTER TABLE wt ADD COLUMN wallet VARCHAR; UPDATE wt SET wallet = {HEX}")
    # balance now for open swap inventory: snapshot holders + transfers since + realtime transfers, plus lending collateral
    c.execute("CREATE TABLE openpos AS SELECT DISTINCT w.wallet, mk.token FROM wt w JOIN mk USING (mid) WHERE w.qty_end > 0")
    xr = glob.glob(f"{an}/rt/xfer_*.parquet")
    xsrc = f"SELECT token, holder, delta FROM {pq(an + '/xfer_gap.parquet')}" + (
        " UNION ALL SELECT token, holder, delta FROM read_parquet([%s])" % ",".join(f"'{x}'" for x in xr) if xr else "")
    c.execute(f"""CREATE TABLE bal AS
      SELECT token, holder, sum(amount) AS amount FROM (
        SELECT token, holder, amount FROM {pq(an + '/holders_snap.parquet')}
        UNION ALL {xsrc}
        UNION ALL SELECT asset, wallet, amount FROM {pq(an + '/positions_snap.parquet')} WHERE category IN ('collateral', 'supplied') AND amount > 0
      ) x SEMI JOIN openpos o ON o.wallet = x.holder AND o.token = x.token GROUP BY ALL""")
    # econ: debt now and borrow / liquidation history
    sheets = f"{EM}/econ/{chain}/v3/sheet_summary.v3.parquet"
    acts = f"{EM}/econ/{chain}/v3/econ_actions/*.parquet"
    c.execute(f"""CREATE TABLE lev AS
      WITH tw AS (SELECT DISTINCT wallet FROM wt),
      a AS (SELECT lower(wallet) AS wallet, count(*) FILTER (WHERE action = 'borrow') AS borrows,
                   count(*) FILTER (WHERE action IN ('liquidated')) AS liquidations, list(DISTINCT protocol) FILTER (WHERE action = 'borrow') AS protocols
            FROM {pq(acts)} WHERE action IN ('borrow', 'liquidated') GROUP BY 1)
      SELECT tw.wallet, coalesce(a.borrows, 0) AS borrows, coalesce(a.liquidations, 0) AS liquidations, a.protocols,
             coalesce(s.debt_usd, 0) AS debt_usd, coalesce(s.supplied_usd, 0) AS supplied_usd, s.net_worth_usd
      FROM tw LEFT JOIN a USING (wallet) LEFT JOIN {pq(sheets)} s ON lower(s.wallet) = tw.wallet""")
    # token-level leverage: borrowed this token (short) or posted it as collateral
    c.execute(f"""CREATE TABLE levtok AS
      SELECT lower(a.wallet) AS wallet, lower(a.asset) AS token, count(*) FILTER (WHERE a.action = 'borrow') AS token_borrows,
             count(*) FILTER (WHERE a.action = 'supply' AND a.pos_kind = 'collateral') AS token_collateral_posts
      FROM {pq(acts)} a SEMI JOIN (SELECT DISTINCT wallet FROM wt) w ON w.wallet = lower(a.wallet)
      WHERE a.action IN ('borrow', 'supply') GROUP BY ALL""")

    c.execute(f"""CREATE TABLE wtp AS
      WITH b AS (SELECT holder AS wallet, token, amount FROM bal)
      SELECT w.wallet, w.mid, mk.token, mk.symbol, mk.kind, w.nb, w.ns, w.nb + w.ns AS trades, w.n30, w.n7,
             w.bq, w.sq, w.bu AS buy_usd, w.su AS sell_usd, w.bu + w.su AS volume_usd,
             w.bu30 + w.su30 AS volume_30d, w.bu7 + w.su7 AS volume_7d,
             w.real_all AS realized_usd, w.real30 AS realized_30d, w.real7 AS realized_7d,
             w.qty_end AS swap_inventory, CASE WHEN w.qty_end > 0 THEN w.cost_end / w.qty_end END AS avg_cost,
             least(w.qty_end, greatest(coalesce(b.amount, 0), 0)) AS held, p.price_now, p.live AS price_live,
             w.xin_q, w.xin_u AS transfer_in_usd, w.trips, w.wins, w.win_sum, w.loss_sum,
             CASE WHEN w.trips > 0 THEN w.hold_sum / w.trips END AS avg_hold_s,
             w.first_t AS first_trade, w.last_t AS last_trade,  -- unix seconds
             CASE WHEN w.first_buy_t >= 0 THEN w.first_buy_t END AS first_buy, w.first_buy_t,
             w.first_buy_px, w.max_gain AS max_single_gain, w.glitches, w.wash_rows,
             mk.first_t AS token_first_t, w.bq30, w.bq7, w.open_t,
             coalesce(lt.token_borrows, 0) AS token_borrows, coalesce(lt.token_collateral_posts, 0) AS token_collateral_posts, mk.vol7
      , q.price_flag, mk.stable
      FROM wt w JOIN mk USING (mid) LEFT JOIN px p USING (mid) JOIN tokq q USING (mid)
      LEFT JOIN b ON b.wallet = w.wallet AND b.token = mk.token
      LEFT JOIN levtok lt ON lt.wallet = w.wallet AND lt.token = mk.token""")
    # unrealized: held x (price now - avg cost), with the held value capped at a quarter of the token's 7-day turnover
    # (min $1k); the uncapped figure stays in unrealized_paper_usd
    c.execute(f"""ALTER TABLE wtp ADD COLUMN unrealized_usd DOUBLE; ALTER TABLE wtp ADD COLUMN unrealized_paper_usd DOUBLE;
      ALTER TABLE wtp ADD COLUMN transferred_out_q DOUBLE;
      UPDATE wtp SET unrealized_paper_usd = CASE WHEN held > 0 AND price_now IS NOT NULL THEN held * (price_now - avg_cost) ELSE 0 END,
                     unrealized_usd = CASE WHEN held > 0 AND price_now IS NOT NULL
                                           THEN least(held * price_now, greatest({MARK_FLOOR_USD}, {MARK_SHARE} * vol7)) - held * avg_cost ELSE 0 END,
                     transferred_out_q = greatest(swap_inventory - held, 0)""")
    c.execute(f"""CREATE TABLE wtx AS SELECT *,
        realized_usd + unrealized_usd AS pnl_all,
        realized_30d + unrealized_usd * CASE WHEN swap_inventory > 0 THEN least(1, bq30 / swap_inventory) ELSE 0 END AS pnl_30d,
        realized_7d + unrealized_usd * CASE WHEN swap_inventory > 0 THEN least(1, bq7 / swap_inventory) ELSE 0 END AS pnl_7d,
        CASE WHEN buy_usd > 0 THEN (realized_usd + unrealized_usd) / buy_usd END AS roi,
        CASE WHEN trips > 0 THEN wins::DOUBLE / trips END AS win_rate,
        (transfer_in_usd > 0.1 * greatest(sell_usd, 1) OR transferred_out_q * coalesce(avg_cost, 0) > 0.1 * greatest(buy_usd, 1)) AS transfer_adjusted,
        CASE WHEN first_buy_t >= 0 AND token_first_t IS NOT NULL AND {now} > token_first_t
             THEN greatest(0, least(1, 1 - (first_buy_t - token_first_t)::DOUBLE / ({now} - token_first_t))) END AS early_score,
        CASE WHEN first_buy_px > 0 AND price_now > 0 THEN price_now / first_buy_px END AS entry_multiple
      FROM wtp""")
    # wallet level (cross-token) + bot-like behaviour
    c.execute(f"""CREATE TABLE wp AS
      WITH a AS (SELECT wallet, count(*) AS tokens, sum(trades) AS trades, sum(n30) AS trades_30d, sum(n7) AS trades_7d,
                        sum(volume_usd) AS volume_usd, sum(volume_30d) AS volume_30d, sum(volume_7d) AS volume_7d,
                        sum(buy_usd) AS buy_usd, sum(sell_usd) AS sell_usd,
                        sum(realized_usd) AS realized_usd, sum(unrealized_usd) AS unrealized_usd, sum(pnl_all) AS pnl_all,
                        sum(pnl_30d) AS pnl_30d, sum(pnl_7d) AS pnl_7d, sum(trips) AS trips, sum(wins) AS wins,
                        sum(avg_hold_s * trips) / nullif(sum(trips), 0) AS avg_hold_s,
                        count(*) FILTER (WHERE pnl_all > 0 AND volume_usd >= 1000) AS profitable_tokens,
                        max(pnl_all) AS best_token_pnl, arg_max(symbol, pnl_all) AS best_token,
                        sum(greatest(pnl_all, 0)) AS gross_profit, max(max_single_gain) AS max_single_gain,
                        sum(transfer_in_usd) AS transfer_in_usd, sum(wash_rows) AS wash_rows, sum(glitches) AS glitches,
                        min(first_trade) AS first_trade, max(last_trade) AS last_trade,
                        bool_or(transfer_adjusted AND volume_usd >= 1000) AS transfer_adjusted
                 FROM wtx WHERE NOT price_flag AND NOT stable GROUP BY wallet)
      SELECT a.*, CASE WHEN a.trips > 0 THEN a.wins::DOUBLE / a.trips END AS win_rate,
             CASE WHEN a.buy_usd > 0 THEN a.pnl_all / a.buy_usd END AS roi,
             a.trades / greatest(1, (a.last_trade - a.first_trade) / {DAY}) AS trades_per_day,
             l.debt_usd, l.supplied_usd, l.net_worth_usd, l.borrows, l.liquidations, l.protocols,
             CASE WHEN a.trades >= {BOT_TRADES} AND a.avg_hold_s < {BOT_HOLD_S} THEN 'fast_round_trips'
                  WHEN a.trades >= 500 AND a.trades / greatest(1, (a.last_trade - a.first_trade) / {DAY}) > {BOT_PER_DAY} THEN 'trades_per_day'
                  WHEN a.wash_rows > {BOT_WASH_SHARE} * a.trades AND a.trades >= 50 THEN 'same_block_flips'
                  WHEN a.tokens > {BOT_TOKENS} THEN 'token_spray' END AS bot_reason
      FROM a LEFT JOIN lev l USING (wallet)""")

    # token leaderboards (top LEADERBOARD_KEEP per token and window, bots out)
    parts = []
    for w, (vmin, nmin) in WINDOW_GATE.items():
        pnl, vol, n = {"all": ("pnl_all", "volume_usd", "trades"), "30d": ("pnl_30d", "volume_30d", "n30"),
                       "7d": ("pnl_7d", "volume_7d", "n7")}[w]
        parts.append(f"""SELECT '{w}' AS win, x.mid, x.token, x.symbol, x.kind, x.wallet, x.{pnl} AS pnl_usd, x.{vol} AS volume_usd,
                                x.{n} AS trades, x.realized_usd, x.unrealized_usd, x.roi, x.win_rate, x.trips, x.avg_hold_s,
                                x.held, x.avg_cost, x.price_now, x.transfer_adjusted, x.early_score, x.entry_multiple, x.max_single_gain,
                                x.first_trade, x.last_trade
                         FROM wtx x JOIN wp USING (wallet) WHERE wp.bot_reason IS NULL AND NOT x.price_flag AND NOT x.stable AND x.{vol} >= {vmin} AND x.{n} >= {nmin}""")
    c.execute(f"CREATE TABLE lb_all AS {' UNION ALL '.join(parts)}")
    floor = " ".join(f"WHEN '{k}' THEN {v}" for k, v in TOKEN_FLOOR.items())
    c.execute(f"""CREATE TABLE lbtok AS SELECT win, mid, any_value(kind) AS kind, count(*) AS traders, sum(volume_usd) AS volume
                  FROM lb_all GROUP BY ALL
                  HAVING count(*) >= {TOKEN_MIN_TRADERS} AND sum(volume_usd) >= (CASE any_value(kind) {floor} ELSE 100000 END)""")
    c.execute(f"""CREATE TABLE lb AS
      SELECT l.*, row_number() OVER (PARTITION BY l.win, l.mid ORDER BY l.pnl_usd DESC) AS rank, t.traders AS eligible_traders
      FROM lb_all l JOIN lbtok t USING (win, mid)
      QUALIFY row_number() OVER (PARTITION BY l.win, l.mid ORDER BY l.pnl_usd DESC) <= {LEADERBOARD_KEEP}""")

    # smart money (cross-token, all history; consistency, sample size and single-trade caps)
    c.execute("""CREATE TABLE sm AS
      SELECT wp.*,
             least(0.95, 0.55 + 0.15 * least(1, trips / 50.0) + 0.15 * least(1, (win_rate - 0.55) / 0.25)
                         + 0.10 * least(1, profitable_tokens / 5.0)) AS confidence
      FROM wp WHERE bot_reason IS NULL AND volume_usd >= 25000 AND trips >= 10 AND pnl_all >= 10000 AND roi >= 0.10
        AND realized_usd >= 0.5 * pnl_all AND win_rate >= 0.55 AND profitable_tokens >= 2
        AND best_token_pnl <= 0.8 * gross_profit AND max_single_gain <= 0.5 * pnl_all
        AND (trades_30d = 0 OR pnl_30d >= 0) AND transfer_in_usd <= 0.5 * greatest(sell_usd, 1)""")

    # labels: smart_money, top_trader:<token> (30d top 20), early_buyer:<token>, leveraged_trader
    c.execute(f"""CREATE TABLE labs AS
      SELECT wallet, 'smart_money' AS label, NULL AS token, NULL AS symbol, confidence,
             json_object('pnl_usd', round(pnl_all, 2), 'pnl_30d', round(pnl_30d, 2), 'realized_usd', round(realized_usd, 2), 'roi', round(roi, 4),
                         'win_rate', round(win_rate, 4), 'round_trips', trips, 'tokens', tokens, 'profitable_tokens', profitable_tokens,
                         'volume_usd', round(volume_usd, 2), 'best_token', best_token) AS evidence
      FROM sm
      UNION ALL
      SELECT l.wallet, 'top_trader', l.token, l.symbol, CASE WHEN l.rank <= 5 THEN 0.9 WHEN l.rank <= 10 THEN 0.8 ELSE 0.7 END,
             json_object('rank', l.rank, 'window', '30d', 'pnl_usd', round(l.pnl_usd, 2), 'volume_usd', round(l.volume_usd, 2),
                         'trades', l.trades, 'eligible_traders', l.eligible_traders)
      FROM lb l WHERE l.win = '30d' AND l.rank <= {TOP_LABEL} AND l.pnl_usd > 0
      UNION ALL
      SELECT e.wallet, 'early_buyer', e.token, e.symbol, 0.8,
             json_object('buyer_rank', e.k, 'first_buy', e.first_buy, 'first_buy_px', e.first_buy_px, 'entry_multiple', round(e.entry_multiple, 3),
                         'buy_usd', round(e.buy_usd, 2), 'early_score', round(e.early_score, 4))
      FROM (SELECT x.*, row_number() OVER (PARTITION BY x.mid ORDER BY x.first_buy_t) AS k
            FROM wtx x JOIN wp USING (wallet) WHERE wp.bot_reason IS NULL AND NOT x.price_flag AND NOT x.stable AND x.first_buy_t >= 0 AND x.buy_usd >= 1000) e
      WHERE e.k <= 25 AND e.early_score >= 0.9 AND (e.kind <> 'token' OR e.entry_multiple >= 2)
        AND e.mid IN (SELECT mid FROM lbtok WHERE win = 'all')
      UNION ALL
      SELECT wallet, 'leveraged_trader', NULL, NULL, CASE WHEN debt_usd >= 5000 AND volume_30d >= 10000 THEN 0.85 ELSE 0.7 END,
             json_object('debt_usd', round(debt_usd, 2), 'supplied_usd', round(supplied_usd, 2), 'borrows', borrows, 'liquidations', liquidations,
                         'protocols', protocols, 'volume_usd', round(volume_usd, 2))
      FROM wp WHERE bot_reason IS NULL
        AND ((debt_usd >= 5000 AND volume_30d >= 10000)                    -- trading now on borrowed money
             OR (liquidations >= 1 AND borrows >= 3 AND volume_usd >= 25000))  -- traded with enough leverage to be liquidated""")

    # ---- write (each file atomic)
    # sorted by the lookup key with small row groups: a wallet or token lookup reads a few KB (min/max pruning)
    write_parquet(c, "SELECT * EXCLUDE (bq30, bq7, open_t, first_buy_t, token_first_t) FROM wtx ORDER BY wallet, pnl_all DESC",
                  f"{out}/wallet_token_pnl.parquet", 8192)
    write_parquet(c, "SELECT * FROM wp ORDER BY wallet", f"{out}/wallet_pnl.parquet", 8192)
    write_parquet(c, "SELECT * FROM lb ORDER BY token, win, rank", f"{out}/token_leaderboard.parquet", 4096)
    write_parquet(c, "SELECT * FROM sm ORDER BY pnl_all DESC", f"{out}/smart_money.parquet")
    write_parquet(c, "SELECT * FROM labs ORDER BY wallet", f"{out}/trader_labels.parquet", 8192)
    write_parquet(c, f"""SELECT from_hex(substr(wallet, 3)) AS address, NULL::UINTEGER AS node_id,
                                CASE WHEN token IS NULL THEN label ELSE label || ':' || symbol END AS category, NULL::UTINYINT AS category_code,
                                confidence::FLOAT AS confidence, 'traders_v1' AS source, 'final' AS state, coalesce(token, label) AS subtype,
                                NULL::UTINYINT AS tier, label AS rule, NULL::VARCHAR AS entity_name, NULL::FLOAT AS importance,
                                'code' AS provider, NULL::VARCHAR AS model, evidence::VARCHAR AS evidence_json, NULL::UBIGINT AS wal_seq,
                                'traders' AS layer
                         FROM labs""", f"{GL}/export/{chain}/labels_traders.parquet")
    stats = {
        "labels": dict(c.execute("SELECT label, count(*) FROM labs GROUP BY 1").fetchall()),
        "wallet_tokens": c.execute("SELECT count(*) FROM wtx").fetchone()[0],
        "wallets": c.execute("SELECT count(*) FROM wp").fetchone()[0],
        "bot_like": dict(c.execute("SELECT bot_reason, count(*) FROM wp WHERE bot_reason IS NOT NULL GROUP BY 1").fetchall()),
        "leaderboard_tokens": dict(c.execute("SELECT win, count(*) FROM lbtok GROUP BY 1").fetchall()),
        "leaderboard_tokens_by_kind_30d": dict(c.execute("SELECT kind, count(*) FROM lbtok WHERE win = '30d' GROUP BY 1").fetchall()),
        "glitch_fills": c.execute("SELECT sum(glitches) FROM wt").fetchone()[0],
        "price_flagged_tokens": c.execute("SELECT count(*) FROM tokq WHERE price_flag AND fills > 0").fetchone()[0],
        "stable_tokens": c.execute("SELECT count(*) FROM mk WHERE stable").fetchone()[0],
        "wash_rows": c.execute("SELECT sum(wash_rows) FROM wt").fetchone()[0],
    }
    info.update(stats)
    info["built_at"] = int(time.time())
    tmp = f"{out}/info.json.tmp"
    json.dump(info, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, f"{out}/info.json")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chain", choices=("robinhood", "arbitrum"))
    ap.add_argument("--nibbles", default="0123456789abcdef")
    ap.add_argument("--keep-work", action="store_true")
    a = ap.parse_args()
    chain = a.chain
    os.makedirs(WORKROOT, exist_ok=True)
    lock = open(f"{WORKROOT}/{chain}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"{chain}: another traders run is active; exit")
        return
    if free_gb() < FLOOR_GB + 5:
        raise SystemExit(f"only {free_gb():.0f} GB free")
    t0 = time.time()
    winfo = json.load(open(f"{CHARTS}/data/{chain}/wallet/info.json"))
    idx_last = int(winfo["trades_last_block"])
    recent, tmax = sync_recent(chain, idx_last)
    now = int(tmax or time.time())
    out = f"{EM}/traders/{chain}"
    os.makedirs(out, exist_ok=True)
    wdir = f"{WORKROOT}/{chain}"
    shutil.rmtree(wdir, ignore_errors=True)
    os.makedirs(wdir)
    work = connect(f"{wdir}/work.duckdb")
    c = connect()
    load_dims(c, chain, recent, now)
    rows = groups = 0
    for nib in a.nibbles:
        if free_gb() < FLOOR_GB:
            raise SystemExit(f"stopping: {free_gb():.0f} GB free")
        n, g = ledger_nibble(c, chain, nib, now, work)
        rows += n
        groups += g
    work.close()
    for t in ("tr", "wash", "k", "grp", "gl", "bars", "rec", "apx", "bm", "ex"):
        c.execute(f"DROP TABLE IF EXISTS {t}")
    c.execute(f"ATTACH '{wdir}/finish.duckdb' AS fin")
    c.execute("CREATE TABLE fin.mk AS SELECT * FROM mk")
    c.close()
    c = connect(f"{wdir}/finish.duckdb")
    c.execute(f"ATTACH '{wdir}/work.duckdb' AS work (READ_ONLY)")
    info = {"chain": chain, "now": now, "now_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "index_last_block": idx_last, "trades": rows, "groups": groups, "nibbles": a.nibbles,
            "method": "average cost per wallet x token; swaps attributed to tx.from; see README.md"}
    stats = finish(c, chain, now, recent, out, info)
    c.close()
    if not a.keep_work:
        shutil.rmtree(wdir, ignore_errors=True)
    shutil.rmtree(f"{WORKROOT}/tmp/{os.getpid()}", ignore_errors=True)
    log(f"{chain} DONE {rows} trades {groups} groups {round(time.time() - t0)}s {json.dumps(stats, default=str)}")


if __name__ == "__main__":
    main()
