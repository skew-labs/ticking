#!/usr/bin/env python3
"""Arb Fast Feed → bars_1s SAFE MERGE (latency paint only).

Research contract:
  SoT = on-chain tip→hot (charts/dump/flow). dumpTags = sec_dump_tags_v1.
  Fast Feed = early latency paint into open UTC second of bars_1s only.
  Never invents prices. Never overwrites binance_1s/binance.
  WICK_BAND clamp vs last solid close. Fail soft if FF stale/disconnect.

Merge policy:
  - source=fastfeed on open second only (t_ms floor).
  - Prefer FF for live tip latency; REST binance_1s remains closed-bucket authority;
    tip_hot remains on-chain SoT when it lands.
  - ON CONFLICT: do not replace binance_1s; merge high/low/close with wick clamp.
  - If FF age > STALE_S or no decoded USD price → no-op (BN/tip_hot/oracle continue).
  - 3/5/15/30s rollups inherit via sec_bars_read from bars_1s.
"""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import time
from pathlib import Path

ROOT = Path(os.environ.get("SEC_BARS_ROOT", "/srv/skew/em-realtime/sec-bars"))
FF_OUT = Path(os.environ.get("FF_OUT", "/srv/skew/em-realtime/arb-fastfeed/out"))
FF_Q = Path(os.environ.get("FF_QUOTES", str(FF_OUT / "quotes.jsonl")))
FF_STATUS = Path(os.environ.get("FF_STATUS", str(FF_OUT / "status.json")))
DB = Path(os.environ.get("SEC_BARS_DB", str(ROOT / "out" / "bars_1s.sqlite3")))
POOL_MAP = Path(os.environ.get("FF_POOL_MAP", str(ROOT / "fastfeed-pool-map.json")))
STATUS = Path(os.environ.get("FF_INGEST_STATUS", str(ROOT / "out" / "fastfeed-ingest-status.json")))
QUOTES = Path(os.environ.get("SEC_BARS_QUOTES", str(ROOT / "out" / "quotes.json")))

STALE_S = float(os.environ.get("FF_STALE_S", "5.0"))
POLL_S = float(os.environ.get("FF_INGEST_POLL_S", "0.05"))
WICK_BAND_TOKEN = float(os.environ.get("FF_WICK_BAND", "0.05"))  # match common.WICK_BAND['token']
SOLID = ("binance_1s", "binance", "tip_hot", "binance_ws")
Q96 = 2**96

# minimal usd anchors on Arb (price ≈ 1 for stables; eth/btc need last solid)
USD_QUOTES = {
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USD₮0
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",  # USDC.e
    "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",  # DAI
}


def write_status(obj):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(obj, indent=2) + "\n")


def load_pool_map():
    if not POOL_MAP.exists():
        return {}
    try:
        return json.loads(POOL_MAP.read_text())
    except Exception:
        return {}


def decode_v3_sqrt_price(data_hex: str) -> float | None:
    """Swap data: amount0, amount1, sqrtPriceX96, liquidity, tick → price token1/token0."""
    if not data_hex:
        return None
    h = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if len(h) < 192:  # 3 * 64 hex chars min for first three words
        return None
    try:
        # word2 = sqrtPriceX96 (uint160 in 32-byte slot)
        sqrt_x96 = int(h[128:192], 16)
        if sqrt_x96 <= 0:
            return None
        ratio = (sqrt_x96 / Q96) ** 2
        if ratio <= 0 or ratio != ratio:  # NaN
            return None
        return float(ratio)
    except Exception:
        return None


def last_solid(con, market_id: str):
    row = con.execute(
        """SELECT close, source, time FROM bars_1s
           WHERE market=? AND source IN ('binance_1s','binance','tip_hot','binance_ws')
           ORDER BY time DESC LIMIT 1""",
        (market_id,),
    ).fetchone()
    if not row:
        return None
    return float(row[0]), row[1], int(row[2])


def wick_clamp(px: float, ref: float, band: float) -> float | None:
    if ref <= 0 or px <= 0:
        return None
    lo, hi = ref * (1 - band), ref * (1 + band)
    if px < lo or px > hi:
        return None  # reject spike — fail soft
    return px


