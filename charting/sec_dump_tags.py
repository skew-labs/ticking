#!/usr/bin/env python3
"""1s-chart whale dump tags (Sol / Arb / RH) — Binance second buckets.

Aligns on-chain tip→hot sells (fill_meta side=A) to UTC-second-floor ms used by
sec-bars / Binance 1s klines. Emits the same marker contract as dump_tags.py
(kind=dump, sellers[], unknown) so FE dumpTags paint can reuse chips.

Product SoT: charts/dump/flow = on-chain. BN 1s bars are paint; dump coincidence
prefers on-chain fill prices when hot exists, with bar-close drop as fallback.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

HOT_ROOT = Path(os.environ.get("EM_HOT_ROOT", "/srv/skew/em-charts/hot"))
SEC_BARS_DB = Path(os.environ.get("SEC_BARS_DB", "/srv/skew/em-realtime/sec-bars/out/bars_1s.sqlite3"))

# Liquid majors on 1s — tighter than Flow 1h dump_tags
MIN_WHALE_USD = float(os.environ.get("SEC_DUMP_MIN_WHALE_USD", "2000"))
CLUSTER_USD = float(os.environ.get("SEC_DUMP_CLUSTER_USD", "8000"))  # whale cluster alone + any down tick
ONCHAIN_DUMP_BPS = float(os.environ.get("SEC_DUMP_ONCHAIN_BPS", "1.5"))  # 1.5 bps
BAR_DUMP_BPS = float(os.environ.get("SEC_DUMP_BAR_BPS", "2.0"))
MERGE_GAP_S = int(os.environ.get("SEC_DUMP_MERGE_GAP_S", "2"))
MAX_MARKERS = int(os.environ.get("SEC_DUMP_MAX_MARKERS", "12"))
MAX_SELLERS = int(os.environ.get("SEC_DUMP_MAX_SELLERS", "3"))
LOOKBACK_PAD_S = int(os.environ.get("SEC_DUMP_LOOKBACK_PAD_S", "5"))

# side A = sender sent market token (sell); B = bought — matches trades_response
SELL_SIDE = "A"


def _nz(x) -> float:
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else 0.0
    except Exception:
        return 0.0


def _short(addr: str) -> str:
    a = (addr or "").lower()
    if a.startswith("0x") and len(a) >= 10:
        return a[:6] + "…" + a[-4:]
    if len(a) > 12:
        return a[:4] + "…" + a[-4:]
    return a or "?"


def _money(n: float) -> str:
    v = abs(_nz(n))
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:.0f}"


def _entity(addr: str) -> str | None:
    a = (addr or "").lower()
    if not (a.startswith("0x") and len(a) == 42):
        return None
    try:
        import sys
        hp = "/srv/skew/em-holdings-proxy"
        if hp not in sys.path:
            sys.path.insert(0, hp)
        import entity_agg  # type: ignore
        ents = entity_agg.load_entities() or {}
        eids = (ents.get("by_addr") or {}).get(a) or []
        if not eids:
            return None
        e = (ents.get("by_id") or {}).get(eids[0]) or {}
        name = e.get("name")
        return str(name) if name else None
    except Exception:
        return None


def _seller_row(addr: str, usd: float, window_out: float, tx: str | None = None) -> dict:
    a = (addr or "").lower()
    ent = _entity(a)
    display = ent or _short(a)
    pct = (usd / window_out) if window_out > 0 else 0.0
    row = {
        "address": a,
        "entity": ent or "",
        "label": display,
        "usd": round(usd, 2),
        "pct": round(pct, 4),
        "role": "seller",
    }
    if tx:
        row["tx"] = tx
    return row


def _hot_path(chain: str) -> Path | None:
    p = HOT_ROOT / chain / "history.sqlite3"
    return p if p.exists() else None


def _split_market(market_id: str) -> tuple[str, str]:
    if ":" not in market_id:
        return "", market_id
    chain, tok = market_id.split(":", 1)
    return chain, tok


def fetch_hot_sells(chain: str, token: str, t0_s: int, t1_s: int) -> list[dict]:
    """On-chain tip→hot sells in [t0_s, t1_s] inclusive (unix seconds)."""
    path = _hot_path(chain)
    if not path or t1_s < t0_s:
        return []
    tok = token.lower() if token.startswith("0x") else token
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=1500")
    try:
        rows = con.execute(
            """
            SELECT f.time, m.side, m.trader, m.usd, m.tx, f.price
            FROM fills f
            JOIN fill_meta m ON m.identity = f.identity
            WHERE f.time >= ? AND f.time <= ?
              AND lower(m.token) = lower(?)
            ORDER BY f.time ASC, f.slot ASC, f.tx_order ASC
            """,
            (int(t0_s), int(t1_s), tok),
        ).fetchall()
    except Exception:
        return []
    finally:
        con.close()
    out = []
    for t, side, trader, usd, tx, price in rows:
        out.append(
            {
                "time": int(t),
                "side": side,
                "trader": (trader or "").lower(),
                "usd": _nz(usd),
                "tx": tx or "",
                "price": _nz(price),
            }
        )
    return out


def fetch_bar_closes(market_id: str, t0_ms: int, t1_ms: int, *, binance_only: bool = False) -> dict[int, float]:
    """UTC second → close from sec-bars (ms keys floored).

    binance_only=True skips flat_carry / oracle-fast tip rows that can jump ~30bps
    vs Binance and fake dump tags (esp. Sol SOLUSDT mirror).
    """
    if not SEC_BARS_DB.exists():
        return {}
    con = sqlite3.connect(f"file:{SEC_BARS_DB}?mode=ro", uri=True)
    try:
        if binance_only:
            rows = con.execute(
                "SELECT time, close FROM bars_1s WHERE market=? AND time>=? AND time<=? "
                "AND source IN ('binance_1s','binance') ORDER BY time",
                (market_id, int(t0_ms), int(t1_ms)),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT time, close FROM bars_1s WHERE market=? AND time>=? AND time<=? ORDER BY time",
                (market_id, int(t0_ms), int(t1_ms)),
            ).fetchall()
    except Exception:
        return {}
    finally:
        con.close()
    return {int(t): float(c) for t, c in rows if c is not None}


def _dump_seconds_from_fills(fills: list[dict]) -> dict[int, dict]:
    """Per-second dump signals from on-chain fill tape."""
    by: dict[int, list] = defaultdict(list)
    for f in fills:
        by[f["time"]].append(f)
    secs = sorted(by)
    prev_last = None
    flagged: dict[int, dict] = {}
    thr = ONCHAIN_DUMP_BPS / 10_000.0
    for t in secs:
        xs = by[t]
        first_px = next((x["price"] for x in xs if x["price"] > 0), None)
        last_px = next((x["price"] for x in reversed(xs) if x["price"] > 0), None)
        sells = [x for x in xs if x.get("side") == SELL_SIDE and x["usd"] >= MIN_WHALE_USD]
        sell_usd = sum(x["usd"] for x in sells)
        all_sell_usd = sum(x["usd"] for x in xs if x.get("side") == SELL_SIDE)
        sos = ((last_px - prev_last) / prev_last) if (prev_last and last_px) else 0.0
        intra = ((last_px - first_px) / first_px) if (first_px and last_px) else 0.0
        sharp = (sos <= -thr) or (intra <= -thr)
        cluster = sell_usd >= CLUSTER_USD or all_sell_usd >= CLUSTER_USD
        # whale sell + (sharp dump OR big cluster with non-positive move)
        if sells and (sharp or (cluster and (sos <= 0 or intra <= 0 or last_px is None))):
            flagged[t] = {
                "time": t,
                "sos_bps": round(sos * 10_000, 3),
                "intra_bps": round(intra * 10_000, 3),
                "sell_usd": round(sell_usd or all_sell_usd, 2),
                "trigger": "onchain_dump+whale" if sharp else "whale_cluster",
                "sells": sells,
            }
        if last_px:
            prev_last = last_px
    return flagged


def _dump_seconds_from_bars(closes: dict[int, float], sell_by_s: dict[int, list]) -> dict[int, dict]:
    """Fallback / reinforce using Binance-mirrored 1s closes (ms → s)."""
    thr = BAR_DUMP_BPS / 10_000.0
    times = sorted(closes)
    flagged: dict[int, dict] = {}
    prev = None
    for t_ms in times:
        c = closes[t_ms]
        t = t_ms // 1000
        chg = ((c - prev) / prev) if (prev and prev > 0) else 0.0
        sells = [x for x in sell_by_s.get(t, []) if x.get("side") == SELL_SIDE and x["usd"] >= MIN_WHALE_USD]
        if chg <= -thr and sells:
            flagged[t] = {
                "time": t,
                "sos_bps": round(chg * 10_000, 3),
                "intra_bps": 0.0,
                "sell_usd": round(sum(x["usd"] for x in sells), 2),
                "trigger": "bar_dump+whale",
                "sells": sells,
            }
        prev = c
    return flagged


def _merge_windows(flagged: dict[int, dict]) -> list[dict]:
    if not flagged:
        return []
    secs = sorted(flagged)
    windows = []
    cur = {
        "t0": secs[0],
        "t1": secs[0],
        "sell_usd": flagged[secs[0]]["sell_usd"],
        "trigger": flagged[secs[0]]["trigger"],
        "sells": list(flagged[secs[0]]["sells"]),
        "sos_bps": flagged[secs[0]].get("sos_bps"),
    }
    for t in secs[1:]:
        if t - cur["t1"] <= MERGE_GAP_S:
            cur["t1"] = t
            cur["sell_usd"] += flagged[t]["sell_usd"]
            cur["sells"].extend(flagged[t]["sells"])
            if "onchain_dump" in flagged[t]["trigger"] or "bar_dump" in flagged[t]["trigger"]:
                cur["trigger"] = flagged[t]["trigger"]
            cur["sos_bps"] = flagged[t].get("sos_bps")
        else:
            windows.append(cur)
            cur = {
                "t0": t,
                "t1": t,
                "sell_usd": flagged[t]["sell_usd"],
                "trigger": flagged[t]["trigger"],
                "sells": list(flagged[t]["sells"]),
                "sos_bps": flagged[t].get("sos_bps"),
            }
    windows.append(cur)
    windows.sort(key=lambda w: -w["sell_usd"])
    return windows[:MAX_MARKERS]


def _aggregate_sellers(sells: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for s in sells:
        a = s.get("trader") or ""
        if not a:
            continue
        row = by.setdefault(a, {"usd": 0.0, "tx": s.get("tx") or ""})
        row["usd"] += _nz(s.get("usd"))
        if s.get("tx") and not row["tx"]:
            row["tx"] = s["tx"]
    if not by:
        return []
    total = sum(v["usd"] for v in by.values()) or 1.0
    ranked = sorted(by.items(), key=lambda kv: -kv[1]["usd"])[:MAX_SELLERS]
    return [_seller_row(a, v["usd"], total, v.get("tx")) for a, v in ranked]


def build_sec_dump_tags(
    market_id: str,
    bars: list[dict] | None = None,
    *,
    now_s: int | None = None,
    period_s: int = 1,
) -> list[dict]:
    """Build dumpTags aligned to TF open times (UTC floor ms for period_s)."""
    chain, token = _split_market(market_id)
    if not chain or not token:
        return []
    now_s = int(now_s or time.time())

    if bars:
        t0_ms = int(bars[0]["time"])
        t1_ms = int(bars[-1]["time"])
    else:
        t1_ms = now_s * 1000
        t0_ms = t1_ms - 300_000

    t0_s = t0_ms // 1000 - LOOKBACK_PAD_S
    t1_s = t1_ms // 1000 + 1

    fills: list[dict] = []
    coverage = "hot_fill_meta"
    if chain in ("arbitrum", "robinhood"):
        fills = fetch_hot_sells(chain, token, t0_s, t1_s)
    elif chain == "solana":
        # No em-charts hot fill_meta for Sol (no trader/side). Honest gap.
        coverage = "sol_no_hot_fill_meta"
        fills = []
    else:
        coverage = "unsupported_chain"
        fills = []

    sell_by_s: dict[int, list] = defaultdict(list)
    for f in fills:
        sell_by_s[f["time"]].append(f)

    flagged = _dump_seconds_from_fills(fills)
    closes = fetch_bar_closes(market_id, t0_ms, t1_ms, binance_only=(chain == "solana"))
    for t, meta in _dump_seconds_from_bars(closes, sell_by_s).items():
        if t not in flagged:
            flagged[t] = meta
        elif flagged[t].get("trigger") == "whale_cluster" and "bar_dump" in meta["trigger"]:
            flagged[t] = meta

    # Sol / empty fills: mark only *sharp* BN 1s dumps (seller unknown).
    # Higher bar threshold — no hot fill_meta traders on Sol yet.
    if not fills and closes:
        thr = max(BAR_DUMP_BPS, 10.0) / 10_000.0  # >=10 bps without sellers
        prev = None
        for t_ms in sorted(closes):
            c = closes[t_ms]
            t = t_ms // 1000
            chg = ((c - prev) / prev) if (prev and prev > 0) else 0.0
            if chg <= -thr:
                flagged[t] = {
                    "time": t,
                    "sos_bps": round(chg * 10_000, 3),
                    "intra_bps": 0.0,
                    "sell_usd": 0.0,
                    "trigger": "bar_dump",
                    "sells": [],
                }
            prev = c

    windows = _merge_windows(flagged)
    markers: list[dict] = []
    for w in windows:
        sellers = _aggregate_sellers(w.get("sells") or [])
        usd = _nz(w.get("sell_usd"))
        if not usd and sellers:
            usd = sum(_nz(s.get("usd")) for s in sellers)
        top = sellers[0] if sellers else None
        if top:
            who = top.get("entity") or top.get("label") or _short(top.get("address") or "")
            pct = top.get("pct") or 0.0
            label = f"{who} sold {_money(top.get('usd') or 0)} / {pct*100:.0f}%"
        elif usd > 0:
            label = f"Outflow {_money(usd)} · seller unknown"
        else:
            bps = w.get("sos_bps")
            label = f"Dump {bps:.1f}bps · seller unknown" if bps is not None else "Dump · seller unknown"

        markers.append(
            {
                "kind": "dump",
                "time": int(w["t0"]) * 1000,  # Binance 1s open ms
                "tEnd": int(w["t1"]) * 1000,
                "usd": round(usd, 2),
                "trigger": w.get("trigger") or "whale_sell",
                "label": label[:96],
                "symbol": "",
                "sellers": sellers,
                "sellerCount": len(sellers),
                "unknown": not bool(sellers),
                "bps": w.get("sos_bps"),
                "source": "sec_dump_tags_v1",
                "coverage": coverage,
                "bucket": "binance_1s_utc_second",
            }
        )

    period_s = max(1, min(60, int(period_s or 1)))
    bucket_ms = period_s * 1000
    # Re-bucket 1s dump windows onto TF open times (merge sellers in same bucket)
    if period_s > 1 and markers:
        by: dict[int, dict] = {}
        for m in markers:
            b0 = (int(m["time"]) // bucket_ms) * bucket_ms
            b1 = (int(m.get("tEnd") or m["time"]) // bucket_ms) * bucket_ms
            cur = by.get(b0)
            if not cur:
                by[b0] = dict(m)
                by[b0]["time"] = b0
                by[b0]["tEnd"] = b1
                by[b0]["sellers"] = list(m.get("sellers") or [])
                by[b0]["bucket"] = f"binance_{period_s}s_utc"
            else:
                cur["tEnd"] = max(cur["tEnd"], b1)
                cur["usd"] = round(_nz(cur.get("usd")) + _nz(m.get("usd")), 2)
                # merge sellers by address
                seen = {s.get("address"): s for s in cur["sellers"]}
                for s in m.get("sellers") or []:
                    a = s.get("address")
                    if not a:
                        continue
                    if a in seen:
                        seen[a]["usd"] = round(_nz(seen[a].get("usd")) + _nz(s.get("usd")), 2)
                    else:
                        seen[a] = dict(s)
                        cur["sellers"].append(seen[a])
                tot = sum(_nz(s.get("usd")) for s in cur["sellers"]) or 1.0
                for s in cur["sellers"]:
                    s["pct"] = round(_nz(s.get("usd")) / tot, 4)
                cur["sellers"] = sorted(cur["sellers"], key=lambda s: -_nz(s.get("usd")))[:MAX_SELLERS]
                cur["sellerCount"] = len(cur["sellers"])
                cur["unknown"] = not bool(cur["sellers"])
                top = cur["sellers"][0] if cur["sellers"] else None
                if top:
                    who = top.get("entity") or top.get("label") or _short(top.get("address") or "")
                    pct = top.get("pct") or 0.0
                    cur["label"] = f"{who} sold {_money(top.get('usd') or 0)} / {pct*100:.0f}%"[:96]
                if "onchain_dump" in (m.get("trigger") or "") or "bar_dump" in (m.get("trigger") or ""):
                    cur["trigger"] = m.get("trigger")
        markers = list(by.values())
    else:
        for m in markers:
            m["bucket"] = f"binance_{period_s}s_utc"

    markers.sort(key=lambda m: (1 if m.get("unknown") else 0, -_nz(m.get("usd"))))
    return markers[:MAX_MARKERS]


def attach_to_candles_body(body: dict, market_id: str, symbol: str = "", period_s: int = 1) -> dict:
    """Mutate candles JSON body with dumpTags/markers (idempotent)."""
    bars = body.get("candles") or []
    period_s = max(1, min(60, int(period_s or 1)))
    tags = build_sec_dump_tags(market_id, bars, period_s=period_s)
    if symbol:
        for t in tags:
            t["symbol"] = symbol.upper()
    body["dumpTags"] = tags
    body["markers"] = tags  # FE accepts markers || dumpTags
    body["dumpTagCount"] = len(tags)
    body["dumpTagsMeta"] = {
        "policy": "sec_dump_tags_v1",
        "bucket": f"binance_{period_s}s_utc",
        "period_s": period_s,
        "minWhaleUsd": MIN_WHALE_USD,
        "onchainDumpBps": ONCHAIN_DUMP_BPS,
        "barDumpBps": BAR_DUMP_BPS,
        "sot": "tip_hot_onchain_fills",
        "paintBars": "sec_bars_1s_rollup",
    }
    return body


if __name__ == "__main__":
    import json
    import sys

    mid = sys.argv[1] if len(sys.argv) > 1 else "robinhood:0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    # synthesize last 180s empty bars range via now
    now = int(time.time())
    bars = [{"time": (now - i) * 1000, "close": 0} for i in range(180, -1, -1)]
    tags = build_sec_dump_tags(mid, bars)
    print(json.dumps({"market": mid, "n": len(tags), "tags": tags[:5]}, indent=2))
