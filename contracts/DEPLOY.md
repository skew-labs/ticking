# Deploying TickingRouter to Robinhood Chain mainnet (4663)

Nothing here has been run. Broadcasting needs the user's explicit go and a key in `DEPLOYER_PK`.

## 0. Pre-flight (no key needed)
```bash
cd contracts && ./ci.sh
forge script script/Deploy.s.sol --rpc-url "$RH_RPC"
```
The second command checks all 13 constructor args against live chain state: code present, factories return the known pools, CLPM.vault() == the vault, and the Balancer vault knows the bush pool. It then stops at the `DEPLOYER_PK` read. Verified on 2026-10-25: all checks pass.

## 1. Deploy
```bash
export DEPLOYER_PK=...                                 # never echo it, never commit it, never pass it on the CLI
forge script script/Deploy.s.sol --rpc-url "$RH_RPC" --broadcast --slow
unset DEPLOYER_PK
```
Record the printed address as `ROUTER`.

## 2. Post-deploy checklist
- [ ] **Bytecode match:**
  ```bash
  forge verify-bytecode $ROUTER src/TickingRouter.sol:TickingRouter --rpc-url "$RH_RPC" \
    --constructor-args $(cast abi-encode "c(address[13])" "$(cast call ... )")
  ```
  If that command is unavailable, compare `cast code $ROUTER` to `forge inspect TickingRouter deployedBytecode`. They must be identical except at the immutable reference offsets listed in `forge inspect TickingRouter immutableReferences`.
- [ ] **Immutables:** `cast call $ROUTER "POOL_MANAGER()(address)"` and the same for all 13 getters. Each must equal `addrs()` in `Deploy.s.sol`.
- [ ] **No admin surface:** `cast storage $ROUTER 0` returns 0, and the ABI has no owner, pause or upgrade functions.
- [ ] **Smoke swap through eth_call on a mainnet fork of the deployed code:**
  ```bash
  python3 script/kyber_fixture.py    # first edit ROUTER in the script to $ROUTER
  ROUTER_ADDR=$ROUTER RH_RPC=... FOUNDRY_PROFILE=fork forge test --match-path test/ForkCov.t.sol
  ```
  `ForkCov` runs every venue (V3, the forks, Algebra, V4 plain and hooked, Pancake CL, WooFi, Fermi, Tessera, bush, Kyber) against the **deployed** bytecode. It asserts quote == fill and that the router holds 0 balance afterwards.
- [ ] **Unarmed callbacks revert:**
  ```bash
  cast call $ROUTER 0xfa461e33...   # expect revert BadCallback
  ```

## 3. Source verification on the RH Blockscout explorer
```bash
forge verify-contract $ROUTER src/TickingRouter.sol:TickingRouter \
  --chain-id 4663 --verifier blockscout --verifier-url https://robinhoodchain.blockscout.com/api/ \
  --compiler-version 0.8.26 --optimizer-runs 200 --via-ir --evm-version cancun \
  --constructor-args $(cast abi-encode "c(address[13])" "[0x8366a39CC670B4001A1121B8F6A443A643e40951,0xB055000fbE3cc9bDE7742C583a86cc6283E3fF85,0x5520385bFcf07Ec87C4c53A7d8d65595Dff69FA4,0x55555522005BcAE1c2424D474BfD5ed477749E3e,0x6131B5fae19EA4f9D964eAc0408E4408b66337b5,0x1f7d7550B1b028f7571E69A784071F0205FD2EfA,0xE51960f1B45f1C9FB6D166E6a884F866fC70433B,0xEce6eCd61177336ea6Fb9b17937AC439D85EE20B,0xE0c4ceb92d08CA985bB70fe0a22fEb121A9854A8,0x1ac9dB4a2608ba45D6127B1737949b51Bb54B7F3,0x16494A80E08Bcb9285D87b67149d7b01774D82F8,0x4F922d5B15e6691e0469663E4F5C4177f23c5FaF,0xeE04c68742e6Bf434bE8039580D2e89BBE55bc6f]")
```
Then open `https://robinhoodchain.blockscout.com/address/$ROUTER` and confirm it shows "Contract source code verified".