def upsert_ff(con, market_id: str, t_ms: int, price: float) -> str:
    """Merge into open second. Never overwrite binance_1s."""
    ot = int(t_ms) - (int(t_ms) % 1000)
    row = con.execute(
        "SELECT open,high,low,close,volume,quote_volume,count,source FROM bars_1s WHERE market=? AND time=?",
        (market_id, ot),
    ).fetchone()
    if row is None:
        con.execute(
            """INSERT INTO bars_1s(market,time,open,high,low,close,volume,quote_volume,count,source)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (market_id, ot, price, price, price, price, 0.0, 0.0, 0, "fastfeed"),
        )
        return "insert"
    o, h, l, c, v, qv, n, src = row
    if src in ("binance_1s", "binance"):
        # closed-bucket SoT: only nudge high/low/close within wick of BN open/close, keep source
        con.execute(
            """UPDATE bars_1s SET high=max(high,?), low=min(low,?), close=?
               WHERE market=? AND time=?""",
            (price, price, price, market_id, ot),
        )
        return "nudge_bn"
    nh, nl = max(h, price), min(l, price)
    con.execute(
        """UPDATE bars_1s SET high=?, low=?, close=?, source='fastfeed'
           WHERE market=? AND time=? AND source NOT IN ('binance_1s','binance')""",
        (nh, nl, price, market_id, ot),
    )
    return "merge"


def ff_fresh() -> tuple[bool, dict]:
    st = {}
    if FF_STATUS.exists():
        try:
            st = json.loads(FF_STATUS.read_text())
        except Exception:
            pass
    # reject if consumer says closed/error/rejected without recent msgs
    state = (st.get("state") or "").lower()
    if state in ("rejected", "error") and not st.get("msgs"):
        return False, st
    # quotes file mtime
    if not FF_Q.exists():
        return False, {**st, "reason": "no_quotes"}
    age = time.time() - FF_Q.stat().st_mtime
    if age > STALE_S * 4:  # file completely idle
        return False, {**st, "reason": "quotes_stale", "file_age_s": age}
    return True, st


def tail_lines(path: Path, offset: int) -> tuple[list[str], int]:
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if offset > size:
        offset = 0  # rotated
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
        new_off = f.tell()
    if not data:
        return [], new_off
    text = data.decode("utf-8", errors="ignore")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # if file didn't end with newline, last line may be partial — keep offset before it
    if not data.endswith(b"\n") and lines:
        partial = lines.pop()
        new_off -= len(partial.encode()) 
    return lines, new_off


def usd_price_from_pool(pool_info: dict, ratio_t1_per_t0: float, con) -> tuple[str, float] | None:
    """Map V3 token1/token0 ratio to market USD using pool map + last solid for eth/btc quotes."""
    token = (pool_info.get("token") or "").lower()
    quote = (pool_info.get("quote") or "").lower()
    qkind = (pool_info.get("qkind") or "").lower()
    market_id = pool_info.get("market") or f"arbitrum:{token}"
    if not token or ratio_t1_per_t0 <= 0:
        return None
    # Uniswap: price = token1 per token0. Which is base?
    # If quote is USD stable and token is token0 → need token0 in USD = (1/ratio) * quote_usd when quote=token1
    # pool map should set token0/token1 or "base_is_token0"
    base_is_t0 = pool_info.get("base_is_token0")
    if base_is_t0 is None:
        # heuristic: if quote in USD_QUOTES, assume quote is token1 (common USDC pairs)
        if quote in USD_QUOTES:
            base_is_t0 = True
        else:
            base_is_t0 = True
    if qkind == "usd" or quote in USD_QUOTES:
        quote_usd = 1.0
    else:
        # use last solid of quote market if mapped
        qmid = pool_info.get("quote_market") or f"arbitrum:{quote}"
        sol = last_solid(con, qmid)
        if not sol:
            return None
        quote_usd = sol[0]
    if base_is_t0:
        # token0 = base, token1 = quote → base_usd = quote_usd / (token1/token0) = quote_usd / ratio
        if ratio_t1_per_t0 == 0:
            return None
        px = quote_usd / ratio_t1_per_t0
    else:
        # token1 = base → base_usd = ratio * quote_usd
        px = ratio_t1_per_t0 * quote_usd
    if px <= 0 or px != px:
        return None
    return market_id, float(px)


def overlay_quote(market_id: str, price: float, t_ms: int):
    try:
        base = json.loads(QUOTES.read_text()) if QUOTES.exists() else {"quotes": {}}
    except Exception:
        base = {"quotes": {}}
    base.setdefault("quotes", {})[market_id] = {
        "t_ms": t_ms,
        "price": price,
        "source": "fastfeed",
        "age_s": 0.0,
        "role": "latency_paint",
    }
    base["updated_ms"] = t_ms
    base["fastfeed"] = True
    QUOTES.write_text(json.dumps(base) + "\n")


def main():
    offset = 0
    offset_path = ROOT / "out" / "fastfeed-ingest.offset"
    if offset_path.exists():
        try:
            offset = int(offset_path.read_text().strip() or "0")
        except Exception:
            offset = 0
    con = sqlite3.connect(str(DB), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    applied = rejected = 0
    while True:
        t0 = time.time()
        fresh, ff_st = ff_fresh()
        pool_map = load_pool_map()
        st = {
            "at": time.time(),
            "state": "running" if fresh else "fail_soft",
            "sot": "tip_hot",
            "role": "latency_paint_only",
            "dumpTags": "sec_dump_tags_v1",
            "wick_band_token": WICK_BAND_TOKEN,
            "stale_s": STALE_S,
            "pool_map_n": len(pool_map),
            "applied": applied,
            "rejected_wick": rejected,
            "ff": {"state": ff_st.get("state"), "msgs": ff_st.get("msgs"), "reason": ff_st.get("reason")},
            "policy": {
                "prefer_ff_open_second": True,
                "never_overwrite_binance_1s": True,
                "fallback": "binance_ws|binance_1s|tip_hot|oracle",
            },
        }
        if not fresh:
            st["note"] = "FF stale/absent — BN/tip_hot path unchanged (no blank, no tails)"
            write_status(st)
            time.sleep(max(0.2, POLL_S))
            continue
        if not pool_map:
            st["state"] = "waiting_pool_map"
            st["note"] = "build fastfeed-pool-map.json (pool→market); consumer may already be writing quotes"
            write_status(st)
            time.sleep(1.0)
            continue
        lines, offset = tail_lines(FF_Q, offset)
        offset_path.write_text(str(offset))
        now_ms = int(time.time() * 1000)
        for ln in lines:
            try:
                row = json.loads(ln)
            except Exception:
                continue
            pool = (row.get("pool") or "").lower()
            if not pool:
                continue
            info = pool_map.get(pool) or pool_map.get(pool.lower())
            if not info:
                continue
            t_ms = int(row.get("t_ms") or now_ms)
            if (now_ms - t_ms) / 1000.0 > STALE_S:
                continue  # too old
            ratio = decode_v3_sqrt_price(row.get("data") or "")
            if ratio is None:
                continue
            got = usd_price_from_pool(info, ratio, con)
            if not got:
                continue
            market_id, px = got
            sol = last_solid(con, market_id)
            if sol:
                clamped = wick_clamp(px, sol[0], WICK_BAND_TOKEN)
                if clamped is None:
                    rejected += 1
                    continue
                px = clamped
            # else: first paint allowed without ref (still real decoded swap)
            try:
                upsert_ff(con, market_id, t_ms, px)
                overlay_quote(market_id, px, t_ms)
                applied += 1
            except sqlite3.Error as e:
                st["err"] = str(e)[:200]
        try:
            con.commit()
        except sqlite3.Error as e:
            st["err"] = str(e)[:200]
        st["applied"] = applied
        st["rejected_wick"] = rejected
        st["cycle_ms"] = int((time.time() - t0) * 1000)
        write_status(st)
        time.sleep(max(0.0, POLL_S - (time.time() - t0)))


if __name__ == "__main__":
    main()
