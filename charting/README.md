# em-charts: USD price charts for Arbitrum One and Robinhood Chain assets

A candle backend that speaks the Ticking chart SDK's API shape, so the Ticking `<tick-chart>` renderer
(copied, not modified in Ticking) draws on-chain DEX prices for Robinhood stock tokens, Robinhood Chain
tokens and Arbitrum tokens. It runs on Amsterdam (`skew:/srv/skew/em-charts`, `127.0.0.1:8895`).

## Architecture (deep + hot)

```
                stage parts (pool-minute bars + priced trades)
                 │ build_db.py (rebuild.sh, live paused)          │ live.py (in server, every 60 s)
                 ▼                                                ▼
   deep: data/<chain>/candles.duckdb  (read-only)     hot: hot/<chain>/history.sqlite3  (WAL, 3 days)
   markets, token_bars, pool_bars, tape, anchor_px    fills, bars (Ticking DDL) + fill_meta, pools, state
                 └──────────────── emread.Reader (read API) ─────────────────┘
                              server.py (HTTP)   ·   Ticking EvmHistory (import)
```

- The deep DuckDB is immutable between rebuilds. Every consumer opens it `read_only`: the em-charts server,
  the live tail (to seed its working set) and Ticking. `emread.Reader` reopens it when a rebuild swaps the file.
- The hot SQLite store has one writer, the live tail. Readers open it with `mode=ro` (WAL), so readers never
  block the writer.
- `state.data_version` is incremented in the same transaction as every write. SQLite's `PRAGMA data_version`
  also changes for other connections.
- `fills` and `bars` use Ticking's `observed_projector.hpp` DDL verbatim. The EVM mapping is in `hotstore.py`:
  - identity = `0x<tx>:<log>`; slot = block, tx_order = tx index, event_order = log index.
  - time is the block second for follower chunks.
  - price and quote_volume are USD; bars are 60, 300 and 3600 s.
  - order strings are `%020d:%010d:%06d:<identity>`.
  - pool keys are `0x<pool>`; the token reference composite uses `token:0x<token>`.
- `Reader.bars` merges the two stores: deep minutes up to `info.data_until_ms`, then hot 1m bars after it.
  Every interval is rolled up from those, so the seam is invisible.

## Data flow

```
map/<chain>/dex_trades (snapshot)        tail/<chain>/logs (Blob gap chunks)   em-realtime/out/<chain>/logs (follower)
        │ build_bars.py                          │ tail_bars.py backfill             │ tail_bars.py realtime / live.py
        └────────────── stage/pool_1m/part=*.parquet  (pool × minute OHLCV in quote units) ──────────────┘
                                       │ build_db.py
                     data/<chain>/candles.duckdb  (markets, market_pools, token_bars, pool_bars, anchor_px, info)
                                       │ server.py (+ live.py thread, every 60 s)
             /api/public/engine/candles · /api/public/stream/candles · /api/public/markets[/<id>]
```

- Block times: `data/block_minutes_<chain>.parquet` (minute → first/last block). It is built by
  `build_block_minutes.py` from the Blob block Parquet (2 columns over HTTPS) and extended from
  local follower chunks by `block_minutes_live.py`. Swaps after the last known block are never assigned
  a minute.
- Swap pricing: only swaps with an anchor on one side are priced.
  - Anchors are USD stables, ETH/WETH (including V4 native ETH), WBTC/cbBTC and ARB.
  - The other side is the base asset. Dust prints are dropped (quote below $1, 0.0005 ETH, 0.00002 BTC or 2 ARB).
- USD conversion: stables count as $1. ETH, BTC and ARB use a per-minute composite of their own
  stable-quoted pools, as-of the same minute and at most 6 h old.
- Token composite (`em_pool_reference_v2`), per minute:
  - Reference: the close of the most established pool printing that minute. "Established" is the number of
    the pool's minutes with at least $50 of turnover, so bot pools that print dust every minute never lead.
  - Pools whose close is within 5% of the reference are admitted.
  - `close` is the reference itself, and `open` is the previous minute's close.
  - Every open, high and low input is held inside ±10% of the reference.
  - Wicks only come from prints with at least $100 of turnover, from the reference pool, or from a sole print.
  - Volume, baseVolume and count are summed over the admitted prints.
  - Dust minutes (stock/anchor < $10, token < $1 turnover) set no price.
  - Stocks and anchors are dropped beyond 5× their whole-history median, which removes launch-day prints from
    mis-scaled pools.
  - Temporal sanity: a minute is dropped when its close leaves either the ±30-bar median or the ±6 h median
    by the kind's factor (stocks/anchors 1.25, tokens 3).
  - Anchor rates use the median of USD-stable pools. Pool-minutes averaging more than $20M per swap are bogus.
    Rates more than 10% off their ±60 min median are dropped.
  - Per-pool series are clamped to 1.25× around the composite. Minutes whose close is more than 2× off it are dropped.
  - Root cause of the NVDA $3,600 wick (fixed): admitted thin pools still contributed their raw `open` and
    `greatest(open, close)` as wick fallbacks. A thin NVDA/WETH V4 pool printing $142–$322 sat inside the
    close band for a minute, so its open became the composite high, and the 1h/4h/1d rollups carried it.
