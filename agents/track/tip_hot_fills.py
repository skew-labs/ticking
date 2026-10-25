#!/usr/bin/env python3
"""Arb/RH tip→hot history.sqlite3 fills → transfer-like rows for track paint.

Layout on t1: /srv/skew/em-charts/hot/{chain}/history.sqlite3
  fills(identity, pool, time, …)  time = unix seconds
  fill_meta(identity, token, side, trader, tx, usd, eligible)
Join on identity. side A = sell/out, B = buy/in.
Solana: same if hot/solana exists; else honest missing.
Never invent bridges/CEX from fills alone.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

HOT_ROOT = Path(os.environ.get("EM_HOT_ROOT", "/srv/skew/em-charts/hot"))
CHAINS_DEFAULT = ("arbitrum", "robinhood", "solana")


def _nz(x) -> float:
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else 0.0
    except Exception:
        return 0.0


def _db_for(chain: str) -> Path | None:
    p = HOT_ROOT / chain / "history.sqlite3"
    return p if p.exists() else None


def fills_for_address(
    address: str,
    *,
    chains: list[str] | None = None,
    window_from_ms: int | None = None,
    window_to_ms: int | None = None,
    limit: int = 500,
) -> tuple[list[dict], dict]:
    addr = (address or "").lower()
    if not (addr.startswith("0x") and len(addr) == 42):
        # solana base58 possible later
        pass
    want = list(chains or CHAINS_DEFAULT)
    t1 = window_to_ms or int(time.time() * 1000)
    t0 = window_from_ms or (t1 - 7 * 86400_000)
    t0_s, t1_s = t0 // 1000, t1 // 1000
    rows: list[dict] = []
    honesty: list[str] = []
    scanned = 0

    for ch in want:
        db = _db_for(ch)
        if not db:
            honesty.append(f"tip_hot_missing:{ch}")
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            q = """
            SELECT m.identity, m.token, m.side, m.trader, m.tx, m.usd, m.eligible,
                   f.time AS ts, f.price, f.base_volume, f.quote_volume, f.pool
            FROM fill_meta m
            JOIN fills f ON f.identity = m.identity
            WHERE lower(m.trader) = ?
              AND f.time >= ? AND f.time <= ?
            ORDER BY f.time DESC
            LIMIT ?
            """
            cur = con.execute(q, [addr, t0_s, t1_s, int(limit)])
            for r in cur:
                scanned += 1
                side = str(r["side"] or "").upper()
                ts_ms = int(r["ts"]) * 1000
                usd = _nz(r["usd"]) or _nz(r["quote_volume"])
                trader = (r["trader"] or "").lower()
                pool = (r["pool"] or "").lower() or ("0x" + "0" * 40)
                # sell A: trader → pool; buy B: pool → trader (pool = venue chip, not same-person)
                if side == "A":
                    frm, to = trader, pool
                else:
                    frm, to = pool, trader
                rows.append({
                    "ts": ts_ms,
                    "chain": ch,
                    "tx": r["tx"] or "",
                    "log_index": 0,
                    "from": frm,
                    "to": to,
                    "token": (r["token"] or "").lower(),
                    "amount": str(r["base_volume"] or ""),
                    "usd": usd or None,
                    "source": "tip_hot_sqlite",
                    "side": side,  # A=sell B=buy for paint kind
                    "market": f"{ch}:{(r['token'] or '')}",
                    "price": r["price"],
                })
            con.close()
        except Exception as e:
            honesty.append(f"tip_hot_read_error:{ch}:{type(e).__name__}")

        if len(rows) >= limit:
            break

    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    rows = rows[:limit]
    status = "ok" if rows else ("partial" if scanned == 0 and not honesty else "partial")
    if not rows and "tip_hot_no_fills_for_seed" not in honesty:
        honesty.append("tip_hot_no_fills_for_seed")
    return rows, {
        "source": "tip_hot_sqlite",
        "status": status,
        "n": len(rows),
        "honesty": honesty,
    }
