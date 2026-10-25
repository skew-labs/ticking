# Invariants and how each is checked

| # | Invariant | Symbolic (Halmos) | Fuzz / invariant (Foundry) | Fork (RH mainnet) |
|---|---|---|---|---|
| I1 | Payer == msg.sender. No program can debit a third party, even one holding a max approval to the router. | `check_payerIsSender`, `check_singleOp` (victim untouched) | `invariant_victimUntouched` (128k calls), `testFuzz_decoder`, `test_venueCannotUseVictimApproval`, `test_kyberBadSelector` | — |
| I2 | Router token balance and allowance are 0 after every call, for every touched token. | `check_singleOp` | `invariant_routerHoldsNothing`, `_clean()` in every unit test | asserted in every `_v1` fork run |
| I3 | Success ⇒ recipient's tokenOut delta ≥ minOut, and `out` == that delta. | `check_singleOp` | `invariant_minOut`, `test_slippage`, `test_feeOnTransferOut_minOutOnDelta`, `test_poolLiesAboutOutput_minOutHolds`, `test_kyberSendsElsewhere` | every fork test |
| I4 | Callbacks only pay the armed pool: factory-verified or an immutable singleton, single use, exact amount. | `check_callbackAuth` (any caller, any args, all 4 selectors + unlockCallback) | `test_unarmedCallback`, `test_fakePoolNotInFactory`, `test_wrongFeeForPool`, `test_unknownFactoryId`, `test_poolCallbackTwice`, `test_poolNoCallback`, `test_poolAsksMore`, `test_shortCallbackData` | `test_fork_fakePool_rejected` |
| I5 | Token identity is never silently swapped (state machine). | `check_singleOp` (symbolic opcode/flags/tout) | `test_identity_*`, `test_chainWithoutPending`, `test_pendingNotConsumed`, `test_badOpcodes`, `test_emptyAndTruncated`, `testFuzz_decoder` | — |
| I6 | Exact-in is exact: every hop consumes exactly `a` (quote == fill). Fee-on-transfer input is rejected. | — | `test_poolPartialFill`, `test_fermiPullsLess`, `test_kyberPullsLess`, `test_feeOnTransferIn_*`, `testFuzz_split` (exact output formula) | v1 == v0 to the wei on 15 venue/route tests + `testFuzz_fork_split`. V3 == QuoterV2 and V4 == V4Quoter to the wei. Kyber fill == Kyber quote. |
| I7 | No re-entry. | — | `test_reentrancyLock`, `test_reentrantToken` | — |

**Additional Halmos proofs:**
- `check_twoOps`: every combination of CHAIN/TERM flags, amounts, intermediate and terminal tokens, and V3/Fermi venue choice (246 paths).
- `check_kyberPayload`: Kyber calldata with fully symbolic content, meaning selector, tokens, amounts, receiver and pull mode (325 paths).

**Bug caught by Halmos:** `check_kyberPayload` found that Kyber calldata routing tokenIn back to the router left tokenIn stranded there. The fix: approval venues must now leave the router's tin balance exactly at `pre`.

**Mutation testing** (`script/mutate.py`, 154 mutants):
- 153 valid; 1 did not compile.
- 152 killed (99.35%).
- The only survivor is `tin < tout` → `tin <= tout`. It is equivalent, because `tin != tout` is enforced first.
- That makes 100% of the non-equivalent mutants killed.

**Coverage:**
- Lines: 195 of 199 non-assembly lines (98%). The 4 lines reported as missed are `_disarmed()` calls. They are mis-attributed by `--ir-minimum` source maps: they run on every callback hop, and deleting them is a killed mutant.
- Branches: 99 of 109 (90.8%). The 10 missed are all inside assembly blocks, which forge cannot attribute. Each has a dedicated test:
  - lock: `test_reentrancyLock`, `test_lockReleasedSameFrame`
  - balanceOf failure: `test_noCodeToken`
  - transfer failure: `test_falseReturningToken`
  - transferFrom failure: `test_pausedToken`, `test_blacklistedRecipient`
  - approve: all approval-venue tests, and mutants of the approve success check are killed
- Functions: 100%.

**Scope limits:**
- Halmos runs against mock venues, not real pool code.
