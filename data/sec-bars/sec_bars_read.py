#!/usr/bin/env python3
"""Read Binance-mirrored 1s bars for em-charts serve path.

Approach A merge (2026-10-24): light/FF DB (bars_1s.sqlite3) is tip-latency SoT for
mapped majors; onchain tip_hot lives in bars_1s_onchain.sqlite3. fetch_1s prefers
light solid sources per second, then fills coverage from onchain without locking
the hot writer DB.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DB = Path(os.environ.get("SEC_BARS_DB", "/srv/skew/em-realtime/sec-bars/out/bars_1s.sqlite3"))
ONCHAIN_DB = Path(
    os.environ.get(
        "SEC_ONCHAIN_DB",
        os.environ.get("SEC_BARS_ONCHAIN_DB", "/srv/skew/em-realtime/sec-bars/out/bars_1s_onchain.sqlite3"),
    )
)
MAP = Path(os.environ.get("SEC_BARS_MAP", "/srv/skew/em-realtime/sec-bars/map.json"))

# OHLC paint prefers these; flat_carry/oracle_fast only fill holes (no fake wicks).
SOLID = frozenset({"binance_1s", "binance", "fastfeed", "tip_hot", "binance_ws"})

# Higher = preferred when both DBs have the same second.
SRC_RANK = {
    "binance_1s": 100,
    "binance": 100,
    "binance_ws": 85,
    "fastfeed": 75,
    "tip_hot": 50,
    "flat_carry": 10,
    "oracle_fast": 5,
}

_meta = {"at": 0.0, "by_id": {}, "by_sym": {}}


def _load_map():
    import json
    if time.time() - _meta["at"] < 30 and _meta["by_id"]:
        return _meta
    m = json.loads(MAP.read_text())
    by_id, by_sym = {}, {}
    for spec in m.get("symbols", {}).values():
        for mk in spec.get("markets", []):
            mid = (mk.get("id") or "").lower()
            if not mid:
                continue
            by_id[mid] = mk
            chain = mk.get("chain")
            if not chain and ":" in mid:
                chain = mid.split(":", 1)[0]
            if chain:
                by_sym[(chain, (mk.get("symbol") or "").upper())] = mk
    _meta.update(at=time.time(), by_id=by_id, by_sym=by_sym, policy=m.get("policy"), empty=m.get("empty_second"))
    return _meta


def _db_has(path: Path, market_id: str) -> bool:
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=3000")
        row = con.execute("SELECT 1 FROM bars_1s WHERE market=? LIMIT 1", (market_id,)).fetchone()
        con.close()
        return row is not None
    except Exception:
        return False


def _has_bars(market_id: str) -> bool:
    return _db_has(DB, market_id) or _db_has(ONCHAIN_DB, market_id)


def resolve_sec_market(network: str | None, mint: str | None, market: str | None):
    """Return market id for sec-bars: BN map OR any Arb/RH id that has tip_hot/binance bars."""
    meta = _load_map()
    cand = (market or mint or "").strip()
    if not cand:
        return None, None
    if ":" not in cand and network:
        cand = f"{network}:{cand}"
    key = cand.lower() if ":" in cand else None
    if key and key in meta["by_id"]:
        mk = meta["by_id"][key]
        return mk["id"], mk
    if network and ":" not in (market or mint or ""):
        key = f"{network}:{(market or mint)}".lower()
        if key in meta["by_id"]:
            mk = meta["by_id"][key]
            return mk["id"], mk
    # Full Arb/RH coverage: serve when bars exist (tip_hot writer) even if not CEX-mapped
    if key and ":" in key:
        chain, addr = key.split(":", 1)
        if chain in ("arbitrum", "robinhood") and addr.startswith("0x") and len(addr) == 42:
            mid = f"{chain}:{addr}"
            if _has_bars(mid) or _has_bars(cand):
                use = mid if _has_bars(mid) else cand
                return use, {"id": use, "symbol": addr[:8], "chain": chain, "source": "tip_hot"}
            return mid, {"id": mid, "symbol": addr[:8], "chain": chain, "source": "tip_hot_pending"}
    return None, None


def _is_solid(src: str | None) -> bool:
    return (src or "") in SOLID


def _rollup(rows: list[tuple], period_s: int) -> list[dict]:
    """UTC-floor buckets. OHLC from solid rows when any exist; volume from all."""
    ms = period_s * 1000
    order: list[int] = []
    buckets: dict[int, list] = {}
    for row in rows:
        t = int(row[0])
        b = (t // ms) * ms
        if b not in buckets:
            buckets[b] = []
            order.append(b)
        buckets[b].append(row)

    out = []
    prev_solid_c = None
    for b in order:
        group = buckets[b]
        solid = [r for r in group if _is_solid(r[8])]
        use = solid if solid else group
        if not solid and prev_solid_c and prev_solid_c > 0:
            o = h = l = c = float(prev_solid_c)
            src = "flat_carry"
        else:
            o = float(use[0][1])
            h = max(float(r[2]) for r in use)
            l = min(float(r[3]) for r in use)
            c = float(use[-1][4])
            src = use[-1][8]
            if solid:
                prev_solid_c = c
        vol = float(sum(r[5] or 0 for r in group))
        qv = float(sum(r[6] or 0 for r in group))
        n = int(sum(r[7] or 0 for r in group))
        out.append(
            {
                "time": b,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": vol,
                "baseVolume": vol,
                "quoteVolume": qv,
                "count": n,
                "source": src,
                "synthetic": False,
            }
        )
    return out


def _fetch_rows(path: Path, market_id: str, hi: int, need: int) -> list[tuple]:
    if not path.exists() or need <= 0:
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=5000")
        rows = con.execute(
            """SELECT time, open, high, low, close, volume, quote_volume, count, source
               FROM bars_1s WHERE market=? AND time<? ORDER BY time DESC LIMIT ?""",
            (market_id, hi, need),
        ).fetchall()
        con.close()
        return rows
    except Exception:
        return []


def _merge_rows(light: list[tuple], onchain: list[tuple]) -> list[tuple]:
    """Prefer light/FF solid sources per second; keep tip_hot coverage from onchain."""
    by_t: dict[int, tuple] = {}
    for r in onchain:
        by_t[int(r[0])] = r
    for r in light:
        t = int(r[0])
        prev = by_t.get(t)
        if prev is None or SRC_RANK.get(r[8], 0) >= SRC_RANK.get(prev[8], 0):
            by_t[t] = r
    return [by_t[t] for t in sorted(by_t)]


def _has_earlier(path: Path, market_id: str, t: int) -> bool:
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=3000")
        row = con.execute(
            "SELECT 1 FROM bars_1s WHERE market=? AND time<? LIMIT 1",
            (market_id, t),
        ).fetchone()
        con.close()
        return row is not None
    except Exception:
        return False


def fetch_1s(market_id: str, period_s: int, before_ms=None, limit=600):
    """Return (bars, has_more) aggregated from light + onchain 1s stores. period_s in 1..60."""
    if period_s < 1 or period_s > 60:
        raise ValueError("sec-bars serve 1s..60s only")
    if not DB.exists() and not ONCHAIN_DB.exists():
        return [], False
    need = limit * period_s + period_s
    hi = int(before_ms) if before_ms else 2**62
    light = _fetch_rows(DB, market_id, hi, need)
    onchain = _fetch_rows(ONCHAIN_DB, market_id, hi, need)
    merged_desc = sorted(_merge_rows(light, onchain), key=lambda r: r[0], reverse=True)
    cut = len(merged_desc) >= need
    rows = list(reversed(merged_desc[:need]))
    out = _rollup(rows, period_s)
    if cut and len(out) > 1:
        out = out[1:]
    out = out[-limit:]
    more = False
    if out:
        t0 = out[0]["time"]
        more = _has_earlier(DB, market_id, t0) or _has_earlier(ONCHAIN_DB, market_id, t0)
    return out, more
