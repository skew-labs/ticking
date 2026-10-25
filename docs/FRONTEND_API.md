# Ticking Router — Frontend API (LIVE at https://api.ticking.dev/api/router/)

Chain: Robinhood Chain mainnet, chainId 4663 (0x1237). Read-only quote engine (no keys, no txs). The user signs and sends the tx.

**Router: TickingRouter v1 `0x9745fF56A6FC085B3e70A7509cd49Ab6b97dCBD6` (RH mainnet, chain 4663).** Every quote with `recipient=` returns `tx.{to,data,value}` targeting it. Deployed-router eth_call proof: 63/63 exact (`data/live/router_proof.json`). Gate numbers: ENGINE.md "FINAL gate run".

## LIVE public endpoints (since 2026-10-25 08:16 SGT): https://api.ticking.dev/api/router/…
- `https://api.ticking.dev/api/router/health`
- `https://api.ticking.dev/api/router/quote?tin=…&tout=…&amt=…&recipient=0x…[&slippageBps=50]` (best)
- `https://api.ticking.dev/api/router/fast?tin=…&tout=…&amt=…&recipient=0x…` (fast)
- `https://api.ticking.dev/api/router/pool_quote?…` (debug)
nginx: exact-match locations in /etc/nginx/snippets/ticking-router.conf (included in the api.ticking.dev :443 server). Rate limit: 20 r/s per IP, burst 40 → 429. CORS: `Access-Control-Allow-Origin: *`, GET/OPTIONS only.
Backend: persistent systemd `ticking-engine.service` on t1 (enabled, PRUNE_STATES=1), 127.0.0.1:8782/8783.
With `recipient`, the response's `tx` = `{to: 0x9745ff56a6fc085b3e70a7509cd49ab6b97dcbd6, data: swap(bytes) calldata, value: "0", minOut, deadline, executable}`. Send it directly after approve(tin → router).

## Public endpoints (mapping)
| public path | upstream | purpose |
|---|---|---|
| `GET /api/router/quote` | `127.0.0.1:8782/quote` | best route (split, multi-hop, gas-netted). Compute p50 6 ms / p99 28 ms (soak6) |
| `GET /api/router/fast`  | `127.0.0.1:8783/quote` | fast path (memes), compute p50 25 µs / p99 1.3 ms (soak6), ≤2 hops, 4-way split |
| `GET /api/router/health`| `127.0.0.1:8782/health` | 200 when lag ≤ 3 blocks, otherwise 503 |
| `GET /api/router/pool_quote` | `127.0.0.1:8782/pool_quote` | single-pool exact simulation (debug) |
`/metrics`, `/divergences` and `/freshness` stay internal (not proxied).

Rate limits: 20 req/s per IP (burst 40). The engine itself has no auth; nginx is the only gate.

## Request: GET /api/router/quote
| param | type | req | meaning |
|---|---|---|---|
| `tin` | address (lowercase hex) | yes | token in (WETH 0x0bd7d308f8e1639fab988df18a8011f41eacad73 for ETH; wrap first) |
| `tout` | address | yes | token out |
| `amt` | decimal string, wei/base units | yes | exact input amount |
| `gp` | integer wei | no | gas price for gas-netting (default 20000000 = 0.02 gwei) |
| `mode` | `fast` | no | on /quote forces the fast path (/fast always uses it) |
| `recipient` | address | no | if set, the response includes `tx.program` for TickingRouter `swap(bytes)` |
| `slippageBps` | int | no | default 50, max 5000; `minOut = netOut*(1-bps/1e4)` |
| `deadline` | unix seconds u32 | no | default now+60 |

Stables: **USDG** 0x5fc5360d0400a0fd4f2af552add042d716f1d168 (6 dec, Paxos, the real liquidity). **USDC** 0x80e0e24718dbfcad49ecaa6f1e6c89a190586ca8 (6 dec, bridged). Prefer USDG for stable routing.

