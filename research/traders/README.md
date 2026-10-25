# em-traders: top traders and smart money (Arbitrum One, Robinhood Chain)

Phase 1 of wallet labeling. Every number comes from our own swap data; no LLM is used.

| | |
|---|---|
| Code | `/Users/huhyul/em-mapping/traders`, mirrored to `/srv/skew/em-traders`, with its own venv: duckdb 1.4.5, numpy, pyarrow |
| Build | `traders.py <chain>`, a step in the 6-hourly em-map-refresh. Robinhood takes about 2.5 min (5.7 GB RSS), Arbitrum about 5 min (7.8 GB RSS) |
| Outputs | `em-data/traders/<chain>/` and `em-graphloop/export/<chain>/labels_traders.parquet` |
| Reads | em-charts `/api/public/em/top-traders`, `/api/public/em/wallet-pnl`; galaxy agent tools `top_traders`, `wallet_pnl` |

## Inputs (all read-only)

- **Trades:** the em-charts wallet index, `data/<chain>/wallet/trades_b=<nibble>.parquet`.
  - It holds every priced swap on a served market token, meaning one side is a USD stable, ETH, BTC or ARB anchor.
  - Each swap is attributed to `tx.from`, through `info.json` `trades_last_block`.
  - Fills after that block come from the em-charts hot store. Every run copies them to `log/recent_trades.parquet`, because the hot store prunes fills after 3 days.
- **Prices:**
  - em-charts `candles.duckdb`: `anchor_px` converts the quote leg to USD. Hourly token bars supply the bad-print band and the reference close.
  - The price now is the median of the last 25 fills in the last 6 h. It is used only when it agrees with the reference close (×1.5 for stocks and anchors, ×5 for tokens). Otherwise the last hourly close with at least 3 fills and at least $100 is used.
  - em-charts' own `last_price` is not used: on dead tokens it can be a single garbage print (122 Robinhood markets are more than 100× off their median).
- **Balances now:** analytics `holders_snap`, plus `xfer_gap` and `rt/xfer_*` deltas, plus lending collateral from `positions_snap`.
- **Exclusions:**
  - Labels from `em-graphloop/export/<chain>/labels.parquet` (main and delta layers). Senders labelled `mev_bot`, `dex_router`, `dex_pool`, `cex_hot_wallet`, `bridge`, `lending`, `token_contract`, `nft_contract`, `contract_other` or `multisig_or_safe` never enter the ledger.
  - Senders labelled `cex_deposit` are kept: a real deposit address does not swap, and on Arbitrum the deposit heuristic also catches router-heavy EOAs.
- **Leverage:** econ v3 `sheet_summary.v3` (debt now) and `econ_actions` (borrow, collateral posts, liquidations).

## Method (per wallet × token)

The ledger is a C kernel (`pnl_kernel.c`, built as `libpnl.so`). The arithmetic follows. `validate.py` recomputes any row in plain Python.

1. **Average cost.** A buy adds units at its USD cost. A sell realizes `proceeds − units × average cost` on the units the wallet bought by swap.
2. **Transfers in.** Units sold beyond the swap-bought inventory came from transfers (or unpriced swaps). They realize nothing: they are valued at the sell price and counted in `xin_q` / `transfer_in_usd`.
3. **Unrealized.**
   - `held = min(swap inventory, balance now + lending collateral)`, and unrealized PnL is `held × (price now − average cost)`.
   - Inventory above the balance left by transfer (to an exchange, another wallet or LP) and is closed at cost, so it adds no PnL.
   - The marked value of `held` is capped at 25% of the token's 7-day turnover (minimum $1k). The uncapped figure is in `unrealized_paper_usd`.
4. **Flags.**
   - `transfer_adjusted`: transfers explain more than 10% of sells or of bought inventory.
   - Round trip: a position opened from flat and sold back to flat (float32 dust under 1e-5 of the peak counts as flat). Win rate is `wins / round trips`, and holding time is per round trip.