- Per-pool series (`aggregation=pool`) are raw USD prints for the top 3 pools of stocks and anchors, and
  of tokens above 10× the market threshold.
- Scope:
  - Every Robinhood stock token (`econ/robinhood/focus_stock_tokens.csv`).
  - All anchors.
  - Tokens whose admitted all-time DEX turnover is ≥ $1M on Robinhood or ≥ $5M on Arbitrum.
- Stored resolutions:

  | Resolution | Kept for |
  |---|---|
  | 1m | 60 days |
  | 5m | 180 days |
  | 15m | 2 years |
  | 1h, 4h, 1d | full history |

  Any other interval (3m, 30m, 2h, 1w, …) is rolled up on request from the largest stored base that divides it.
  `history.resolutionStarts` in each response tells the chart where finer data begins.

## API (Ticking shapes)

- `GET /api/public/engine/candles?aggregation=token&mint=<chain>:<address>&interval=15m&limit=180&gap_policy=carry[&before=<ms>]`
  - Returns `mode: asset_history` and `identity.network = arbitrum | robinhood`.
  - `quote` is `USD` and `series` is `reference`.
  - `mint` also accepts a symbol (`NVDA`, `robinhood:NVDA`); stocks win ties, then volume decides.
- `GET /api/public/engine/candles?aggregation=pool&pool=<chain>:<pool>&interval=1h`
  - Returns `mode: executed_history` with `series: trade`.
- `GET /api/public/stream/candles?...` (same parameters)
  - SSE `snapshot` messages for the latest 180 bars whenever the live tail changes them.
  - A `: ping` is sent every 10 s; the server asks the client to reconnect after 15 min.
- `GET /api/public/markets[?network=arbitrum|robinhood&kind=stock|token|anchor]`
  - Fields: price, 24h change % and volume, and a 7-day hourly sparkline.
- `GET /api/public/markets/<id>`
  - Adds the top pools (quote symbol, USD volume, swaps), the first bar time and `dataUntil`.
- `GET /api/public/engine/trades?mint=<chain>:<token>&aggregation=<token policy>|pool[&pool=…][&limit=100][&before=<s>]`
  - Ticking trade-tape shape (`normalizeTape`), newest first.
  - Each trade has `identity` (`<chain>:<tx>:<log>`), `time` (s, minute precision), `price`,
    `reference_price`, `reference_eligible`, `base_volume`, `quote_mint: USD`, `usd`, `side`, `slot` (block),
    `tx_order`, `event_order`, `tradeId`, `pool`, `tx` and `trader`.
  - `side` is `B` when the transaction sender received the market token and `A` when it sent it.
  - The window is the last 7 days (`tape` table), kept live.
  - `/api/public/stream/trades` sends the same payload as SSE snapshots.
- `GET /api/public/em/wallet-trades?chain=&address=&token=&from=&to=&limit=1000`
  - `trades`: the wallet's buys/sells of the token: `time`, `side`, `amount`, `price`, `usd`, `block`,
    `tx_index`, `log_index`, `tx`, `source`.
  - `actions`: lending/vault actions on the token: `supply`/`withdraw` (with `position: collateral|…`),
    `borrow`, `repay`, `liquidated`, `liquidate`, `stake` and `unstake`. Each has `amount`, `usd` (1h close) and `tx`.
  - The wallet is the transaction sender.
  - Data comes from the wallet index (snapshot dex_trades + gap + follower) plus the live tape.
  - `from`/`to` accept seconds or milliseconds. `truncated` is true when more than `limit` rows matched.
- `GET /api/health`: overall `ok` or `degraded` (HTTP 503 when any chain lags more than 15 min while live).
  Per chain it reports lag, live state, whether the tape and wallet index are present, and the policy.
- `GET /api/health/quality[?refresh=1]`: the regression report (`qa.py`), cached for 10 min.
- `GET /api/status`
  - Per chain: lag in seconds and the last live cycle.
