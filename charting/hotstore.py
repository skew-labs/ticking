"""Hot suffix store for Ticking's EvmHistory: /srv/skew/em-charts/hot/<chain>/history.sqlite3 (WAL).

Tables `fills` and `bars` use exactly the DDL of Ticking's observed_projector.hpp. Mapping for EVM chains:
  fills.identity = '<0x tx hash>:<log index>'   fills.pool = pool key (below)
  fills.time     = block timestamp, seconds (minute-floor when the source chunk carries no block times)
  fills.slot = block number, tx_order = tx index, event_order = log index
  fills.price    = USD per base token (quote leg converted with the same-minute anchor rate)
  fills.base_volume = base token amount, quote_volume = USD
  bars           = interval 60 / 300 / 3600 (seconds), time = bucket start in seconds,
                   first_order/last_order = '%020d:%010d:%06d:<identity>' (block : tx : log : identity)
Pool keys: '0x<pool address or V4 pool id>' for a DEX pool, 'token:0x<token>' for the token reference composite
(em_pool_reference_v2, same series as the deep DuckDB token_bars). The composite has bars but no fills.
Extra tables (not in Ticking's DDL): fill_meta (token, side, trader, tx, usd per fill identity), pools (pool ->
token, quote), state (data_version, cursor, data_until_ms, hot_days). `state.data_version` increments on every
commit; SQLite's own PRAGMA data_version also changes for other connections.
"""
import os
import sqlite3
import time

from common import ROOT

HOT_DAYS = 14
INTERVALS = (60, 300, 3600)
DDL = """
CREATE TABLE IF NOT EXISTS fills(identity TEXT PRIMARY KEY,pool TEXT,time INTEGER,slot INTEGER,tx_order INTEGER,event_order INTEGER,price REAL,base_volume REAL,quote_volume REAL);
CREATE INDEX IF NOT EXISTS ftime ON fills(pool,time);
CREATE INDEX IF NOT EXISTS fslot ON fills(slot);
CREATE INDEX IF NOT EXISTS ford ON fills(pool,slot,tx_order,event_order,identity);
CREATE INDEX IF NOT EXISTS reference_time ON fills(time,slot,tx_order,event_order,identity);
CREATE TABLE IF NOT EXISTS bars(pool TEXT,interval INTEGER,time INTEGER,open REAL,high REAL,low REAL,close REAL,base_volume REAL,quote_volume REAL,count INTEGER,first_order TEXT,last_order TEXT,PRIMARY KEY(pool,interval,time));
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS fill_meta(identity TEXT PRIMARY KEY,token TEXT,side TEXT,trader TEXT,tx TEXT,usd REAL,eligible INTEGER);
CREATE INDEX IF NOT EXISTS fm_token ON fill_meta(token);
CREATE INDEX IF NOT EXISTS fm_trader ON fill_meta(trader,token);
CREATE TABLE IF NOT EXISTS pools(pool TEXT PRIMARY KEY,token TEXT,quote TEXT);
"""


def path(chain):
    return f"{ROOT}/hot/{chain}/history.sqlite3"


def order(slot, tx, log, identity):
    return "%020d:%010d:%06d:%s" % (slot, tx, log, identity)


class HotWriter:
    """Single writer (the live tail). Every batch is one transaction ending in a data_version bump."""

    def __init__(self, chain):
        self.chain = chain
        os.makedirs(os.path.dirname(path(chain)), exist_ok=True)
        self.db = sqlite3.connect(path(chain), timeout=30, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # chart-cache 2026-10-23
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.execute("PRAGMA cache_size=-65536")  # ~64MB writer
        self.db.execute("PRAGMA mmap_size=268435456")
        self.db.executescript(DDL)
        self.db.execute("INSERT OR IGNORE INTO state VALUES('data_version','0')")
        self.db.execute("INSERT OR REPLACE INTO state VALUES('hot_days',?)", (str(HOT_DAYS),))

    def state(self, key, default=None):
        r = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def write(self, fills, bars, pools, cursor, data_until_ms):
        """fills: (identity, pool, time, slot, tx_order, event_order, price, base_volume, quote_volume, token, side, trader, tx, usd, eligible)
        bars: (pool, interval, time, open, high, low, close, base_volume, quote_volume, count, first_order, last_order), replacing
        the bucket. pools: (pool, token, quote)."""
        c = self.db
        c.execute("BEGIN IMMEDIATE")
        try:
            c.executemany("INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?,?,?,?)", [f[:9] for f in fills])
            c.executemany("INSERT OR REPLACE INTO fill_meta VALUES(?,?,?,?,?,?,?)", [(f[0],) + tuple(f[9:15]) for f in fills])
            c.executemany("INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", bars)
            c.executemany("INSERT OR IGNORE INTO pools VALUES(?,?,?)", pools)
            cut = int(time.time()) - HOT_DAYS * 86400
            c.execute("DELETE FROM fill_meta WHERE identity IN (SELECT identity FROM fills WHERE time < ?)", (cut,))
            c.execute("DELETE FROM fills WHERE time < ?", (cut,))
            c.execute("DELETE FROM bars WHERE time < ?", (cut - 3600,))
            c.execute("INSERT OR REPLACE INTO state VALUES('cursor',?)", (str(cursor),))
            c.execute("INSERT OR REPLACE INTO state VALUES('data_until_ms',?)", (str(data_until_ms),))
            c.execute("UPDATE state SET value = CAST(value AS INTEGER) + 1 WHERE key='data_version'")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def reader(chain):
    """Read-only connection (WAL readers never block the writer)."""
    p = path(chain)
    if not os.path.exists(p):
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    try:
        c.execute("PRAGMA query_only=ON")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA cache_size=-131072")
        c.execute("PRAGMA mmap_size=268435456")
    except sqlite3.Error:
        pass
    return c