Example:
`/api/router/quote?tin=0x0bd7d308f8e1639fab988df18a8011f41eacad73&tout=0x5fc5360d0400a0fd4f2af552add042d716f1d168&amt=1000000000000000000&recipient=0xYourAddr&slippageBps=30`

## Response 200
```json
{
  "block": 96952801, "blockHash": "0x4d0c…", "headBlock": 96952801, "lagBlocks": 0, "stateAgeUs": 1830,
  "tokenIn": "0x0bd7…", "tokenOut": "0x5fc5…", "amountIn": "1000000000000000000",
  "grossOut": "2320684736",          // exact output if executed at `block` (proven quote==fill to the wei)
  "netOut": "2320601234",            // grossOut minus gas cost in tokenOut units (what the split optimizes)
  "minOut": "2313639430", "slippageBps": 30,
  "gasUnits": 300000,                // estimate: 60k base + 120k per hop
  "mode": "best", "compute_us": 6595, "paths": 1155,
  "legs": [ { "amountIn": "688…", "hops": [ { "pool": "0x1667…", "venue": "univ3", "fee": 79, "zfo": true, "tokenIn": "0x0bd7…", "tokenOut": "0x5fc5…" } ] } ],
  "tx": { "to": "0x9745ff56a6fc085b3e70a7509cd49ab6b97dcbd6", "data": "0x627dd56a…", "value": "0", "program": "0x…", "executable": true, "reason": "", "minOut": "2313639430", "deadline": 1792865000 }   // null when no recipient
}
```
`venue` ∈ univ3, sushi, giga (V3 fee-keyed), ramses, up (V3 tickSpacing-keyed), v4 (zero-hook Uniswap V4).

## Building the tx
TickingRouter v1 (contracts/, UNAUDITED by the owner's choice). **Deployed on RH mainnet (chain 4663): `0x9745ff56a6fc085b3e70a7509cd49ab6b97dcbd6`**, tx `0x2440a58c7937491ab97eb9992403c5aaedc0b28a2636421d2a03f2b2324cd908`, block 96981545, Sourcify exact match (contracts/DEPLOYED.md).
- The engine returns a ready-to-send tx: `tx.to` = ROUTER, `tx.data` = `swap(bytes)` (selector `0x627dd56a`) ABI-encoded around `tx.program`, `tx.value` = "0". Send it as-is.
- `swap()` returns `out`. The read-only proof `harness/router_proof.py` eth_calls `tx.data` against the deployed router at the quote's block (holder funded via state override) and checks `out == grossOut` to the wei. Results: `data/live/router_proof.json`.
- Before the swap: `IERC20(tin).approve(ROUTER, amt)`. The router pulls `transferFrom(user → pool)` just in time.
- If `tx.executable == false`, don't send (`reason` explains, e.g. a V4 hop on a native-ETH key). Fall back to Kyber or re-quote.
- Program layout: header `tokenIn20|tokenOut20|recipient20|minOut u128|deadline u32`, then per hop `op u8 | amount u128 (first hop of each leg only) | hopTokenOut20 | payload` (op low nibble = venue, 0x80 = CHAIN, 0x40 = TERM). See contracts/ARCHITECTURE.md.
- Simulate with `eth_call` first. Quotes are valid for the quoted block; the price moves every ~100 ms, so `minOut` protects the user.

## Errors
| HTTP | body | meaning / UI action |
|---|---|---|
| 400 | `{"error":"no path"}` / `"no route"` | no executable route (e.g. USDC→X). Show "no liquidity" |
| 400 | `{"error":"tin"}`, `"amt"`, `"zero amount"`, `"same token"` | bad params |
| 400 | `{"error":"exceeds gas cap: N tick crossings"}` (pool_quote) | amount too large to execute |
| 503 | `{"error":"stale state","block":…, "head":…}` | engine >3 blocks behind. Retry after 200 ms, or fall back to Kyber |
| 404 | `{}` | unknown path |
Always check `lagBlocks` (0 = state at the newest block seen) and `block`.
