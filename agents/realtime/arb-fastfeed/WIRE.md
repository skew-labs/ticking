# Wire plan — Arb Fast Feed → bars_1s SAFE MERGE

## Research contract (locked)

| Layer | Role |
|-------|------|
| **SoT** | on-chain **tip→hot** — charts / dump / flow |
| **dumpTags** | `sec_dump_tags_v1` |
| **Fast Feed** | **latency paint only** into open UTC second of `bars_1s` |
| **Binance** | closed 1s authority (`binance_1s`); WS tip (`binance_ws`) |
| **WICK_BAND** | token 5% — FF rejects outside band vs last solid |
| **Fail soft** | FF stale/disconnect → no-op; BN/tip_hot continue |

## Merge (`em-sec-bars-fastfeed-ingest`)

1. Decode V3 `sqrtPriceX96` → USD via `fastfeed-pool-map.json`.
2. Wick-clamp vs last solid; reject spikes.
3. Open-second upsert `source=fastfeed`; **never overwrite** `binance_1s`.
4. `quotes.json` overlay; 3/5/15/30s via `sec_bars_read`.
5. STALE_S=5 fail-soft.

## Go-live ~2026-10-24 01:00 SGT

```bash
systemctl enable --now em-arb-fastfeed-prices.service
# ingest already enabled
```
No extra ticket spend. No FE deploy required for this path.