5. **Windows.** 7d and 30d PnL is the realized PnL on sells inside the window, plus the share of unrealized PnL that was bought inside the window (`min(1, window buy units / inventory)`).
6. **Wash.** A wallet's buy and sell of the same token in the same block are dropped (both legs).
7. **Bad prints are dropped from the ledger.** A fill is dropped when any of these holds:
   - its price is outside the token's hourly reference `[low/f, high×f]`, where f is 1.25 for stocks, 1.10 for anchors and 1.5 for tokens (the same hour; within 6 h for stocks and anchors; bars with fewer than 2 fills are ignored);
   - its price is more than 10× off the last reference close when no bar is near;
   - a single fill is above $20M.
8. **Price-flagged tokens.** A token is flagged when more than 10% of its fills are bad prints, or when em-charts flags the market. Flagged tokens stay in `wallet_token_pnl` but are left out of leaderboards, labels and wallet totals.
9. **Stable-value markets** (a 90-day daily range under 10% around $1, e.g. USD anchors and yield-bearing dollars) get no leaderboards or labels and are left out of wallet totals.
10. **Bot-like senders** that escaped the labels:
    - more than 2,000 swaps with an average hold under 5 min;
    - 500 or more swaps at more than 300 per day;
    - more than 20% same-block buy+sell rows (with 50 or more swaps);
    - more than 300 tokens.

    They keep their rows but get `bot_reason` and no leaderboard or label.

Not covered yet:
- Round trips between a wallet's own addresses (needs entity clustering, Phase 2).
- Swaps against an unpriced pair (no anchor leg).
- PnL from LP fees or perps.
- Swaps sent through smart-contract wallets or relayers are attributed to the relayer EOA.

## Labels (`trader_labels.parquet`; `labels_traders.parquet` with `layer='traders'`, one row per wallet × label)

| label | rule (bots, price-flagged and stable tokens excluded) |
|---|---|
| `smart_money` | Cross-token, all history. All of: ≥ $25k volume, ≥ 10 round trips, PnL ≥ $10k, ROI ≥ 10%, realized ≥ 50% of PnL, win rate ≥ 55%, ≥ 2 profitable tokens, best token ≤ 80% of gross profit, largest single sell ≤ 50% of PnL, 30-day PnL ≥ 0 when active, transfers-in ≤ 50% of sells. Confidence 0.55–0.95 from round trips, win rate and profitable tokens. |
| `top_trader:<SYMBOL>` | Top 20 by 30-day PnL (> 0) on a token with a 30-day leaderboard. |
| `early_buyer:<SYMBOL>` | Among the first 25 eligible buyers with ≥ $1k bought, in the first 10% of the token's trading life. Tokens (not stocks) also need the price now ≥ 2× the entry. |
| `leveraged_trader` | Debt now ≥ $5k and ≥ $10k swapped in 30 days; or liquidated at least once with ≥ 3 borrows and ≥ $25k swapped. |

These rows are kept out of `labels.parquet` on purpose: em-charts analytics (`build_classes`) and the delta labeler expect one row per address there, and a second row per address would double-count balances.

## Leaderboards (`token_leaderboard.parquet`, top 100 per token and window)

- **Per-row gates** (volume in the window / trades in the window): all time $10k / 5; 30d $5k / 3; 7d $2k / 2.
- **A token gets a leaderboard** when its eligible traders swapped at least $25k (stocks), $250k (anchors) or $100k (tokens) in the window, and there are at least 5 of them.

## Files

- `wallet_token_pnl.parquet`: every wallet × token with a priced swap, sorted by wallet.
- `wallet_pnl.parquet`: the cross-token totals, plus `bot_reason` and leverage.
- `token_leaderboard.parquet`: sorted by token, window and rank.
- `smart_money.parquet` and `trader_labels.parquet`.
- `info.json`: as-of time, counts and method.
- `log/recent_trades.parquet`: the durable copy of hot-store fills.

## Tools

- `validate.py <chain> <wallet> <token|SYMBOL>`: independent recompute with the arithmetic printed next to the stored row.
- `botcheck.py <chain>`: the fate of the raw top 100 senders.
- `q.py`: ad-hoc read-only SQL (`{O}` = traders dir; `CH=arbitrum`).
