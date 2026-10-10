# Customer API, embeds and agent access

The hosted service supports subscription entitlements and explicitly enabled prepaid access. Both use the same market identity and read contracts. The current code defines Explore, Charts and Data products; see `api/server/products.py` for authoritative plan values.

- Charts: managed display grants and registered-origin embeds.
- Data: direct feeds and execution tape access in addition to charts.
- Prepaid reads: integer-unit price, candle and trade charges with an idempotency key.
- Prepaid streams: explicit bounded time/bandwidth leases, reservation and release of unused units.
- Mint watches: verified discovery and shared collection admission before a managed chart is ready.

The source snapshot has Charts at $29/30 days and Data at $59/30 days, with its existing annual pricing. Prepaid base rates are 5 micro-USDC per price read, 50 per candle/trade payload unit, and 100 per 15-second stream unit plus bandwidth. Limits and feature gates are checked server-side. Publishing source does not create an invoice or authorize a charge.

## Agent request lifecycle

1. The owner registers an account/project, scopes a server-side key, and explicitly enables a bounded budget.
2. A funding invoice pins payer, destination, token, amount, network and unique reference.
3. The client signs an unsigned transaction using its own authorized wallet. The server never takes custody of its signing key.
4. Finalized status and exact receipt/balance effects are verified before ledger credit. A replayed receipt cannot credit twice.
5. A read reserves its maximum charge. Successful response bytes, fingerprint and balanced posting commit together.
6. Retry with the same idempotency key returns the stored result without a second charge. Conflicting parameters are rejected. Failed or unavailable reads release the reservation.

See `api/server/credit_api.py`, `credits.py`, `payments.py`, and the agent-oriented `/api/mcp` handler in `app.py`. Tests mock settlement and upstream reads; they do not sign, fund, or charge a live account.

```sh
# Explicit paid-read example; configure your own authorized key and existing credits.
TICKING_API_KEY=... node examples/agent-read.mjs \
  --market SOL --idempotency-key my-unique-read-0001 --allow-charge
```

The example requires an explicit charge flag and does not create a payment. Keep the key out of browser code and logs. For display, use a public project ID with `terminal/sdk/managed-embed.js`; its registered-origin grant is short lived and scoped. For direct data use `/api/v1/candles`, `/api/v1/trades/history` and their stream equivalents. Use explicit exchange aggregation for venue instruments, and mint/pool selectors for on-chain instruments.

The public API scaffold preserves account/authentication and payment boundaries, but excludes private console screens and production configuration. A deployment still requires its own identity-provider configuration, secure HTTPS origin, payment destination and valid data rights.
