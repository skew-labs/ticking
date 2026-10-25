# ADR 0005: Tiered candle storage (deep DuckDB, hot SQLite, 1 s store)

- Status: accepted
- Date: 2026-10

## Context
Charts need full history, a live tail and sub-minute resolution, and they must not block writers.

## Decision
- **Deep:** `candles.duckdb` per chain. It is rebuilt offline, swapped in atomically and opened read-only by every reader.
- **Hot:** `history.sqlite3` per chain in WAL mode. It has one writer (the live tail) and keeps 14 days.
  `state.data_version` is bumped in every write transaction.
- **1 s:** `bars_1s.sqlite3`, whose rows are tagged by source. A closed Binance second is never overwritten.
  Fast Feed ticks paint only the open second.
- The reader merges deep minutes up to `data_until_ms` with hot 1-minute bars after that point, then rolls up
  every larger interval from them.

## Consequences
- Minute candles have no gap at the deep/hot seam, and readers never block the writer.
