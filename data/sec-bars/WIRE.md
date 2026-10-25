# Wire plan — Arb Fast Feed → early prices / 1s paint

1. **SoT**: tip → hot → em-charts (`em_pool_reference_v2`) stays authoritative for 1m+ / dump / flow.
2. **초봉 (1s) paint path (LIVE NOW)**: `em-sec-bars-1s` mirrors Binance `interval=1s` klines into
   `/srv/skew/em-realtime/sec-bars/out/bars_1s.sqlite3` for mapped Sol/Arb/RH markets.
   Served by `:8895` when `interval=1s` (see `sec_bars_read.py`).
3. **Fast Feed** (`wss://arb1-txfeed.arbitrum.io/`): early on-chain ticks after ticket
   **2026-10-24 01:00 SGT** → `out/quotes.jsonl` → (next) `ingest_fastfeed_ticks.py` into same
   `bars_1s` open-second buckets (`source=fastfeed`). Does not replace Binance bucket rules.
4. Binder `early-paint-binder.py` still snapshots pools; 1s serve reads `sec-bars` not binder file.
5. Free tip path unchanged (Arb chunk 50 / RH 100). Wick clamp intact.

Binance 1s rules mirrored: UTC second floor; contiguous seconds; empty → O=H=L=C=prior, vol=0.
