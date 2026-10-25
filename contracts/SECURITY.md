# TickingRouter v1 — Threat model (UNAUDITED; not deployed)

## Actors and trust
- **Caller/payer**: `msg.sender`, untrusted. Can only spend its own tokens (I1). A malicious program can only hurt its sender.
- **Recipient**: arbitrary, except `0` and the router itself.
- **Trusted immutables**:
  - Uniswap V4 PoolManager, Balancer V3 vault, WooPP, Tessera, Kyber router.
  - Six factories: Uniswap V3, Sushi V3, fork 0xEce6, tickSpacing forks 0xE0c4 and 0x1ac9, Algebra 0x1649.
- **Untrusted**:
  - Every pool address in a program. V3, ts and Algebra pools are authenticated through their factory before any call.
  - Fermi pools: approval-sandboxed and delta-measured.
  - Every token.
  - Kyber calldata.
- **No admin.** No owner, pause, upgrade or sweep. Stray donations to the router are not recoverable. That is accepted: the router has no balance at rest by construction.

## External calls (complete list)
- **Tokens:**
  - `transferFrom(msg.sender, pool|vault|PM|router, a)` and `transfer(pool|vault|PM|recipient, a)`.
  - `approve(venue, a)` only for Fermi, Tessera and Kyber. The remaining allowance must read 0 after the call, otherwise the call reverts.
  - `balanceOf` and `allowance` reads.
- **Factories:** `getPool` and `poolByPair` view calls.
- **Pools and venues:**
  - `IV3Pool.swap`, `PM.unlock/swap/sync/settle/take`, `Vault.unlock/swap/settle/sendTo`.
  - `WooPP.swap`, `Fermi.swapExactIn`, `Tessera.tesseraSwapWithAllowances`.
  - A Kyber call to the fixed router with a whitelisted selector.

## Vulnerabilities found in v0 and fixed in v1
1. **(High) Unauthenticated V3 pools.** `expectedCallback = o.target` came from the caller, so any contract could act as a "pool". v1 verifies every pool via its factory and pins the token pair and fee.
2. **(High) Pool return values were trusted for chained amounts and token identity.** v0 read token0/token1 from the pool. v1 takes tokens from the program, verified by the factory, and measures untrusted venues by balance delta.
3. **(High) Partial fills stranded intermediate tokens in the router.** Hitting the price limit consumed less than `a`, and v0 only refunded tokenIn. v1 enforces exact-in on every venue (I6), and the state machine forbids unconsumed intermediates (I5).
4. **(Med) Callback could be paid twice in one hop.** v1 auth is single use (`_consume` clears `T_POOL`), and `_disarmed` asserts the callback actually fired.
5. **(Med) Approval-venue partial pulls** left lingering allowances and dust. v1 requires the allowance to be exactly 0 after the call.
6. **(Med) Fee-on-transfer tokenIn** silently under-filled. v1 rejects it on pull (`BadToken`); V3 pools reject it themselves.
7. **(Med) Untrusted WooFi/Fermi/Tessera return values were used as the next hop's amounts.** v1 uses deltas instead.
8. **(Low) No path for Kyber, or an unsafe one.** v1 sandboxes it:
   - fixed target and 2 allowed selectors;
   - exact approval, required back to 0 afterwards;
   - output measured at the router, then forwarded;
   - a re-entering executor hits `T_LOCK`, and callbacks are unarmed.
9. **(Low) Storage-based lock and callback state.** About 40–60k gas went to SSTOREs. v1 uses transient storage.
10. **(Med, v1-internal, found by Halmos)** Kyber calldata could return tokenIn to the router: the allowance was fully consumed but the funds stayed stranded. Fixed with an exact router tin-balance check after every approval venue.
11. **(Info) Behaviour cleanups:**
    - Revert strings replaced with custom errors.
    - Token revert reasons normalized to `TransferFailed`, so reverts are clean and funds stay with the payer.

## Token quirks
- **Blacklist, pausable, false-returning or no-code tokens:** the whole call reverts and the payer keeps their funds (unit tests).
- **USDC (bridged, 0x80e0…6cA8, 6 decimals, supply ≈ 340) and USDG (6 decimals, proxy):** both work as input and output (fork tests, both directions and via stocks). Liquidity caveat: the only USDC/USDG pool is a V4 pool with a 92% fee.
- **Rebasing tokens:** unsupported in principle. Exact-in checks revert if a balance changes mid-call.
- **Native ETH:** unsupported. WETH only, no `payable`, no `receive`.

## Slither
- **v0:** 48 findings (3 High reentrancy-balance, 2 incorrect-equality, 2 uninitialized-local, 4 unused-return, 1 write-after-write, …).
- **v1:** 39 findings, 0 High and 0 Medium.
- **Remaining v1 findings, justified:**
  - 12× calls-loop: inherent to a router.
  - 1× timestamp: the deadline check.
  - 9× assembly: intentional and commented.
  - 11× naming: uppercase immutables.
  - 2× cyclomatic complexity: the interpreter.
  - 1× low-level call: Kyber, sandboxed.
- **unused-return:** 7 occurrences are suppressed inline, each with a written justification. Outputs are measured by balance delta and exactness is enforced separately.

## Residual risks (honest)
- **No external audit by decision of the owner.** The evidence instead: Slither, 5 Halmos proofs, a 99.35% mutation kill rate, 68 unit/fuzz/invariant tests and 57 mainnet-fork tests.
- **V4 and Pancake hooked pools with delta-returning flags are rejected by design.** That currently excludes the 0x4E34… and 0xe5e7… hook families.
- **Trusted-venue risk:** a compromised Kyber, Woo or Tessera can lose at most the in-flight amount of that hop, and `minOut` still binds.
- **Fork coverage:** test pools can expire with RH history, so fork tests use the latest block (`FORK_BLOCK` pin).
- **Pancake Infinity CL is supported (op 10).** Fork-proven: quote == fill against the CLQuoter in both directions, plus a second pool.
