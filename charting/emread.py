"""em-charts read API (stable, read-only): deep DuckDB history + hot SQLite suffix for one chain.

    from emread import Reader
    r = Reader("robinhood")                       # or "arbitrum"
    r.markets(kind="stock")                       # [{id, chain, token, symbol, name, decimals, kind, last_price, ...}]
    r.market("NVDA")                              # by symbol, 0x address or '<chain>:<0x>' id
    r.bars("token", "0xd060...", 900, before_ms=None, limit=600)   # oldest -> newest
    r.bars("pool", "0x62ab...", 3600)
    r.tape("0xd060...", pool=None, before_s=None, limit=100)       # newest first
    r.data_until_ms(); r.data_version(); r.resolution_starts(); r.info()

Units: times are UTC epoch milliseconds for bars (bucket start) and seconds for tape rows; prices and volumes in
USD; base_volume in base-token units. Intervals are any whole number of minutes/hours/days up to 1 week; each is
rolled up from the largest stored resolution that divides it (1m kept 60 d, 5m 180 d, 15m 2 y, 1h/4h/1d full).
Weeks start Monday 00:00 UTC (Ticking's intervalStart). The token series is the em_pool_reference_v2 composite; pool
series are per-pool USD prints.

Read-only and safe next to the em-charts server: DuckDB is opened read_only (re-opened when a rebuild swaps the
file) and the hot SQLite store is opened mode=ro (WAL, never blocks its writer). Thread-safe: every call uses its
own cursors; one Reader per process is enough.
"""
import os
import sqlite3
import threading
import time

import duckdb

from common import RETENTION, ROOT

STORED = sorted(RETENTION)
WEEK_ORIGIN = -259200000


def interval_start(t, ms):
    origin = WEEK_ORIGIN if ms == 604800000 else 0
    return (t - origin) // ms * ms + origin


