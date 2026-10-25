# ADR 0002: Own routing engine, Kyber only when it wins after gas

- Status: accepted
- Date: 2026-10

## Context
Aggregator APIs are convenient, but they do not let us prove that quote equals fill, and their latency is outside our control.

## Decision
- Ticking routes with its own engine and its own router contract by default.
- A KyberSwap route is used only when its calldata, **simulated at the same block** and **netted for gas**,
  returns more than Ticking's best route.
- Kyber calls run inside a sandbox in the router: 2 allowed selectors, an exact approval that must read 0
  afterwards, and output measured as a balance delta.

## Consequences
- Our own routes are verified as quote == fill to the wei (see the gate runs in `aggregator/ENGINE.md`).
- The comparison is fair because both sides are evaluated at the same block with the same gas model.
