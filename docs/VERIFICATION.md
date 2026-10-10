# Verification

`scripts/build-native.sh` compiles the public source and pinned wire interface. `scripts/check.sh` exercises native ordering/deduplication, durable storage/failure recovery, source decoding/projection, candle quality, API entitlements, prepaid accounting, shared stream delivery and browser-module math/contracts.

All checks use isolated temporary directories and mocked payment receipts. No production database, account credit, subscription, wallet signature or financial transaction is created. Fixture bars are explicitly tests, not evidence of real market coverage.

The selected source passed 108 engine/bridge tests, 95 API/accounting tests and 47 independent SDK tests. Additional checks cover the native journal, compression capsules, injected storage failures, a local TLS reconnect/replay feed, live-venue input validation, indicator math and browser-feed contracts. Exact results and toolchain versions are recorded in [`verification.json`](../verification.json).

The standalone Pro example was opened in a browser against the explicitly selected public read API. Its market list, SOL candles, drawing controls and execution table rendered. This is a source-preview check of one market, not a claim that every listed asset has complete history. No captured production dataset is included in this repository.

Browser smoothness, long-duration ingestion, whole-chain coverage, complete trade replay, all-history acquisition, data rights and high availability are distinct acceptance gates. They are not inferred from the offline suite.