- Limits and headers:
  - At most 32 concurrent requests (otherwise 503 with `Retry-After`) and 64 SSE streams.
  - 20 req/s per client with a burst of 120 (429). `X-Forwarded-For` is honoured from loopback proxies.
  - `Cache-Control`: latest candles and trades 5 s, historical pages 120 s, markets and wallet 30 s, SDK
    files 300 s, health no-store.
  - Methods other than GET return 405. Errors are JSON `{detail}`: 400 bad params, 404 unknown market,
    503 data not ready.
- Gaps of at most 5 bars carry the prior close: `synthetic: true` and `gapKind: unverified_no_record`.
  Longer gaps stay open, so `fillPolicy` is `recorded`, the same as Ticking's token references.

## Running

```bash
# one-time / full rebuild (each step checks free disk and aborts below 100 GB)
python build_block_minutes.py <chain>        # needs /root/.em-blob.sas (Blob blocks over HTTPS)
python build_bars.py <chain>                 # snapshot dex_trades -> stage
python tail_bars.py <chain> backfill         # Blob gap chunks in tail/<chain> -> stage
python tail_bars.py <chain> realtime         # follower chunks -> stage
python build_bars.py <chain> recent          # snapshot trades inside the tape window
python build_db.py <chain>                   # stage -> candles.duckdb (writes .building, then swaps)
python build_wallet_index.py <chain>         # wallet index under data/<chain>/wallet
python qa.py data/<chain>/candles.duckdb     # wick/close regression report
systemctl restart em-map-charts              # API + live tail (systemd unit em-map-charts.service)
./rebuild.sh [chain ...]                     # routine deep rebuild: pauses live, rebuilds, restarts the unit
```

Use the venv `/srv/skew/em-galaxy/.venv` (duckdb 1.5.6). Each DuckDB process gets its own temp dir under
`tmp/<pid>`, because DuckDB temp file names are not unique per process.

## Read API (`emread.py`, stable)

```python
from emread import Reader            # sys.path: /srv/skew/em-charts ; venv: /srv/skew/em-galaxy/.venv (duckdb 1.5.6)
r = Reader("robinhood")              # or "arbitrum"
r.markets(kind="stock")              # list of dicts: id, chain, token, symbol, name, decimals, kind, usd_volume, swaps,
                                     #   n_pools, last_price, last_time(ms), change_24h, volume_24h, quality, quality_note, ref_median
r.market("NVDA")                     # symbol | 0x address | "<chain>:0x..."; stocks win ties, then volume
bars, has_more = r.bars("token", "0xd060…", 900, before_ms=None, limit=600)   # oldest -> newest
bars, has_more = r.bars("pool",  "0x62ab…", 3600)                             # per-pool USD series (top pools)
r.tape("0xd060…", pool=None, before_s=None, limit=100)                        # newest first
r.pools["0xd060…"]                   # top pools: pool id, address, quote symbol, usdVolume, swaps, rank
r.data_until_ms(); r.deep_until_ms(); r.data_version(); r.resolution_starts(); r.info()
```

- Bar dict: `time` (bucket start, ms UTC), `open`, `high`, `low`, `close`, `volume` (USD), `baseVolume`, `count`.
- Intervals: any whole number of minutes, hours or days up to 1 week. Weeks start Monday 00:00 UTC.
- Tape dict: `time` (s), `price`, `base_volume`, `usd`, `side` (B/A), `block`, `tx_index`, `log_index`, `tx`, `trader`,
  `pool` and `eligible`.
- Raises `ValueError` for sub-minute intervals. Thread-safe; one Reader per process is enough.

## Test page

`/` renders the Ticking `<tick-chart>` (dark theme, tools on) from the copied SDK in `web/ticking/`.
`patch_sdk.py` makes two edits to that copy only:

1. `sdk/data-feed.js` `validate()` accepts `identity.network` ∈ {`solana`, `arbitrum`, `robinhood`}.
   The original accepts only `solana` for executed/asset history.
2. `sdk/data-feed.js` `apply()` builds the drawing/market identity key from the response network
   instead of the literal `solana:`.

These are the only changes Ticking needs to render these markets natively. No auth token is needed: without a
`tokenProvider`, the SDK uses `/api/public/*` on `api-base`, and plain http is allowed on 127.0.0.1.

## Integration notes

**Galaxy (skew:/srv/skew/em-galaxy)**
- Serve the copied SDK (`web/ticking/`) from the galaxy origin or from this server.
- Mount `<tick-chart mint="<chain>:<token>" aggregation="token" theme="dark" api-base="…">` in the wallet or
  asset detail panel.
