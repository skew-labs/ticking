#!/usr/bin/env python3
"""tip_hot 1s for ALL hot Arb+RH tokens — WRITE-ISOLATED into bars_1s_onchain.sqlite3.

Approach A (2026-10-24): never write the live light/FF DB (bars_1s.sqlite3).
Light path (em-sec-bars-1s / bn-ws / fastfeed-ingest) owns tip latency.
:8895 sec_bars_read merges light (preferred) + onchain coverage at read time.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(os.environ.get("SEC_BARS_ROOT", "/srv/skew/em-realtime/sec-bars"))
# Isolated onchain DB — do NOT default to bars_1s.sqlite3
DB_PATH = Path(
    os.environ.get(
        "SEC_ONCHAIN_DB",
        os.environ.get("SEC_BARS_ONCHAIN_DB", str(ROOT / "out" / "bars_1s_onchain.sqlite3")),
    )
)
HOT_ROOT = Path(os.environ.get("EM_HOT_ROOT", "/srv/skew/em-charts/hot"))
STATE_PATH = Path(os.environ.get("SEC_ONCHAIN_STATE", str(ROOT / "out" / "onchain_state.json")))
STATUS_PATH = Path(os.environ.get("SEC_ONCHAIN_STATUS", str(ROOT / "out" / "onchain_status.json")))
CHAINS = tuple(filter(None, os.environ.get("SEC_ONCHAIN_CHAINS", "arbitrum,robinhood").split(",")))
POLL_S = float(os.environ.get("SEC_ONCHAIN_POLL_S", "1.0"))
KEEP_S = int(os.environ.get("SEC_ONCHAIN_KEEP_S", str(7 * 86400)))
HOT_LOOKBACK_S = int(os.environ.get("SEC_ONCHAIN_HOT_LOOKBACK_S", str(KEEP_S)))
FULL_SYNC_EVERY = int(os.environ.get("SEC_ONCHAIN_FULL_SYNC_EVERY", "45"))
FLAT_MAX_S = int(os.environ.get("SEC_ONCHAIN_FLAT_MAX_S", "30"))
TOKEN_BATCH = int(os.environ.get("SEC_ONCHAIN_TOKEN_BATCH", "80"))
COMMIT_EVERY = int(os.environ.get("SEC_ONCHAIN_COMMIT_EVERY", "5"))  # short txns
SOURCE = "tip_hot"


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA wal_autocheckpoint=1000")
    con.execute(
        """CREATE TABLE IF NOT EXISTS bars_1s (
            market TEXT NOT NULL, time INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
            volume REAL NOT NULL, quote_volume REAL NOT NULL, count INTEGER NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY(market, time))"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS bars_1s_market_time ON bars_1s(market, time DESC)")
    return con


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"chain_cursors": {}, "cycles": 0, "token_offset": {}}


def save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE_PATH)


def hot_path(chain: str) -> Path | None:
    p = HOT_ROOT / chain / "history.sqlite3"
    return p if p.exists() else None


TOKEN_CACHE_TTL_S = int(os.environ.get("SEC_ONCHAIN_TOKEN_CACHE_TTL_S", "900"))
LIGHT_DB = Path(os.environ.get("SEC_BARS_DB", str(ROOT / "out" / "bars_1s.sqlite3")))
_token_mem: dict[str, tuple[float, list[str]]] = {}


def _token_cache_path(chain: str) -> Path:
    return ROOT / "out" / f"hot_tokens_{chain}.json"


def _tokens_from_bars_db(path: Path, chain: str) -> set[str]:
    if not path.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=5000")
        rows = con.execute(
            "SELECT DISTINCT lower(market) FROM bars_1s WHERE market LIKE ?",
            (f"{chain}:%",),
        ).fetchall()
        con.close()
        out = set()
        for (m,) in rows:
            if not m or ":" not in m:
                continue
            tok = m.split(":", 1)[1]
            if tok and len(tok) > 10:
                out.add(tok)
        return out
    except Exception:
        return set()