class Reader:
    def __init__(self, chain, root=ROOT):
        self.chain = self.name = chain
        self.deep_path = f"{root}/data/{chain}/candles.duckdb"
        self.hot_path = f"{root}/hot/{chain}/history.sqlite3"
        self._deep, self._deep_id = None, None
        self._lock = threading.Lock()
        self._local = threading.local()
        self._meta_at = 0
        self.refresh(force=True)

    # ---------- connections
    def deep(self):
        st = os.stat(self.deep_path)
        ident = (st.st_ino, st.st_mtime)
        with self._lock:
            if self._deep is None or ident != self._deep_id:
                con = duckdb.connect(self.deep_path, read_only=True)
                con.execute("SET threads=2")
                con.execute("SET memory_limit='2GB'")
                self._deep, self._deep_id = con, ident
                self._meta_at = 0
            return self._deep.cursor()

    @property
    def con(self):
        return self.deep()

    def hot(self):
        c = getattr(self._local, "hot", None)
        if c is None and os.path.exists(self.hot_path):
            c = sqlite3.connect(f"file:{self.hot_path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
            # chart-cache 2026-10-23: enlarge page cache + mmap for arb/RH hot reads (RO; tip writer untouched)
            try:
                c.execute("PRAGMA query_only=ON")
                c.execute("PRAGMA busy_timeout=5000")
                c.execute("PRAGMA temp_store=MEMORY")
                c.execute("PRAGMA cache_size=-131072")  # ~128MB
                c.execute("PRAGMA mmap_size=268435456")  # 256MB
            except sqlite3.Error:
                pass
            self._local.hot = c
        return c

    # ---------- metadata
    def refresh(self, force=False):
        if not force and time.time() - self._meta_at < 15:  # was 30; markets-faster
            return
        c = self.deep()
        cols = [d[0] for d in c.execute("SELECT * FROM markets LIMIT 0").description]
        # Build + overlay offline, then swap pointers — never publish deep-only last_time to concurrent readers
        # (markets-faster 2026-10-23: torn refresh made /api/public/markets flip to ~days-old ages mid-cycle).
        markets = {r[0]: dict(zip(cols, r)) for r in c.execute("SELECT * FROM markets").fetchall()}
        pools = {}
        for token, pool, quote, qkind, qsym, usd, swaps, rk in c.execute(
                'SELECT token, pool, "quote", qkind, quote_symbol, usd_volume, swaps, rk FROM market_pools ORDER BY token, rk').fetchall():
            pools.setdefault(token, []).append({"pool": f"{self.chain}:{pool}", "address": pool, "quote": qsym, "quoteToken": quote,
                                                "usdVolume": usd, "swaps": swaps, "rank": rk})
        info = dict(c.execute("SELECT key, value FROM info").fetchall())
        tables = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        firsts = dict(c.execute("SELECT token, min(time) FROM token_bars WHERE period = 86400 GROUP BY token").fetchall())
        self._overlay_into(markets, info)
        self._markets = markets
        self.pools = pools
        self.pool_index = {p["address"]: (t, p) for t, ps in pools.items() for p in ps}
        self._info = info
        self.tables = tables
        self.firsts = firsts
        self._meta_at = time.time()

    def _overlay(self):
        """Latest price / 24 h change / 24 h volume from the hot store where it is newer than the deep build."""
        self._overlay_into(self._markets, self._info)

    def _overlay_into(self, markets, info=None):
        """Latest price / 24 h change / 24 h volume from the hot store where it is newer than the deep build."""
        h = self.hot()
        if h is None:
            return
        if info is None:
            info = self._info
        cut = int(info.get("data_until_ms") or 0) if info else self.deep_until_ms()
        now_s = int(time.time())
        last = {p[6:]: (cl, t) for p, cl, t in h.execute(
            "SELECT pool, close, max(time) FROM bars WHERE interval=60 AND pool LIKE 'token:%' GROUP BY pool").fetchall()}
        vol = dict((p[6:], v) for p, v in h.execute(
            "SELECT pool, sum(quote_volume) FROM bars WHERE interval=60 AND pool LIKE 'token:%' AND time > ? GROUP BY pool",
            (now_s - 86400,)).fetchall())
        ago = {p[6:]: cl for p, cl, _ in h.execute(
            "SELECT pool, close, max(time) FROM bars WHERE interval=60 AND pool LIKE 'token:%' AND time <= ? GROUP BY pool",
            (now_s - 86400,)).fetchall()}
        for m in markets.values():
            hit = last.get(m["token"])
            if hit and hit[1] * 1000 > (m["last_time"] or 0):
                m["last_price"], m["last_time"] = hit[0], hit[1] * 1000
                base = ago.get(m["token"])
                if base is None and m.get("last_price") is not None:
                    base = self._deep_close_before(m["token"], (now_s - 86400) * 1000)
                if base:
                    m["change_24h"] = hit[0] / base - 1
                deep_part = 0.0
                if cut > (now_s - 86400) * 1000:
                    r = self.deep().execute("SELECT sum(volume) FROM token_bars WHERE token = ? AND period = 3600 AND time >= ?",
                                            [m["token"], (now_s - 86400) * 1000]).fetchone()
                    deep_part = r[0] or 0.0
                m["volume_24h"] = deep_part + (vol.get(m["token"]) or 0.0)

    def _deep_close_before(self, token, t_ms):
        r = self.deep().execute("SELECT close FROM token_bars WHERE token = ? AND period = 3600 AND time <= ? ORDER BY time DESC LIMIT 1",
                                [token, t_ms]).fetchone()
        return r[0] if r else None

    def info(self):
        self.refresh()
        out = dict(self._info)
        h = self.hot()
        if h is not None:
            out["hot"] = dict(h.execute("SELECT key, value FROM state").fetchall())
        return out

    def deep_until_ms(self):
        return int(self._info.get("data_until_ms", 0))

    def data_until_ms(self):
        d = self.deep_until_ms()
        h = self.hot()
        if h is not None:
            r = h.execute("SELECT value FROM state WHERE key='data_until_ms'").fetchone()
            if r and r[0]:
                d = max(d, int(r[0]))
        return d

    @property
    def data_until(self):
        return self.data_until_ms()

    def data_version(self):
        """Changes whenever the hot store commits or the deep DB is rebuilt."""
        h = self.hot()
        hv = 0
        if h is not None:
            r = h.execute("SELECT value FROM state WHERE key='data_version'").fetchone()
            hv = int(r[0]) if r else 0
        return int(self._info.get("built_at", 0)) * 1_000_000 + hv

    @property
    def revision(self):
        return self.data_version()

    @property
    def markets_by_id(self):
        self.refresh()
        return self._markets

    def markets(self, kind=None):
        self.refresh()
        return [m for m in self._markets.values() if kind is None or m["kind"] == kind]

    def market(self, key):
        self.refresh()
        k = (key or "").strip()
        if ":" in k:
            ch, k = k.split(":", 1)
            if ch != self.chain:
                return None
        if k.lower().startswith("0x"):
            return self._markets.get(f"{self.chain}:{k.lower()}")
        c = [m for m in self._markets.values() if (m["symbol"] or "").lower() == k.lower()]
        c.sort(key=lambda m: (m["kind"] != "stock", m["kind"] != "anchor", -(m["usd_volume"] or 0)))
        return c[0] if c else None

    def resolution_starts(self):
        until = self.data_until_ms()
        names = {60: "1m", 300: "5m", 900: "15m", 3600: "1h", 14400: "4h", 86400: "1d"}
        return {names[s]: (None if RETENTION[s] is None else until - RETENTION[s] * 1000) for s in STORED}

    # ---------- series
    def bars(self, series, key, period_s, before_ms=None, limit=600):
        """series: 'token' (key = token 0x address) or 'pool' (key = pool 0x id). Returns (bars, has_more)."""
        bases = [s for s in STORED if period_s % s == 0]
        if not bases:
            raise ValueError("Intervals start at 1m.")
        base = max(bases)
        factor = period_s // base
        keep = RETENTION[base]
        until = self.data_until_ms()
        since = 0 if keep is None else interval_start(until - keep * 1000, period_s * 1000)
        cut = self.deep_until_ms() + 60000  # deep minutes end before this; the hot store carries the rest
        table, col = ("token_bars", "token") if series == "token" else ("pool_bars", "pool")
        need = limit * factor + factor
        deep_before = cut if before_ms is None else min(int(before_ms), cut)
        c = self.deep()
        rows = c.execute(f"""SELECT time, open, high, low, close, volume, base_volume, count FROM {table}
                             WHERE {col} = ? AND period = ? AND time >= ? AND time < ? ORDER BY time DESC LIMIT ?""",
                         [key, base, since, deep_before, need]).fetchall()
        rows.reverse()
        cut_by_limit = len(rows) == need
        h = self.hot()
        if h is not None and (before_ms is None or int(before_ms) > cut):
            hk = ("token:" + key) if series == "token" else key
            hi = int(before_ms) // 1000 if before_ms else 2 ** 62
            rows += [(t * 1000, o, hh, l, cl, qv, bv, n) for t, o, hh, l, cl, qv, bv, n in h.execute(
                "SELECT time, open, high, low, close, quote_volume, base_volume, count FROM bars "
                "WHERE pool=? AND interval=60 AND time>=? AND time<? ORDER BY time", (hk, cut // 1000, hi)).fetchall()]
        ms = period_s * 1000
        out = []
        for t, o, hh, l, cl, v, bv, n in rows:
            b = interval_start(t, ms)
            if out and out[-1]["time"] == b:
                x = out[-1]
                x["high"], x["low"], x["close"] = max(x["high"], hh), min(x["low"], l), cl
                x["volume"] += v or 0
                x["baseVolume"] += bv or 0
                x["count"] += n or 0
            else:
                out.append({"time": b, "open": o, "high": hh, "low": l, "close": cl, "volume": v or 0.0,
                            "baseVolume": bv or 0.0, "count": n or 0})
        if cut_by_limit and len(out) > 1:
            out = out[1:]  # earliest bucket may be cut by the row limit
        out = [x for x in out if all(x[k] == x[k] and abs(x[k]) != float("inf") for k in ("open", "high", "low", "close"))
               and x["low"] > 0][-limit:]
        more = False
        if out:
            more = c.execute(f"SELECT 1 FROM {table} WHERE {col} = ? AND period = ? AND time >= ? AND time < ? LIMIT 1",
                             [key, base, since, out[0]["time"]]).fetchone() is not None
        return out, more

    def tape(self, token, pool=None, before_s=None, limit=100):
        """Newest first. Rows: time (s), price, base_volume, usd, side, block, tx_index, log_index, tx, trader, pool, eligible."""
        out = []
        h = self.hot()
        if h is not None:
            q = ("SELECT f.time, f.price, f.base_volume, m.usd, m.side, f.slot, f.tx_order, f.event_order, m.tx, m.trader, f.pool, m.eligible "
                 "FROM fill_meta m JOIN fills f ON f.identity = m.identity WHERE m.token = ?")
            args = [token]
            if pool:
                q += " AND f.pool = ?"
                args.append(pool)
            if before_s:
                q += " AND f.time < ?"
                args.append(int(before_s))
            q += " ORDER BY f.slot DESC, f.event_order DESC LIMIT ?"
            out = [dict(zip(("time", "price", "base_volume", "usd", "side", "block", "tx_index", "log_index", "tx", "trader", "pool",
                             "eligible"), r)) for r in h.execute(q, args + [limit]).fetchall()]
        if len(out) < limit and "tape" in self.tables:
            low = min((r["block"] for r in out), default=None)
            args = [token]
            cond = ""
            if pool:
                cond += " AND pool = ?"
                args.append(pool)
            if before_s:
                cond += " AND time < ?"
                args.append(int(before_s))
            if low is not None:
                cond += " AND bn < ?"
                args.append(low)
            for t, bn, txi, li, txh, trader, price, bv, usd, side, el, pl in self.deep().execute(
                    f"""SELECT time, bn, tx_index, log_index, tx_hash, trader, price, base_volume, usd, side, eligible, pool FROM tape
                        WHERE token = ?{cond} ORDER BY bn DESC, log_index DESC LIMIT ?""", args + [limit - len(out)]).fetchall():
                out.append({"time": t, "price": price, "base_volume": bv, "usd": usd, "side": side, "block": bn, "tx_index": txi,
                            "log_index": li, "tx": "0x" + txh.hex() if txh else None, "trader": "0x" + trader.hex() if trader else None,
                            "pool": pl, "eligible": bool(el)})
        return out
