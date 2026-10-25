# ticking-engine (Robinhood Chain) — runbook  [NOT DEPLOYED; box only; eth_call only; no keys/txs]

Build:  `cd core && cargo build --release`
Run:    `./core/target/release/engine`   (cwd = repo root; needs `.venv` for the snapshot fetcher)
Env:    RH_RPC (default public RPC), ENGINE_BIND=127.0.0.1:8782, ENGINE_POLL_MS=300, ENGINE_VERIFY_EVERY=20 (polls),
        ENGINE_RESYNC_S=90, ENGINE_STALE_BLOCKS=30, ENGINE_MAX_GAP=20000, PYTHON=.venv/bin/python
Endpoints: /quote?tin&tout&amt(wei)&mode=best|fast[&gp] · /pool_quote?pool&zfo&amt · /health (503 if lag>=50) ·
        /metrics (Prometheus) · /freshness (pool -> last event block) · /divergences
Soak/gates: `.venv/bin/python service/soak.py <secs>` → data/live/soak.json ; `./ci_gates.sh`

Update model: poll head → eth_getLogs(all venue addrs + V4 PoolManager) → exact in-memory apply
(V3-family/Algebra Swap/Mint/Burn, V4 Swap/ModifyLiquidity) → dynamic fee() refresh for up-v3/Algebra pools
(one Multicall3 per poll, exact at the applied block) → every N polls Multicall3 verify slot0/globalState+liquidity of
every address-keyed pool; mismatch ⇒ pool disabled (liquidity=0 in published state) + background snapshot resync.
Reorg: tip hash re-checked; rollback to newest of 64 ring snapshots whose hash still matches; else full resync.
Stale gate: /quote returns 503 when state lags head by > ENGINE_STALE_BLOCKS.
Known limits: no public WS / sequencer feed on RH (probed: wss rpc 4xx, feed.mainnet 400) ⇒ HTTP polling;
public RPC rate-limits (429) bound poll rate; V4 pools are not in the slot0 verification (event-applied only);
snapshot fetcher is Python; pool universe is static (new pools need a universe refresh).

## engine2 (2026-10-25) — event-driven, `core/src/bin/engine2.rs`
- Trigger: QuickNode WSS `newHeads` (endpoint from `.env.rpc`, never logged; `redact()` scrubs URL+host from every log line).
  Per head: `eth_getLogs{blockHash}` on the same WSS + dynamic-fee Multicall3 @blockHash on a 2nd WSS (parallel). Apply in block order (BTreeMap queue, no coalescing).
- QN plan limit measured: **50 req/s** (code -32007; batch items count). Budget: ~10 heads + 10 getLogs + 10 fee calls/s; logs push subscription OFF by default (WS_LOG_PUSH=1) because notifications push us over the cap.
  QN pushes ~3% of logs after the next head (measured 53/1663), so push can't finalize a block; getLogs-by-hash is the source of truth.
- Gap filler: public RPC poller (400ms) injects heads only if WSS silent >700ms / behind; small holes (<=3 blocks, <600ms) wait for the in-flight WSS fetch;
  larger gaps: ONE batch range backfill (getLogs from..to + both boundary headers; parent link + hash binding checked).
- Reorg: parentHash check per head; mismatch -> verify our tip vs canonical; roll back via 64-entry ring; else retry (no more hash clearing).
- Verify: async thread every 20 blocks, Multicall slot0+liquidity (V3-family) and PoolManager.extsload (V4: slot0 + slot+3 liquidity, pools mapping slot 6).
- Universe: live PoolCreated (5 factories) + V3 Initialize + V4 Initialize (zero-hook only) applied exactly. Full historical scans: data/rh_v3_pools_scan.json (458,248 pools), data/rh_v4_keys_scan.json (1,072,702 keys, 368,789 hooked).
- Quotes: FAST port 8783 (pinned cores, 32-path cap, 4 chunks, no local search, step cap 120) and BEST port 8782 (384-path cap, 32 chunks, LS with 20ms deadline).
  Path cache keyed on structural topology epoch (only changes on new pools). Zero-liquidity tick ranges fast-forwarded exactly; swaps non-panicking (`swap_try`).
- 1e21 "univ3 fuzz revert" root cause: USDG token itself reverts transfers >= 1e20 raw (custom error 0x356680b7; 1e18 ok, 1e20 revert, verified by eth_call with balance override). Not a pool-math bug.
- USDC: canonical = 0x80e0e24718dbFcad49ECAA6F1e6C89A190586cA8 (L2 StandardGateway/Router.calculateL2TokenAddress(L1 USDC) == this; l1Address()==0xa0b8…eb48; 6 dec; totalSupply 340.54).
- Deploy: t1 `ticking-engine-test` (systemd-run transient, user `ticking`, 127.0.0.1:8782/8783, /opt/ticking-engine). Persistent unit file installed but disabled.

## FINAL gate run — soak6, 2026-10-25 04:04–04:30 SGT (t1 test unit `ticking-engine-test`, 127.0.0.1:8782/8783)
Build: engine2 with dual-QuickNode newHeads race, header-logsBloom getLogs skip, fees only for touched pools (separate fee socket),
verifier on QN2→QN1 (public last), liquidity-filtered zero-hook universe (~850–917 pools), USDC V4 keys, /pool_quote gas cap,
PancakeV3-style Swap topic (giga) + Ramses Mint/FeeAdjustment decoding, rebuild swap-in exact reconcile, tx.to/data for the deployed router.
Load: service/load.py on t1 (8 best + 2 fast threads) + service/soak.py from the box over an SSH tunnel.

| metric (soak window, engine-side counter deltas) | value | target | |
|---|---|---|---|
| internal lag0 (all quotes) | 96.13% (295,516 / 307,404); lag≤1 99.99% | ≥95% | PASS |
| soak-client lag0 / lag≤1 | 96.6% / 100% (920 samples) | | |
| verifier | 759 runs, 610,038 pool checks, **0 diverged**, 0 verify errors | 0 | PASS |
| proofs (Exec, quote==fill at quote block) | 57/57 exact, 0 skip | exact | PASS |
| fuzz (pool_quote vs exec) | 10/10 | clean | PASS |
| **deployed TickingRouter 0x9745…CBD6** eth_call `swap(bytes)` == grossOut | **63/63 exact**, 0 fail | exact | PASS |
| fast compute p50/p99/p999 (engine, all reqs) | 25 µs / 1.34 ms / 3.97 ms | p99 <2 ms | PASS |
| best compute p50/p99 (engine) | 6.0 ms / 27.9 ms (soak: 3.8 / 45.7 ms) | | |
| head→quotable p50/p99 (engine lifetime) | 1.5 ms / 16.4 ms | | |
| WS reconnects / reorgs / stale rejects | 0 / 0 / 0 | | |
| head wins ep1 / ep2 | 7,920 / 7,251 | | |
| bloom-skipped getLogs | 8,966 of 15,171 heads (59%) | | |
| fee multicall updates | 15,087 | | |

## LIVE (2026-10-25 08:16 SGT)
- t1 `ticking-engine.service` enabled (drop-in prune.conf: PRUNE_STATES=1), build with startup exact reconcile (startup_reconciled patched=0 at 97167738). Previous binary: /opt/ticking-engine/engine2.prev. Test unit stopped.
- nginx: +1 include line in /etc/nginx/sites-enabled/ticking-api (backup /etc/nginx/bak/ticking-api.enabled.bak-router-20261025-011226), snippet /etc/nginx/snippets/ticking-router.conf, zone /etc/nginx/conf.d/ticking-router-zone.conf (20 r/s, burst 40).
