# Changelog

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