def _tokens_from_recent_fills(chain: str, since_s: int) -> set[str]:
    path = hot_path(chain)
    if not path:
        return set()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=15000")
        rows = con.execute(
            """
            SELECT DISTINCT lower(m.token)
            FROM fills f
            JOIN fill_meta m ON m.identity = f.identity
            WHERE f.time >= ?
              AND m.token IS NOT NULL AND length(m.token) > 10
            """,
            (int(since_s),),
        ).fetchall()
        con.close()
        zero = "0x" + ("0" * 40)
        return {t for (t,) in rows if t and t != zero}
    except Exception:
        return set()


def list_hot_tokens(chain: str, force_refresh: bool = False) -> list[str]:
    """Cached hot token list. Avoids full fill_meta DISTINCT every cycle (disk D-state)."""
    now = time.time()
    mem = _token_mem.get(chain)
    if not force_refresh and mem and now - mem[0] < TOKEN_CACHE_TTL_S:
        return mem[1]
    cache_p = _token_cache_path(chain)
    if not force_refresh and cache_p.exists():
        try:
            obj = json.loads(cache_p.read_text())
            if now - float(obj.get("at") or 0) < TOKEN_CACHE_TTL_S and obj.get("tokens"):
                toks = [t for t in obj["tokens"] if t and len(t) > 10]
                _token_mem[chain] = (now, toks)
                return toks
        except Exception:
            pass
    # Bootstrap cheap: light + onchain bars markets + recent 6h fills (indexed by time)
    toks_set = set()
    toks_set |= _tokens_from_bars_db(LIGHT_DB, chain)
    toks_set |= _tokens_from_bars_db(DB_PATH, chain)
    toks_set |= _tokens_from_recent_fills(chain, int(now) - 900)  # 15m only — avoid disk thrash
    # Optional rare full DISTINCT (only when forced or cache empty after bootstrap)
    if force_refresh or len(toks_set) < 10:
        path = hot_path(chain)
        if path:
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                con.execute("PRAGMA busy_timeout=20000")
                rows = con.execute(
                    "SELECT DISTINCT lower(token) FROM fill_meta WHERE token IS NOT NULL AND length(token)>10"
                ).fetchall()
                con.close()
                zero = "0x" + ("0" * 40)
                toks_set |= {t for (t,) in rows if t and t != zero}
            except Exception:
                pass
    toks = sorted(toks_set)
    _token_mem[chain] = (now, toks)
    try:
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": now, "n": len(toks), "tokens": toks}))
        tmp.replace(cache_p)
    except Exception:
        pass
    return toks


def scan_fills_recent(chain: str, since_s: int) -> dict[str, list]:
    path = hot_path(chain)
    if not path:
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=15000")
    try:
        rows = con.execute(
            """
            SELECT lower(m.token), f.time, f.price, f.base_volume, coalesce(m.usd,0)
            FROM fills f
            JOIN fill_meta m ON m.identity = f.identity
            WHERE f.time >= ?
              AND m.token IS NOT NULL AND length(m.token) > 10
              AND f.price IS NOT NULL AND f.price > 0
            ORDER BY f.time ASC
            """,
            (int(since_s),),
        ).fetchall()
    finally:
        con.close()
    by: dict[str, list] = defaultdict(list)
    zero = "0x" + ("0" * 40)
    for tok, t, px, bv, usd in rows:
        if not tok or tok == zero:
            continue
        by[tok].append((int(t), float(px), float(bv or 0), float(usd or 0)))
    return by


def scan_token_fills(chain: str, tok: str, since_s: int) -> list:
    path = hot_path(chain)
    if not path:
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=10000")
    try:
        rows = con.execute(
            """
            SELECT f.time, f.price, f.base_volume, coalesce(m.usd,0)
            FROM fill_meta m
            JOIN fills f ON f.identity = m.identity
            WHERE lower(m.token) = ? AND f.time >= ? AND f.price > 0
            ORDER BY f.time ASC
            """,
            (tok, int(since_s)),
        ).fetchall()
        if not rows:
            rows = con.execute(
                """
                SELECT f.time, f.price, f.base_volume, coalesce(m.usd,0)
                FROM fill_meta m
                JOIN fills f ON f.identity = m.identity
                WHERE lower(m.token) = ? AND f.price > 0
                ORDER BY f.time DESC LIMIT 50
                """,
                (tok,),
            ).fetchall()
            rows = list(reversed(rows))
    finally:
        con.close()
    return [(int(t), float(px), float(bv or 0), float(usd or 0)) for t, px, bv, usd in rows]