- Wallet holdings and econ positions already carry token addresses, so `<chain>:<address>` maps to a market id
  directly. Use `GET /api/public/markets/<id>` to check that a market exists, and show the "not charted" state
  on 404.
- The galaxy runs on 127.0.0.1:8890 and this server on 8895. Either proxy `/api/public/*` from the galaxy
  server to 8895, or pass `api-base` (CORS `*` is enabled for GET). A same-origin proxy is preferred, so a
  future public deployment exposes one origin.
- The live agent can call `/api/public/engine/candles?interval=1d` for "how did NVDA trade this week" answers,
  and `/api/public/markets?kind=stock` for rankings.

**Ticking (no change made; this is what native support needs)**
1. In `sdk/data-feed.js` `validate()`, allow `identity.network` ∈ {solana, arbitrum, robinhood} for
   `executed_history` / `asset_history`.
2. In `sdk/data-feed.js` `apply()`, build the market identity key from `identity.network`.
3. Optional: `src/chart-advanced.js` comparisons and the tape read `/api/public/markets?view=table` and
   `/api/public/engine/trades` on `apiBase`. This server returns 404 for trades, so the order-flow tab stays empty.
4. Mints are `<chain>:<0x address>` strings. Ticking's `TOKENS` catalogue (`src/data.js`) has no entries for
   them, so the header falls back to `symbol` from the response.


## Token analytics: `/api/public/em/*` (Economic Machine layer for Ticking)

These endpoints are precomputed by `analytics.py` and served by `emapi.py`.
- `market` is `<chain>:<token address>`; a symbol (`NVDA`, `arbitrum:WETH`) also works.
- Every response has `asOf` (the data cut, unix seconds), `updatedAt`, and `coverage {notes[], unpricedShare}`.
- Unknown markets return 404 `{detail}`. Before the first refresh the endpoints return 503.
- Responses take 1–3 ms, read from `data/<chain>/analytics/analytics.sqlite3`.
- `Cache-Control` is 60 s for token-state, token-brief and flows, and 30 s for liquidations and events.
- The rate limits and `X-Forwarded-For` handling are the same as for the other endpoints.

| Endpoint | Shape |
|---|---|
| `token-state?market=` | `{market, symbol, kind, quality, asOf, updatedAt, price, supply, supplyUsd, holders, burned, buckets[{key, label, amount, usd, share, wallets, d24h, d7d, d24hUsd, d7dUsd, protocols[{name, id, amount, usd, share, wallets, d24h?, d7d?}], families?}], lendingContractsPhysical, lendingClaims[{name, id, collateral, supplied, borrowed, collateralWallets, physical}], topHolders[{address, amount, usd, share, bucket, class, entity}], coverage{notes, unpricedShare, attributedShare, changesFrom, change7dComplete, econ}}` |
| `liquidations?market=` | `{market, symbol, price, priceAsOf, asOf, updatedAt, ladder[{price, priceLow, collateralAmount, collateralUsd, debtUsd, positions, cumulativeUsd}] (2% bins of the current price down to −50%, then the rest; descending), positions[{wallet, protocol, position, liqPrice, distancePct, collateralAmount, collateralUsd, debtUsd, debtSymbol, threshold, thresholdKind}] (nearest first, ≤50), atRisk[…] (at or past liquidation by up to 1.5×), totals{positions, debtUsd, collateralAmount, collateralUsd, atRiskPositions, atRiskCollateralUsd, excludedDeepUnderwater, excludedDeepUnderwaterDebtUsd}, coverage}` |
| `flows?market=&window=1h\|24h\|7d` | `{market, symbol, window, asOf, updatedAt, from, price, classes[{class: whale\|retail\|mev\|exchange\|contract, buyUsd, sellUsd, netUsd, trades, wallets}], lending{supplyUsd, withdrawUsd, borrowUsd, repayUsd, liquidatedUsd, events, supplyAmount, withdrawAmount, liquidatedAmount, netCollateralUsd, netCollateralByClass}, bridges{inUsd, outUsd, inAmount, outAmount}, lp{addAmount, removeAmount, addUsd, removeUsd}, topWallets[{wallet, class, netUsd}], coverage{notes, unpricedShare, lendingFrom, lendingComplete, bridgesFrom, lpFrom, lpComplete}}` |
| `events?market=&from=&to=&limit=&kinds=` | `{market, symbol, asOf, updatedAt, from, to, events[{time, kind, usd, amount, wallet, walletClass, tx, block, logIndex, protocol, text}] (newest first, ≤500, default last 7 d), coverage}`. `kind` is one of `whale_buy`, `whale_sell`, `collateral_in`, `collateral_out`, `borrow`, `repay`, `liquidation`, `bridge_in`, `bridge_out`, `lp_add` or `lp_remove`. |
| `token-brief?market=` | `{market, symbol, asOf, updatedAt, price, quality, headline, facts[{key, text, value, unit, source}], coverage}`. Each fact's `value` is the same number as in the text, taken from the other four outputs of the same refresh. |

