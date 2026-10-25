# Contributing

1. Branch from `main` and keep each PR to one component (`contracts`, `aggregator`, `data`, `charting`, `docs`).
2. Run `make lint test` before pushing. CI runs the same targets.
3. Use [Conventional Commits](https://www.conventionalcommits.org): `feat(aggregator): …`, `fix(contracts): …`, `docs: …`.
4. Contract changes must keep `forge test` green and update `contracts/INVARIANTS.md` if an invariant changes.
   Re-run `contracts/ci.sh` (Slither, Halmos, mutation) before requesting review.
5. Engine changes that touch pool math must pass the harness oracles (`aggregator/harness/`) against a fork.
6. Never commit RPC URLs, keys, `.env` files, databases or data dumps.