def bucket_1s(fills: list) -> list[tuple]:
    by: dict[int, list] = defaultdict(list)
    for t, px, bv, usd in fills:
        by[int(t)].append((px, bv, usd))
    out = []
    for t in sorted(by):
        xs = by[t]
        o, c = xs[0][0], xs[-1][0]
        h, l = max(x[0] for x in xs), min(x[0] for x in xs)
        out.append((t * 1000, o, h, l, c, sum(x[1] for x in xs), sum(x[2] for x in xs), len(xs)))
    return out


def upsert_tip_hot(con, market_id: str, bars: list[tuple]) -> int:
    if not bars:
        return 0
    payload = [(market_id, t, o, h, l, c, v, qv, n, SOURCE) for t, o, h, l, c, v, qv, n in bars]
    con.executemany(
        """INSERT INTO bars_1s(market,time,open,high,low,close,volume,quote_volume,count,source)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(market,time) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
             volume=excluded.volume, quote_volume=excluded.quote_volume, count=excluded.count,
             source=excluded.source
           WHERE bars_1s.source NOT IN ('binance_1s','binance')
        """,
        payload,
    )
    return len(payload)


def fill_flat_tip(con, market_id: str) -> int:
    row = con.execute(
        """SELECT time, close FROM bars_1s
           WHERE market=? AND source IN ('tip_hot','binance_ws','binance_1s','binance')
           ORDER BY time DESC LIMIT 1""",
        (market_id,),
    ).fetchone()
    if not row:
        return 0
    last_t, last_c = int(row[0]), float(row[1])
    if last_c <= 0:
        return 0
    now_ot = int(time.time() * 1000)
    now_ot -= now_ot % 1000
    if now_ot - last_t > FLAT_MAX_S * 1000 or now_ot <= last_t:
        return 0
    t = last_t + 1000
    payload = []
    while t <= now_ot:
        payload.append((market_id, t, last_c, last_c, last_c, last_c, 0.0, 0.0, 0, "flat_carry"))
        t += 1000
    if not payload:
        return 0
    con.executemany(
        """INSERT INTO bars_1s(market,time,open,high,low,close,volume,quote_volume,count,source)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(market,time) DO NOTHING""",
        payload,
    )
    return len(payload)


def trim(con):
    cut = int(time.time() * 1000) - KEEP_S * 1000
    con.execute(
        "DELETE FROM bars_1s WHERE time < ? AND source IN ('tip_hot','flat_carry')",
        (cut,),
    )


def write_status(obj):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(obj, indent=2) + "\n")