**Methodology**
- **Balances.** The map snapshot holdings (`address_tokens`) are combined with every ERC20 `Transfer` after it: the Blob gap chunks plus the follower chunks. Balances are block-exact up to the hot tip.
  - Supply is the sum of positive balances, excluding burn addresses; mints are included because they are transfers from 0x0.
  - Arbitrum WETH wraps emit `Transfer` from 0x0, so they are counted.
- **Buckets are physical holdings and sum to supply.** The holder class is decided by these rules, first match wins:

  | Holder | Bucket |
  |---|---|
  | 0x0 / 0xdead | burned (excluded from supply) |
  | DEX pool or V4 PoolManager | `dex_liquidity`, split v2/v3/v4/other |
  | Morpho singleton, Aave aToken, cToken or Comet | lending (split below) |
  | GMX market or vault | `perp_liquidity` |
  | ERC-4626 vault | `vaults_contracts` |
  | Bridge label or OFT adapter | `bridges` |
  | CEX hot wallet or deposit label | `exchanges` |
  | Stock-token minter or the token contract | `issuer` |
  | MEV bot, or any other contract | `vaults_contracts` |
  | EOA | `whales` or `retail` |

  - A whale is an EOA (or a contract wallet with no protocol role) holding at least $100K and at least 0.1% of supply. Labelled whales qualify from 0.01%.
  - Contracts with no protocol role (mostly smart-contract user accounts) go to `smart_wallets`.
- **Lending.** Tokens physically inside a lending protocol are split into `lending_collateral` and `lending_supplied`.
  - Morpho keeps collateral and never lends it, so collateral is assigned first.
  - Other lenders are split by the ratio of position claims.
  - `lendingClaims` lists the economic claims: collateral posted, supplied and borrowed. Claims can exceed the physical amount because borrowed tokens are re-lent.
- **Positions.** Econ v2 sheet rows (v3 is used automatically once `econ/<chain>/v3/sheet_rows*.parquet` exists) plus Morpho Blue and Aave v3 (and fork) events decoded from chain logs after the econ snapshot.
  - The decoded events are SupplyCollateral, WithdrawCollateral, Borrow, Repay and Liquidate for Morpho, and Supply, Withdraw, Borrow, Repay and LiquidationCall for Aave.
  - Rows flagged `unverified_principal` or `morpho_interest_runaway` are excluded.
- **Liquidation price.**
  - Morpho: `debt × loan price / (collateral × LLTV)`. LLTV comes from CreateMarket and was cross-checked with `idToMarketParams` over RPC.
  - Aave family: per wallet, holding the other collateral and debts fixed: `(Σdebt − Σother collateral × LT) / (amount × LT)`.
  - LT is read on-chain with `Pool.getConfiguration` (for example Aave v3 Arbitrum: WETH 0.84, ARB 0.63).
  - Left out: dust (< $10 collateral); wallets with unpriced legs; debt only in the same token as the collateral; positions more than 1.5× past liquidation (counted as `excludedDeepUnderwater`).
- **Flows and events.**
  - DEX trades come from the trade tape (7 days; the wallet is the transaction sender).
  - Lending comes from the decoded events. Borrows are attributed to the collateral token through the Morpho market; Aave borrows are cross-collateral and are not attributed.
  - Bridges are net transfers into and out of bridge contracts. LP comes from live.db.
  - Trade events are netted per transaction, so arbitrage loops cancel. Thresholds:

    | Event | Threshold |
    |---|---|
    | Trades | ≥ max($25K, 0.5% of the token's DEX liquidity) |
    | Collateral, borrow, repay, bridge, LP | ≥ max($10K, 0.25% of DEX liquidity) |
    | Liquidations | ≥ $1K |

  - Wallet classes come from labels plus the token's whale list.

**Refresh**
- `AnalyticsJob` runs in the server every 10 minutes and refreshes one chain at a time (about 100–220 s each at 2 DuckDB threads, `Nice=10` from the unit).
- The static layer (dims, snapshot holders, gap chunks, classes, Aave LTs) is rebuilt when the deep DB is newer.
- Run it by hand with `python analytics.py <chain> static|refresh|all`.
