# TickingRouter v2: Robinhood Chain mainnet (chain 4663) — LIVE

| field | value |
|---|---|
| Address | `0x05d35a3b72458cD1C20832Ec4F2Cd16962d1567F` |
| Deploy tx | `0xb9ce514af3476542990455b276366882a83e64e24c1bc3893b75fdcd1801b143` |
| Block | 97273019 (2026-10-25 11:14:49 SGT) |
| Deployer | `0x3e13cd3704099424a6301Ea4125791a6a1433637` |
| Gas used | 2,610,266 |
| Deployer balance after | 0.008906033910403144 ETH |
| Fee | `FEE_BPS() == 5` (0.05%); `FEE_RECIPIENT() == 0x3e13cd3704099424a6301Ea4125791a6a1433637` (verified on-chain) |
| Compiler | solc 0.8.26, via-IR, optimizer 200 runs, EVM version cancun |
| Explorer | https://robinhoodchain.blockscout.com/address/0x05d35a3b72458cD1C20832Ec4F2Cd16962d1567F |
| Source verification | Sourcify **exact_match**, creation + runtime (match id 56911058): https://sourcify.dev/server/v2/contract/4663/0x05d35a3b72458cD1C20832Ec4F2Cd16962d1567F |
| What's new vs v1 | 5 bps protocol fee on gross out (floor); native-ETH Uniswap V4 hops |

## Post-deploy checks (2026-10-25 ~11:15 SGT)
- [x] Broadcast SUCCESS (forge script Deploy.s.sol --broadcast --slow)
- [x] codesize 11753; FEE_BPS=5; FEE_RECIPIENT=0x3e13…3637; WETH/POOL_MANAGER/KYBER getters match constructor
- [x] Sourcify **exact_match** creation+runtime (match id 56911058, 2026-10-25 11:18:27 SGT): https://sourcify.dev/server/v2/contract/4663/0x05d35a3b72458cD1C20832Ec4F2Cd16962d1567F — 
- [x] ForkCov smoke vs deployed bytecode: kyber fixture refreshed for ROUTER_ADDR; `FOUNDRY_PROFILE=fork forge test --match-path test/ForkCov.t.sol` → **27/27 pass** (2026-10-25 ~11:18 SGT)

Engine: `engine2.rs` defaults `ROUTER_V2` to this address (overridable via env `ROUTER_V2`). The public API (`api.ticking.dev/api/router/quote`) returns this router as `tx.to`; `?router=v1` targets the superseded v1 address.

---

# Superseded — TickingRouter v1 (no fee)

# TickingRouter v1 (superseded)

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