def main():
    st = load_state()
    chain_cursors = st.setdefault("chain_cursors", {})
    token_offset = st.setdefault("token_offset", {})
    cycles = int(st.get("cycles") or 0)
    started_at = time.time()
    local_cycles = 0
    TIP_ONLY_BOOT_S = float(os.environ.get("SEC_ONCHAIN_TIP_ONLY_BOOT_S", "45"))
    while True:
        t0 = time.time()
        now = int(time.time())
        # tip-first: process-local boot window ignores persisted cycles so restart never
        # immediately hits full fill_meta / long lookback scans.
        boot = (time.time() - started_at) < TIP_ONLY_BOOT_S or local_cycles < 5
        warm = bool(chain_cursors)
        full = (not boot) and warm and (cycles % FULL_SYNC_EVERY == 0)
        stats = {
            "at": now,
            "full_sync": full,
            "db": str(DB_PATH),
            "isolate": "bars_1s_onchain",
            "chains": {},
            "wrote_bars": 0,
            "flat": 0,
            "markets_touched": 0,
            "err": None,
        }
        try:
            con = db()
            pending = 0
            for chain in CHAINS:
                wrote = flat = 0
                scanned = 0
                hot_n = 0
                if full:
                    # refresh cache at most when full cycle hits offset 0
                    force = int(token_offset.get(chain) or 0) == 0
                    toks = list_hot_tokens(chain, force_refresh=False)  # never full fill_meta DISTINCT
                    hot_n = len(toks)
                    since = now - HOT_LOOKBACK_S
                    off = int(token_offset.get(chain) or 0)
                    if off >= hot_n:
                        off = 0
                    batch = toks[off : off + TOKEN_BATCH]
                    if not batch and toks:
                        off = 0
                        batch = toks[:TOKEN_BATCH]
                    token_offset[chain] = off + len(batch)
                    batch_t0 = time.time()
                    FULL_BUDGET_S = float(os.environ.get("SEC_ONCHAIN_FULL_BUDGET_S", "8"))
                    for tok in batch:
                        if time.time() - batch_t0 > FULL_BUDGET_S:
                            break
                        fills = scan_token_fills(chain, tok, since)
                        if not fills:
                            continue
                        mid = f"{chain}:{tok}"
                        n = upsert_tip_hot(con, mid, bucket_1s(fills))
                        f = fill_flat_tip(con, mid)
                        wrote += n
                        flat += f
                        scanned += 1
                        pending += 1
                        if n or f:
                            stats["markets_touched"] += 1
                        if pending >= COMMIT_EVERY:
                            con.commit()
                            pending = 0
                    lookback_h = round((now - since) / 3600, 2)
                else:
                    # tip-only: never DISTINCT whole fill_meta; time-indexed recent fills only
                    cur = int(chain_cursors.get(chain) or (now - 120))
                    since = max(cur - 60, now - 180)  # tip window ≤3m, ≥60s overlap (hot lag ~15–30s)
                    by_tok = scan_fills_recent(chain, since)
                    hot_n = len(by_tok)
                    lookback_h = round((now - since) / 3600, 2)
                    for tok, fills in by_tok.items():
                        mid = f"{chain}:{tok}"
                        n = upsert_tip_hot(con, mid, bucket_1s(fills))
                        f = fill_flat_tip(con, mid)
                        wrote += n
                        flat += f
                        scanned += 1
                        pending += 1
                        if n or f:
                            stats["markets_touched"] += 1
                        if pending >= COMMIT_EVERY:
                            con.commit()
                            pending = 0
                    for (mid,) in con.execute(
                        "SELECT DISTINCT market FROM bars_1s WHERE market LIKE ? AND source='tip_hot' AND time>=?",
                        (f"{chain}:%", (now - 120) * 1000),
                    ):
                        flat += fill_flat_tip(con, mid)
                        pending += 1
                        if pending >= COMMIT_EVERY:
                            con.commit()
                            pending = 0
                chain_cursors[chain] = now
                stats["chains"][chain] = {
                    "hot_tokens": hot_n,
                    "scanned_tokens": scanned,
                    "wrote_1s": wrote,
                    "flat": flat,
                    "since_s": since,
                    "lookback_h": lookback_h,
                    "token_offset": int(token_offset.get(chain) or 0),
                }
                stats["wrote_bars"] += wrote
                stats["flat"] += flat
                con.commit()
                pending = 0
            if cycles % 90 == 0:
                trim(con)
                con.commit()
            con.close()
            st["chain_cursors"] = chain_cursors
            st["token_offset"] = token_offset
            st["cycles"] = cycles + 1
            st["updated"] = now
            st["db"] = str(DB_PATH)
            save_state(st)
        except Exception as e:
            stats["err"] = str(e)[:400]
        stats["cycle_ms"] = int((time.time() - t0) * 1000)
        write_status(stats)
        cycles += 1
        local_cycles += 1
        time.sleep(max(0.15, POLL_S - (time.time() - t0)))


if __name__ == "__main__":
    main()
