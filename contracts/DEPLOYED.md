# TickingRouter: Robinhood Chain mainnet (chain 4663)

| field | value |
|---|---|
| Address | `0x9745fF56A6FC085B3e70A7509cd49Ab6b97dCBD6` |
| Deploy tx | `0x2440a58c7937491ab97eb9992403c5aaedc0b28a2636421d2a03f2b2324cd908` |
| Block | 96981545 (2026-10-25 02:47:36 SGT) |
| Deployer | `0x1c8F822682DDAbFF7dA1A7D5D881bdA96c8F41C5` (nonce 4 → 5) |
| Gas used | 2,278,361 at 0.020026 gwei = 0.000045626457386 ETH |
| Deployer balance after | 0.002163549275904883 ETH |
| Compiler | solc 0.8.26, via-IR, optimizer 200 runs, EVM version cancun |
| Constructor args | `verify/constructor_args.hex` (the 13 addresses in `script/Deploy.s.sol::addrs()`) |
| Source verification | Sourcify **exact_match**, creation + runtime (match id 56865093): https://sourcify.dev/server/v2/contract/4663/0x9745fF56A6FC085B3e70A7509cd49Ab6b97dCBD6 |
| Explorer | https://robinhoodchain.blockscout.com/address/0x9745fF56A6FC085B3e70A7509cd49Ab6b97dCBD6 |

## Post-deploy checklist (all done 2026-10-25, about 02:48–02:51 SGT)
- [x] **Pre-deploy:** `./ci.sh` green at fresh block 96979287, after refreshing the Kyber fixture. All 13 constructor args passed the on-chain checks in `Deploy.s.sol`.
- [x] **Bytecode match:** the on-chain runtime is 10,257 B, the same length as local. With the 30 immutable references (13 immutables) masked, **0 bytes differ**.
- [x] **Address getters:** all 13 equal the constructor args.
- [x] **No admin storage:** storage slot 0 is `0x0`. The contract has no owner, pause or upgrade functions.
- [x] **Smoke test:** `ROUTER_ADDR=0x9745… forge test --match-path test/ForkCov.t.sol` ran against the **deployed** code: 27/27 pass. Every venue was exercised, including Kyber with a fixture built for this address.
- [x] **Unarmed callbacks revert:** all 4 V3-style callback selectors, `unlockCallback` and `lockAcquired` revert with `BadCallback()` (0x6c25f7f8) for a nonzero caller, including the real pool and PoolManager calling while unarmed.
  - Note: an eth_call from `from = address(0)` reverts with `PartialFill`/`TransferFailed` instead, because `address(0)` equals the empty transient slot. This can't be reached by a real transaction, since no transaction can have `msg.sender == 0`, and it moves no funds.
- [x] **Source verification:** Blockscout's API is behind a Cloudflare challenge from this machine (HTTP 403), so I verified via Sourcify instead, which returned an exact match. Blockscout normally imports Sourcify matches. If it doesn't show as verified, upload `verify/TickingRouter.standard-input.json` and `verify/constructor_args.hex` in the Blockscout UI (Verify & Publish → Standard JSON input).

Only the single deploy transaction was broadcast. The key was read from the environment only; it doesn't appear in any file in this repo (`broadcast/` and `cache/` were scanned).
