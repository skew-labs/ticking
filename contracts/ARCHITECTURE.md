# TickingRouter v1 — Architecture (UNAUDITED)

## Principles
1. **One entry, one interpreter.** `swap(bytes program)` decodes a bit-packed program with bounds-checked `calldataload` reads. No ABI structs on the hot path. 364 B for a 4-venue split, vs 1,252 B for the same route in v0 (−71% L2 calldata).
2. **Zero persistent storage.** The lock and the callback context live in EIP-1153 transient slots (`T_LOCK, T_POOL, T_TIN, T_AMT, T_SRC, T_ZFO`). The contract has no `SSTORE`. RH chain support for these slots is proven on the fork (`test_fork_transient_supported`).
3. **Funds never rest.** Venues with callbacks (V3/forks, Algebra, V4, Balancer V3) are paid just in time, directly from the payer (`transferFrom(msg.sender → pool)`). Terminal hops deliver straight to the recipient. Only approval-based venues (Fermi, Tessera, Kyber) and chained intermediates touch the router's balance, and only inside the same call.
4. **Ownerless and immutable.** No owner, admin, pause, upgrade, proxy, sweep or `receive`. Venue addresses are constructor immutables. Adding a venue means deploying a new router.
5. **Trust is minimized per venue.**
   - Factory-verified pools: `getPool(t0,t1,fee)`, `getPool(t0,t1,tickSpacing)` or `poolByPair`. Their swap return value is trusted.
   - Everything else is **measured by balance delta**. Exactness is enforced by the remaining allowance being `== 0` after each approval venue.
6. **Assembly only where it pays.** Assembly is used for the program reads, ERC-20 transfer/transferFrom/approve/balanceOf (Solady-style success check: return true, or empty return plus code present) and the transient lock and context. Every block is commented with the invariant it maintains.

## Encoding
Header (80 B): `tokenIn 20 | tokenOut 20 | recipient 20 | minOut u128 | deadline u32`

Each op: `op u8 | [amount u128 unless CHAIN] | tokenOut 20 | venue payload`
- op low nibble = venue. Bit 7 = `CHAIN`: the input is the previous hop's output, held by the router. Bit 6 = `TERM`: the output goes to the recipient and must equal `tokenOut`.

| op | venue | payload |
|---|---|---|
|1|V3 fee-keyed (fid 0 Uniswap, 1 Sushi, 2 fork 0xEce6)| fid u8, fee u24, pool 20 |
|2|V3 tickSpacing-keyed (fid 0 0xE0c4, 1 0x1ac9)| fid u8, ts u24, pool 20 |
|3|Algebra (Alandale)| pool 20 |
|4|Uniswap V4| fee u24, ts u24, hooks 20 |
|5|WooPP| — |
|6|Fermi PMM| pool 20 |
|7|Tessera| — |
|8|Balancer V3 (bush)| pool 20 |
|10|Pancake Infinity CL| fee u24, parameters 32, hooks 20 |
|9|Kyber MetaAggregationRouterV2| len u16, calldata (selector ∈ {swap 0xe21fd0e9, swapSimpleMode 0x8af033fb}) |

The hop state machine reverts the whole call if any rule below is broken (I5):
- A non-terminal hop must be followed by a `CHAIN` hop.
- A `CHAIN` hop may only follow a non-terminal hop.
- The program must end on a terminal hop.
- `tout != tin`.
- Terminal hops have `tout == tokenOut`. Non-terminal hops have `tout != tokenOut`.
- Unchained hops have `tin == tokenIn`.

Pool token identity is never read from the pool. It comes from the program and is proven by the factory lookup.

## Hook policy
- **Uniswap V4:** hooks are rejected if the address flags contain `BEFORE_SWAP_RETURNS_DELTA` (1<<3) or `AFTER_SWAP_RETURNS_DELTA` (1<<2), because those hooks can change amounts. Other hooks are allowed, for example dynamic-fee or beforeSwap-only hooks such as 0xC15b… on USDG/SPCX. That pool is fork-proven: quote == fill against the V4Quoter.
- **Pancake Infinity:** a pool is rejected when `hooks != 0` and its parameters bitmap has bit 10 or 11 set (the delta-returning flags).
- **Second line of defense, both venues:** the input delta must be exactly `-a` and the output must be greater than 0.

## External surface (every function justified)
| function | why it exists |
|---|---|
| `swap(bytes)` | the only user entry |
| `fallback()` | V3-family callbacks (4 selectors). Any other selector reverts. Not payable. |
| `unlockCallback(bytes)` | required by the V4 PoolManager |
| `balancerCallback(address,address,address)` | Balancer V3 `unlock` re-entry target |
| `lockAcquired(bytes)` | required by the Pancake Infinity vault |
| 11 immutable getters | transparency (auto-generated) |

## Gas: v1 vs v0, same block, same route, measured on the fork
| route | v1 − v0 |
|---|---|
| Uniswap V3 single (WETH→USDG) | −82,356 (256,333 → ~173,900) |
| Sushi / V3 fork / V3-ts / Algebra | −82k / −82k / −89k…−91k / −82k |
| V4 USDG→CRCL / CRCL→USDG | −91,557 / −94,228 |
| WooFi / Fermi / Tessera / bush | −65,927 / −37,807 / −46,131 / −109,436 |
| WETH→USDG→CRCL (2-hop) | −103,046 |
| 4-venue split | −57,992 |

Runtime bytecode is 10,257 B at optimizer runs = 200, vs 6,826 B for v0. The extra size buys factory auth, Algebra, the V3-ts venues, Pancake CL and the Kyber sandbox.

Optimizer choice, measured on the same tests:

| optimizer runs | runtime size | gas vs runs = 200 |
|---|---|---|
| 1 | 10,220 B | — |
| 200 | 10,257 B | baseline |
| 10,000 | 11,595 B | — |
| 1,000,000 | 13,371 B | −1.0% |

We keep 200: the smallest bytecode is a design goal, and the gas delta is negligible.

`via_ir` is required: the interpreter is stack-heavy. `evm_version = cancun` is required for EIP-1153.

See SECURITY.md (threat model) and INVARIANTS.md (what proves what).
