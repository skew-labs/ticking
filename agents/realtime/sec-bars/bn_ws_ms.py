#!/usr/bin/env python3
"""Binance combined trade/kline_1s WS → ms tip into bars_1s + quotes.json (mapped majors).

Institutional paint path: sub-second updates to the open UTC second.
Does not replace REST kline mirror; provisional source=binance_ws until REST lands.
Never invents markets outside map.json.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("SEC_BARS_ROOT", "/srv/skew/em-realtime/sec-bars"))
MAP_PATH = Path(os.environ.get("SEC_BARS_MAP", str(ROOT / "map.json")))
DB_PATH = Path(os.environ.get("SEC_BARS_DB", str(ROOT / "out" / "bars_1s.sqlite3")))
QUOTES_PATH = Path(os.environ.get("SEC_BARS_QUOTES", str(ROOT / "out" / "quotes.json")))
STATUS_PATH = Path(os.environ.get("SEC_WS_STATUS", str(ROOT / "out" / "bn_ws_status.json")))
WS_BASE = os.environ.get("BINANCE_WS", "wss://stream.binance.com:9443/stream")


def load_map():
    m = json.loads(MAP_PATH.read_text())
    streams = []
    sym_to_markets = {}
    for key, spec in m.get("symbols", {}).items():
        bn = (spec.get("binance") or "").lower()
        if not bn:
            continue
        streams.append(f"{bn}@trade")
        streams.append(f"{bn}@kline_1s")
        sym_to_markets[bn.upper()] = [x["id"] for x in spec.get("markets", [])]
    return streams, sym_to_markets


def db():
    con = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def apply_trade(con, market_ids, price, qty, t_ms, lock):
    if price <= 0:
        return
    ot = int(t_ms) - (int(t_ms) % 1000)
    q = float(qty or 0)
    qv = q * price
    with lock:
        for mid in market_ids:
            # Upsert: never fight REST unique; ON CONFLICT merges into open second
            con.execute(
                """INSERT INTO bars_1s(market,time,open,high,low,close,volume,quote_volume,count,source)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(market,time) DO UPDATE SET
                     high=max(bars_1s.high, excluded.high),
                     low=min(bars_1s.low, excluded.low),
                     close=excluded.close,
                     volume=bars_1s.volume + excluded.volume,
                     quote_volume=bars_1s.quote_volume + excluded.quote_volume,
                     count=bars_1s.count + 1,
                     source=CASE WHEN bars_1s.source IN ('binance_1s','binance') THEN bars_1s.source ELSE 'binance_ws' END
                """,
                (mid, ot, price, price, price, price, q, qv, 1, "binance_ws"),
            )
        con.commit()


def write_quotes_overlay(sym_to_markets, last_px, lock):
    """Merge WS tip into quotes.json without clobbering structure."""
    try:
        base = json.loads(QUOTES_PATH.read_text()) if QUOTES_PATH.exists() else {"quotes": {}}
    except Exception:
        base = {"quotes": {}}
    now = int(time.time() * 1000)
    with lock:
        for sym, px in list(last_px.items()):
            for mid in sym_to_markets.get(sym, []):
                base.setdefault("quotes", {})[mid] = {
                    "t_ms": now,
                    "price": px,
                    "source": "binance_ws",
                    "age_s": 0.0,
                }
        base["updated_ms"] = now
        base["ws"] = True
        QUOTES_PATH.write_text(json.dumps(base) + "\n")


def main():
    try:
        import websocket  # websocket-client
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "websocket-client"])
        import websocket

    streams, sym_to_markets = load_map()
    url = WS_BASE + "?streams=" + "/".join(streams)
    con = db()
    lock = threading.Lock()
    last_px = {}
    stats = {"at": 0, "msgs": 0, "trades": 0, "err": None, "streams": len(streams)}

    def on_msg(ws, message):
        try:
            data = json.loads(message)
            payload = data.get("data") or data
            stats["msgs"] += 1
            # trade
            if payload.get("e") == "trade":
                sym = (payload.get("s") or "").upper()
                price = float(payload["p"])
                qty = float(payload.get("q") or 0)
                t_ms = int(payload.get("T") or payload.get("E") or time.time() * 1000)
                last_px[sym] = price
                mids = sym_to_markets.get(sym) or []
                if mids:
                    apply_trade(con, mids, price, qty, t_ms, lock)
                    stats["trades"] += 1
            elif payload.get("e") == "kline":
                k = payload.get("k") or {}
                sym = (payload.get("s") or k.get("s") or "").upper()
                if sym and k.get("c"):
                    last_px[sym] = float(k["c"])
        except Exception as e:
            stats["err"] = str(e)[:200]

    def on_err(ws, err):
        stats["err"] = str(err)[:200]

    def status_loop():
        while True:
            stats["at"] = int(time.time())
            stats["last_px_n"] = len(last_px)
            STATUS_PATH.write_text(json.dumps(stats, indent=2) + "\n")
            if last_px:
                write_quotes_overlay(sym_to_markets, last_px, lock)
            time.sleep(0.5)

    threading.Thread(target=status_loop, daemon=True).start()
    while True:
        try:
            ws = websocket.WebSocketApp(url, on_message=on_msg, on_error=on_err)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            stats["err"] = f"reconnect:{e}"
            time.sleep(2)


if __name__ == "__main__":
    main()
