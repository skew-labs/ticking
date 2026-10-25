# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through **GitHub Security Advisories**:
<https://github.com/skew-labs/ticking/security/advisories/new>. Do not open public issues for security
reports. We aim to acknowledge a report within 72 hours.

## Threat model (summary)

**TickingRouter** (`contracts/`, details in [`contracts/SECURITY.md`](contracts/SECURITY.md) and
[`contracts/INVARIANTS.md`](contracts/INVARIANTS.md)):

- **No privileged surface.** There is no owner, admin, pause, proxy or storage. Every dependency address is immutable.
- **Callbacks.** A pool callback is accepted only from a factory-derived pool address that matches the
  expected-callback slot in transient storage.
- **Funds.** The router never holds a balance between transactions. It pulls tokens from the user only
  inside the callback, for exactly the amount owed, and enforces `minOut` on the measured balance delta.
- **Kyber fallback.** Only 2 selectors are allowed. The approval is exact and must read 0 afterwards, and
  output is measured as a delta, never taken from return data.
- **Reentrancy.** A lock in transient storage (EIP-1153) guards against reentrancy.

**Engine and data services:**

- Quotes are advisory. The contract enforces every user guarantee (`minOut`, recipient, deadline).
- RPC credentials, signing keys and storage tokens are read from environment variables or root-only files.
  None of them is committed.

## Assurance

Foundry unit, fork, fuzz and invariant tests, Halmos symbolic proofs, Slither (0 High/Medium findings) and
mutation testing (152/153 mutants killed). There has been no external audit.
