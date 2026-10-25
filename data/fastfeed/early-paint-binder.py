#!/usr/bin/env python3
"""Provisional candle/banner binder from Fast Feed quotes.jsonl (early paint only).
Activates meaningfully after ticket round (~2026-10-24 01:00 SGT). tip/hot stays SoT.
Writes: out/early-prices.jsonl (latest per pool) + out/early-status.json
"""
import json, os, time
from collections import defaultdict
OUT = os.environ.get("OUT_DIR", "/srv/skew/em-realtime/arb-fastfeed/out")
Q = os.path.join(OUT, "quotes.jsonl")
EARLY = os.path.join(OUT, "early-prices.jsonl")
STATUS = os.path.join(OUT, "early-status.json")
os.makedirs(OUT, exist_ok=True)

def main():
    if not os.path.exists(Q):
        json.dump({"state": "waiting_quotes", "at": time.time(), "note": "ticket not active or consumer not writing yet"}, open(STATUS, "w"))
        print("waiting for", Q)
        return 0
    latest = {}
    n = 0
    with open(Q) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            pool = (row.get("pool") or "").lower()
            if not pool: continue
            latest[pool] = row
    # rewrite compact latest snapshot (not full history)
    with open(EARLY, "w") as f:
        for row in latest.values():
            f.write(json.dumps({"t_ms": row.get("t_ms"), "pool": row.get("pool"), "tx": row.get("tx"),
                                "block": row.get("block"), "early": True, "role": "provisional"}) + "\n")
    json.dump({"state": "ok", "at": time.time(), "quotes_lines": n, "pools": len(latest),
               "sot": "tip_hot", "role": "early_paint_only",
               "ticket_active_after_sgt": "2026-10-24 01:00"}, open(STATUS, "w"))
    print(json.dumps({"pools": len(latest), "lines": n}))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
