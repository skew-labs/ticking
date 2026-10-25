# Changelog

## Unreleased

- Sync `contracts/` to TickingRouter v2 source (0.05% fee, native ETH V4 flag); live deployment remains v1 in `DEPLOYED.md`.
- Sync `aggregator/` engine sources with the running t1 engine tree.
- Add `search/`, `research/`, `em/`, `graph/`, `agents/` (Muse, track, bridge, MCP, context, realtime) with `docs/ARCHITECTURE-AGENTS.md`.
- Frontend router API doc marked LIVE at `https://api.ticking.dev/api/router/`.

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com).

## [Unreleased]

### Added
- Repository layout covering contracts, aggregator, data and charting, plus a root Makefile and CI workflows.
- Architecture docs and ADRs.

## [1.0.0] - 2026-10-25

### Added
- `TickingRouter` v1 deployed on Robinhood Chain mainnet at `0x9745fF56A6FC085B3e70A7509cd49Ab6b97dCBD6`.
- `engine2`: dual-WSS state engine with fast (`:8783`) and best (`:8782`) quote servers. Public API at
  `https://api.ticking.dev/api/router/`.
- 1-second bars (Binance mirror, on-chain tip→hot, Arbitrum Fast Feed paint) and gap-free minute candles on the charts edge.
