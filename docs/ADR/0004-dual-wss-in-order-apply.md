# ADR 0004: Dual QuickNode WSS, in-order apply, 64-state reorg ring

- Status: accepted
- Date: 2026-10

## Context
Quotes must reflect the latest block. A single WebSocket can go silent, pushed logs can arrive after the next
head, and each endpoint is rate-limited.

## Decision
- `newHeads` is raced across **two QuickNode WSS endpoints**. The first head to arrive wins.
- Logs are fetched **by block hash** as the source of truth. Dynamic fees are read on a separate socket, and
  only for pools that were touched.
- Blocks are applied **strictly in order**. A public-RPC poller fills gaps.
- A **ring of 64 states** allows hash-based reorg rollback.
- A verifier keeps re-checking pools on chain (Multicall3 `slot0`/`liquidity`, V4 via PoolManager storage).
  A divergent pool is removed from routing until it is reconciled.

## Consequences
- In the final gate run, 96.1% of quotes were on the latest block, and 610k verifier checks found 0 divergences.
