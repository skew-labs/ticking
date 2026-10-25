#!/usr/bin/env python3
"""Binance-style 1s OHLCV writer for Sol + Arb + RH (early paint / 초봉).

Binance 1s rules we mirror (verified 2026-10-23 against data-api.binance.vision):
  - open time = UTC floor to 1s (ms % 1000 == 0)
  - close time = open + 999 ms
  - contiguous seconds in serve window (gap always 1000 ms)
  - empty second: still emit bar; O=H=L=C = last close; volume=0; count=0 (flat carry)
  - trade in second: update O (first), H/L, C (last), volume, count

Primary tick source NOW: Binance REST 1s klines (exact shape). Optional: oracle-fast
last price as extra tick into current second for Sol (does not replace BN bars).
Fast Feed (Arb PGA) later: append ticks into same buckets (source=fastfeed).

SoT for dump/flow/history remains tip/hot on-chain. This store is latency paint only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(os.environ.get("SEC_BARS_ROOT", "/srv/skew/em-realtime/sec-bars"))
MAP_PATH = Path(os.environ.get("SEC_BARS_MAP", str(ROOT / "map.json")))
DB_PATH = Path(os.environ.get("SEC_BARS_DB", str(ROOT / "out" / "bars_1s.sqlite3")))
STATUS_PATH = Path(os.environ.get("SEC_BARS_STATUS", str(ROOT / "out" / "status.json")))
QUOTES_PATH = Path(os.environ.get("SEC_BARS_QUOTES", str(ROOT / "out" / "quotes.json")))
BN_BASE = os.environ.get("BINANCE_API", "https://data-api.binance.vision")
ORACLE_STATUS = Path(
    os.environ.get(
        "ORACLE_FAST_STATUS",
        "/srv/skew/solana-mingle-20261007/data/oracle-fast/status.json",
    )
)
POLL_S = float(os.environ.get("SEC_BARS_POLL_S", "0.5"))
KEEP_S = int(os.environ.get("SEC_BARS_KEEP_S", "7200"))  # 2h
LIMIT = int(os.environ.get("SEC_BARS_FETCH_LIMIT", "120"))


def load_map():
    return json.loads(MAP_PATH.read_text())


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS bars_1s (
            market TEXT NOT NULL,
            time INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            quote_volume REAL NOT NULL,
            count INTEGER NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY(market, time)
        )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS bars_1s_market_time ON bars_1s(market, time DESC)"
    )
    return con


def fetch_klines(symbol: str, limit: int = LIMIT):
    url = f"{BN_BASE}/api/v3/klines?symbol={symbol}&interval=1s&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "em-sec-bars/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def upsert_bn(con, market_id: str, rows):
    """Mirror Binance 1s kline row → our bars_1s (including empty flat-carry seconds)."""
    payload = []
    for r in rows:
        # BN: [ot,o,h,l,c,v,ct,qv,n,tb,tq,ignore]
        ot = int(r[0])
        o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        v, qv, n = float(r[5]), float(r[7]), int(r[8])
        payload.append((market_id, ot, o, h, l, c, v, qv, n, "binance_1s"))
    con.executemany(
        """INSERT INTO bars_1s(market,time,open,high,low,close,volume,quote_volume,count,source)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(market,time) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
             volume=excluded.volume, quote_volume=excluded.quote_volume, count=excluded.count,
             source=excluded.source
           WHERE bars_1s.source IN ('binance_1s','seed')
              OR excluded.count >= bars_1s.count""",
        payload,
    )



def fill_flat_to_now(con, market_id: str):
    """Binance empty-second policy for the live tip: emit contiguous flat bars up to current UTC second.

    Carry the last *Binance* close only — never oracle_fast tip (avoids ~30bps Sol flicker wicks).
    """
    row = con.execute(
        "SELECT time, close FROM bars_1s WHERE market=? AND source IN ('binance_1s','binance') "
        "ORDER BY time DESC LIMIT 1",
        (market_id,),
    ).fetchone()
    if not row:
        return
    last_t, last_c = int(row[0]), float(row[1])
    if last_c <= 0:
        return
    now_ot = int(time.time() * 1000)
    now_ot = now_ot - (now_ot % 1000)
    # do not invent more than 30s ahead of last BN (protect against stalled feed)
    if now_ot - last_t > 30_000 or now_ot <= last_t:
        return
    t = last_t + 1000
    payload = []
    while t <= now_ot:
        payload.append((market_id, t, last_c, last_c, last_c, last_c, 0.0, 0.0, 0, "flat_carry"))
        t += 1000
    if not payload:
        return
    con.executemany(
        """INSERT INTO bars_1s(market,time,open,high,low,close,volume,quote_volume,count,source)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(market,time) DO NOTHING""",
        payload,
    )

def apply_oracle_tick(con, market_id: str, price: float, t_ms: int | None = None):
    """Optional Sol tip into *current* UTC second only if Binance has not written it yet.

    Candle construction stays Binance-first (empty-second flat carry from BN klines).
    Oracle may advance the live tip by <1s when BN REST has not landed the open second.
    """
    if price <= 0:
        return
    wall = int(time.time() * 1000)
    ot = wall - (wall % 1000)  # always wall-clock current second
    row = con.execute(
        "SELECT open,high,low,close,volume,quote_volume,count,source FROM bars_1s WHERE market=? AND time=?",
        (market_id, ot),
    ).fetchone()
    if row is None:
        # seed current second only; BN upsert will replace on next poll
        con.execute(
            """INSERT INTO bars_1s(market,time,open,high,low,close,volume,quote_volume,count,source)
               VALUES(?,?,?,?,?,?,0,0,1,'oracle_fast')""",
            (market_id, ot, price, price, price, price),
        )
        return
    o, h, l, c, v, qv, n, src = row
    if src == "binance_1s":
        return  # never fight Binance OHLCV for that second
    nh, nl = max(h, price), min(l, price)
    con.execute(
        "UPDATE bars_1s SET high=?, low=?, close=?, count=count+1 WHERE market=? AND time=?",
        (nh, nl, price, market_id, ot),
    )


def trim(con):
    cut = int(time.time() * 1000) - KEEP_S * 1000
    con.execute("DELETE FROM bars_1s WHERE time < ?", (cut,))


def write_status(extra):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(extra, indent=2) + "\n")


def write_quotes(con, markets):
    """Latest close per market for banner / live tip (< seconds age)."""
    out = {"updated_ms": int(time.time() * 1000), "policy": "binance_1s_kline_mirror_v1", "quotes": {}}
    for mid in markets:
        r = con.execute(
            "SELECT time, close, source, count, volume FROM bars_1s WHERE market=? ORDER BY time DESC LIMIT 1",
            (mid,),
        ).fetchone()
        if not r:
            continue
        out["quotes"][mid] = {
            "t_ms": r[0],
            "price": r[1],
            "source": r[2],
            "count": r[3],
            "volume": r[4],
            "age_s": round(time.time() - r[0] / 1000, 3),
        }
    QUOTES_PATH.write_text(json.dumps(out) + "\n")


def main():
    m = load_map()
    con = db()
    markets = []
    oracle_map = {}  # oracle_id -> market_id
    cycles = 0
    while True:
        t0 = time.time()
        err = None
        try:
            specs = list(m["symbols"].items())
            fetched = {}
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(specs)))) as pool:
                futs = {pool.submit(fetch_klines, spec["binance"]): (key, spec) for key, spec in specs}
                for fut in as_completed(futs):
                    key, spec = futs[fut]
                    fetched[key] = (spec, fut.result())
            for key, (spec, rows) in fetched.items():
                for mk in spec["markets"]:
                    mid = mk["id"]
                    markets.append(mid)
                    upsert_bn(con, mid, rows)
                    fill_flat_to_now(con, mid)
                    if spec.get("oracle_id"):
                        oracle_map[spec["oracle_id"]] = mid
            # oracle-fast tip for Sol
            if ORACLE_STATUS.exists():
                try:
                    st = json.loads(ORACLE_STATUS.read_text())
                    for row in (st.get("last") or {}).get("rows") or []:
                        oid = row.get("id")
                        mid = oracle_map.get(oid)
                        if not mid:
                            continue
                        px = float(row["price"])
                        ts = int(row.get("observedAtMs") or row.get("receivedAtMs") or time.time() * 1000)
                        apply_oracle_tick(con, mid, px, ts)
                except Exception as e:
                    err = f"oracle:{e}"
            if cycles % 30 == 0:
                trim(con)
            con.commit()
            markets = sorted(set(markets))
            write_quotes(con, markets)
            # freshness sample
            sample = {}
            for mid in markets:
                r = con.execute(
                    "SELECT time, close, source FROM bars_1s WHERE market=? ORDER BY time DESC LIMIT 1",
                    (mid,),
                ).fetchone()
                if r:
                    sample[mid] = {"t_ms": r[0], "close": r[1], "source": r[2], "age_s": round(time.time() - r[0] / 1000, 3)}
            write_status(
                {
                    "at": time.time(),
                    "at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "cycles": cycles,
                    "poll_s": POLL_S,
                    "keep_s": KEEP_S,
                    "markets": markets,
                    "sample": sample,
                    "loop_ms": int((time.time() - t0) * 1000),
                    "err": err,
                    "role": "sec_bars_1s_binance_mirror",
                    "sot": "tip_hot_onchain",
                }
            )
        except Exception as e:
            write_status({"at": time.time(), "state": "error", "err": str(e), "cycles": cycles})
            try:
                con.rollback()
            except Exception:
                pass
        cycles += 1
        time.sleep(max(0.05, POLL_S - (time.time() - t0)))


if __name__ == "__main__":
    main()
