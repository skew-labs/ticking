# ADR 0003: Ownerless, storage-free router with transient storage

- Status: accepted
- Date: 2026-10

## Context
A router holds approvals from users. Any owner key, upgrade path or persistent state is attack surface.

## Decision
- `TickingRouter` has no owner, no pause, no proxy and no storage. Its 13 dependency addresses are immutables.
- The reentrancy lock and the expected-callback authorization live in **transient storage** (EIP-1153, `cancun`).
- Routes are bit-packed programs executed by `swap(bytes)`. Pools are authenticated against their factory, or
  against PoolManager / Vault for V4-style venues. Payment is just in time inside the callback.

## Consequences
- A bug cannot be patched in place. A fix means deploying a new router and pointing the engine at it.
- Assurance comes from tests, invariants, Halmos proofs and mutation testing (`contracts/`).
