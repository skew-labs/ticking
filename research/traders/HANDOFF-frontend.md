# Handoff: trader APIs for the Economy panel and the wallet dossier

Both endpoints are served by em-charts (`127.0.0.1:8895`), are read-only, and are cached for 60 s. Their data is rebuilt every 6 h.

To reach the frontend they need either a pass-through in the wallet bridge (`/api/public/em/*` on `:8896`) or a direct read; that wiring is not done yet. Nothing in Ticking production or the frontend was changed.

## Economy panel: "Top traders" section

`GET /api/public/em/top-traders?market=<chain>:<token>&window=7d|30d|all&limit=20` (limit 1..100, default 20)

```json
{"market": "robinhood:0xd060…", "chain": "robinhood", "token": "0xd060…", "symbol": "NVDA", "window": "30d",
 "asOf": 1792402303, "builtAt": 1792403000, "eligibleTraders": 1530,
 "traders": [{"rank": 1, "wallet": "0xb802…", "pnl_usd": 13079.86, "realized_usd": 12266.22, "unrealized_usd": 0.0,
              "volume_usd": 1699162.0, "trades": 183, "roi": 0.0132, "win_rate": 0.1429, "trips": 7, "avg_hold_s": 101234.0,
              "held": 0.0, "avg_cost": null, "price_now": 217.15, "transfer_adjusted": true, "early_score": 0.61,
              "entry_multiple": 0.94, "max_single_gain": 720.1, "first_trade": 1788686160, "last_trade": 1791375540,
              "labels": ["top_trader:NVDA", "smart_money"]}],
 "method": {"pnl": "...", "realized": "...", "unrealized": "...", "windows": "...", "excluded": "..."},
 "reason": "only present when traders is empty"}
```

Suggested UI:
- **Rows:** rank, wallet (opens the dossier, or the console map with `focus=`), PnL with realized / unrealized split, volume, trades, win rate, and label chips.
- **Badge:** show "transfer-adjusted" when `transfer_adjusted` is true.
- **Window tabs:** 7d / 30d / all.
- **Empty state:** show `reason`.
- **Footer:** "Swap PnL · as of `asOf` (UTC)", with `method` in a tooltip.

## Wallet dossier: PnL

`GET /api/public/em/wallet-pnl?chain=robinhood|arbitrum&address=0x…&limit=20`

```json
{"chain": "robinhood", "address": "0x…", "asOf": 1792402303, "builtAt": 1792403000,
 "summary": {"pnl_all": 11727.16, "pnl_30d": 0.0, "pnl_7d": 0.0, "realized_usd": 11727.16, "unrealized_usd": 0.0, "volume_usd": 82996.12,
             "trades": 60, "tokens": 16, "profitable_tokens": 6, "trips": 12, "wins": 7, "win_rate": 0.583, "roi": 0.277,
             "best_token": "JUGGERNAUT", "debt_usd": 0.0, "borrows": 0, "liquidations": 0, "transfer_adjusted": false,
             "bot_reason": null, "...": "..."},
 "tokens": [{"token": "0xd732…", "symbol": "JUGGERNAUT", "kind": "token", "pnl_usd": 5649.0, "pnl_30d": 0.0, "pnl_7d": 0.0,
             "realized_usd": 5649.0, "unrealized_usd": 0.0, "volume_usd": 7259.18, "trades": 4, "win_rate": 1.0, "trips": 1,
             "held": 0.0, "avg_cost": null, "price_now": 0.0039, "transfer_adjusted": false, "early_score": 0.98,
             "entry_multiple": 39.7, "price_flag": false, "stable": false, "first_trade": 1782933720, "last_trade": 1783489920}],
 "labels": [{"label": "smart_money", "symbol": null, "confidence": 0.706, "evidence": {"pnl_usd": 11727.16, "win_rate": 0.583, "...": "..."}}],
 "method": {"...": "..."}}
```

Behaviour notes:
- An unknown wallet or an excluded sender (bot, protocol or exchange) returns 404 with a `detail` sentence. Show it as "No swap PnL".
- For `price_flag: true` rows, show "price unreliable" and leave them out of totals (the summary already does).

The galaxy agent answers "top traders of X", "smart money" and "PnL of 0x…" with the tools `top_traders` and `wallet_pnl`. The same data can feed the Ticking assistant intents.
