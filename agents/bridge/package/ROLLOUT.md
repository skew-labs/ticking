# Ticking: Arbitrum One + Robinhood Chain as first-class networks — production rollout

**Status:** staging complete and green; **nothing in production has been changed**. The controller runs every step below.

**Design.** One chart contract, a store per network:
- The engine bridge dispatches `network=arbitrum|robinhood` through a `READERS` map to `EvmHistory` (`evm_history.py`):
  - hot suffix: `/srv/skew/em-charts/hot/<chain>/history.sqlite3`, read-only, the observed store's fills/bars DDL;
  - deep history: the em-charts loopback archive API (`127.0.0.1:8895`).
- It returns the same dict shape as the Solana readers, with `identity.network`.
- The console API, SDK and frontend carry `network`; market ids are `<network>:<0x address>`.
- The `RH-`/`ARB-` exchange relabel is gone.
- The bridge on `:8896` (`em-ticking-bridge`) now serves only the wallet layer (`/api/public/em/*`: wallet, holders, trace, trades, tx tools and galaxy reads).

**Economic Machine and UX layer (same release):**
- **Economy panel:** a workspace data tab (`?panel=economy`) and a token-page section, for EVM markets. It shows:
  - money state (where the supply sits, with a stacked bar);
  - leverage and liquidations (the ladder and the nearest positions, or the Morpho Blue borrowers until a ladder is served);
  - flows (1h/24h/7d by wallet class);
  - notable wallets, each opening the wallet dossier.
- **Source and freshness:** every section shows its source (Live analytics or Map snapshot) and its as-of time, relative and in UTC.
- **Chart overlays:** economic-event markers (on by default) and liquidation bands (off by default), with a toggle legend. They follow zoom, pan and interval changes.
- **Assistant intents:** "why did X move", "liquidation levels for X", "who is leveraged on X" and "money flow for X" return an Economic Machine card with "Open Economy panel" and "Show on chart". Every number comes from the APIs.
- **Readable answers:**
  - "Where it sits" (protocol venues, e.g. Uniswap V4 · Pool manager 33%, Morpho Blue 7.9%) is separate from "Notable wallets" (EOAs, Safes, whales and MEV bots, ranked by position size, with a role such as "Borrows $617.5K USDG against NVDA on Morpho Blue").
  - Registry keys are humanised everywhere.
  - "Why did X move" opens with one sentence built only from API numbers that ties the price change to flows, lending and the nearest liquidation cluster. It then lists the top events (UTC), the net flow by class and the cluster.
  - The assistant placeholder and suggestions follow the open market.
  - The overlay legend offers "Fit events".
- **UX fixes:**
  - The Markets preview follows the filtered or searched selection.
  - Network chips, and `#/markets?network=…` deep links.
  - One network badge (Solana / Arbitrum One / Robinhood Chain) in lists, headers and the terminal.
  - Clear stock names (e.g. "NVIDIA · Robinhood stock token") with no truncation on desktop.
  - Home entry points for the new networks, the Economic Machine and the wallet map.
- **Data sources:**
  - Economy data comes from em-charts token analytics (`/api/public/em/{token-state,liquidations,flows,events,token-brief}`) as soon as each endpoint answers.
  - Until then, the wallet bridge serves the em-galaxy map-snapshot equivalent and labels it as a snapshot. No other change is needed when the endpoints land.

**Safety by construction:**
- **Opt-in EVM rows:** they appear in `/api/public/markets` and `/api/public/stream/markets` only with `networks=all`, which only the new frontend and the new resolver send. Already-open tabs and a rolled-back frontend never receive rows they would reject.
- **Solana is byte-identical:** a request without `network` takes exactly the existing code path. This is proven by `tests/solana_regression.py` (closed windows compared byte-for-byte). The only intended difference is that Solana catalog rows gain an explicit `network: "solana"`.
- **Isolated lanes:** EVM reads use their own bridge lanes and never enter the Solana history process pool.
- **Bounded archive stalls:** if the archive stalls, the last good copy of the same read is served.

## Bases (re-snapshotted 2026-10-19 after the production drift; re-checked by `deploy/verify_base.sh` before every step)

| File | sha256 prefix |
|---|---|
| solana-mingle `ticking_bridge.py` | `07bd42202516ee68` (adds the chart listener on port+1) |
| solana-mingle `commit_notifications.py` | `49d320878a4a9cfc` |
| `server/app.py` | `b5baf985c0184824` |
| `server/engine_data.py` | `b7f3adf415a42773` (adds `chart_client`) |
| `server/market_data.py` | `0ecb22c556354f33` (adds `?view=identity`) |
| `server/realtime.py` | `95138fc572e94380` |
| `server/changes.py` | `d542ae923ef27fdd` |
| `server/token_icons.py` | `8cee1e18dfac6622` |
| `server/store.py` | `796ae0da3c2f8b7e` |
| `server/reviewed_markets.json` | `064534dbcaaa4613` |
| `qa/refresh-token-icons.py` | `69d2ecc36cdef9c2` |
| resolver `intelligence_app.py` | `3c4330255467f9eb` |
| resolver `analyst_service.py` | `d914b980a1481ba8` |

- **Public release:** `.releases/ticking-market-panel-20261019-02` (adds `market-identity.js`, `workspace-markets.js`).
- **Resolver release:** `intelligence-analyst-20261017-10`.
- **nginx `ticking-api`:** `a8698af1f2b0da17`.
- The full list is in `package/BASE.sha256`.

**Rebase onto the 2026-10-19 drift (Codex):**
- Engine: the history-compute split (`foreground_compute`/`history_compute`) keeps EVM out of both process pools (`... and compute and not evm`). The new chart listener (port+1) dispatches EVM through the same `query()`.
- Console: `?view=identity` gets an EVM branch (`evm_data.identity`: a static reviewed catalog row, with no engine, pool or price read).
- Frontend: `market-identity.js` accepts `<network>:<0x address>` identities; `workspace-markets.js` gains the Arbitrum One / Robinhood Chain sources (terminal filter plus origin label); the terminal asset list keeps the new origin label, with the network icon for EVM rows.

**Pinned package:** `/srv/skew/ticking-em-staging/fc/package`. The `PACKAGE.sha256` digest is printed by step 0. `install_bridge.sh` refuses any other package.

## Steps

All commands run as root on skew unless marked **[Mac]**. Stop at the first failed check.

```bash
P=/srv/skew/ticking-em-staging/fc/package; R=/srv/skew/tick-market-ui-20261007; E=/srv/skew/solana-mingle-20261007
K=/srv/skew/em-ticking-bridge/backups/$(date +%Y%m%d-%H%M%S); mkdir -p $K
```

### 0. Pre-flight (read-only)

```bash
( cd $P && sha256sum -c --quiet PACKAGE.sha256 && sha256sum PACKAGE.sha256 )   # digest must match the report
$P/deploy/verify_base.sh                     # every line "ok"; any DRIFT = stop, rebase (apply_*_patch.py --check), re-test
systemctl is-active em-map-charts em-map-galaxy ticking-engine-bridge ticking-console ticking-intelligence nginx
curl -s 127.0.0.1:8895/api/health | head -c 200                     # em-charts ok, lag < 15 min
df -h / | tail -1                                                    # ≥ 60 GB free
python3 $P/tests/solana_regression.py --record $K/solana-baseline.json http://127.0.0.1:19428   # Solana baseline before any change
```

### 1. Engine bridge: network dispatch and EvmHistory (`ticking-engine-bridge` restart)

```bash
$P/deploy/verify_base.sh engine && cp -p $E/ticking_bridge.py $E/commit_notifications.py $K/
python3 $P/apply_engine_patch.py $E --check && python3 $P/apply_engine_patch.py $E
chown ubuntu:ubuntu $E/identity.py $E/evm_history.py
( cd $E && tools/live-python/bin/python -m py_compile ticking_bridge.py commit_notifications.py identity.py evm_history.py )
systemctl restart ticking-engine-bridge
```

Health:

```bash
T=$(cat $R/.state/engine-bridge.token); B=http://127.0.0.1:19643
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" "$B/token/candles?token=So11111111111111111111111111111111111111112&interval=60&limit=5"   # 200
curl -s -H "Authorization: Bearer $T" "$B/token/candles?network=robinhood&token=0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec&interval=900&limit=5" | python3 -c "import json,sys;print(json.load(sys.stdin)['identity'])"   # network robinhood, historyPolicy em_pool_reference_v2
curl -s -H "Authorization: Bearer $T" "$B/changes?wait=0" | python3 -c "import json,sys;v=json.load(sys.stdin)['versions'];print('evm_arbitrum' in v,'evm_robinhood' in v)"   # True True
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" "http://127.0.0.1:19644/token/candles?network=robinhood&token=0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec&interval=900&limit=5"   # 200 (chart listener, port+1)
journalctl -u ticking-engine-bridge --since -2min | grep -c read_failed     # ~0
```

Rollback:

```bash
cp -p $K/ticking_bridge.py $K/commit_notifications.py $E/ && rm -f $E/identity.py $E/evm_history.py && systemctl restart ticking-engine-bridge
```

### 2. Console API, catalog and icons (`ticking-console` restart)

```bash
$P/deploy/verify_base.sh "ui-server qa"
cp -p $R/server/{app,engine_data,market_data,realtime,changes,token_icons,store}.py $R/server/reviewed_markets.json $R/qa/refresh-token-icons.py $K/
python3 $P/apply_ui_patch.py $R/server --check && python3 $P/apply_ui_patch.py $R/server   # + evm_data.py, evm_catalog.json, 277 pinned reviews
python3 $P/apply_qa_patch.py $R --check && python3 $P/apply_qa_patch.py $R                 # icon job: --networks; keeps networkIcons on hourly runs
mkdir -p $R/evidence/evm-artwork-20261018 && cp -a $P/artwork/. $R/evidence/evm-artwork-20261018/ && chown -R ubuntu:ubuntu $R/evidence/evm-artwork-20261018
runuser -u ubuntu -- $R/.api-venv/bin/python $P/tools/refresh_network_icons.py $R $R/.state/token-icons $R/evidence/evm-artwork-20261018   # networkIcons 277, failures {}
( cd $R && .api-venv/bin/python -m py_compile server/*.py )
systemctl restart ticking-console
```

Health:

```bash
python3 $P/tests/solana_regression.py --compare $K/solana-baseline.json http://127.0.0.1:19428     # 9/9 pass
curl -s 'http://127.0.0.1:19428/api/public/markets?view=table' | python3 -c "import json,sys,collections;print(collections.Counter(t.get('network') for t in json.load(sys.stdin)['tokens']))"                 # solana + exchange only
curl -s 'http://127.0.0.1:19428/api/public/markets?view=table&networks=all' | python3 -c "import json,sys,collections;print(collections.Counter(t.get('network') for t in json.load(sys.stdin)['tokens']))"   # + robinhood 217, arbitrum 60
curl -s 'http://127.0.0.1:19428/api/public/engine/candles?network=robinhood&mint=0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec&aggregation=token&interval=15m&limit=5' | head -c 160   # executed_history, network robinhood
curl -s 'http://127.0.0.1:19428/api/public/markets/robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec?view=identity' | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['network'],d['symbol'],d['verification']['status'])"   # robinhood NVDA reviewed (static, no engine read)
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' "http://127.0.0.1:19428$(curl -s 'http://127.0.0.1:19428/api/public/markets/robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec' | python3 -c "import json,sys;print(json.load(sys.stdin)['icon'])")"   # 200 image/webp (NVIDIA tile)
```

Rollback:

```bash
cp -p $K/{app,engine_data,market_data,realtime,changes,token_icons,store}.py $K/reviewed_markets.json $R/server/ && cp -p $K/refresh-token-icons.py $R/qa/ && rm -f $R/server/evm_data.py $R/server/evm_catalog.json && systemctl restart ticking-console
```

Notes:
- `display_tokens` keeps its added `network` column (default `solana`). Old code ignores it.
- The `networkIcons` manifest key and the files under `.state/token-icons` are inert for the old code.

### 3. Wallet-layer bridge (new unit `em-ticking-bridge`, loopback 8896)

```bash
bash $P/deploy/install_bridge.sh $P                     # verifies PACKAGE.sha256, writes token-index.json from networkIcons
cp /srv/skew/em-ticking-bridge/deploy/em-ticking-bridge.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now em-ticking-bridge
```

Health:

```bash
curl -s 127.0.0.1:8896/api/public/em/health                                    # ok true, catalog 277, upstreams galaxy/charts true
curl -s '127.0.0.1:8896/api/public/em/tools/holders?market=robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec' | head -c 160
curl -s '127.0.0.1:8896/api/public/em/tools/economy?market=robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec' | python3 -c "import json,sys;d=json.load(sys.stdin);print({k:d[k]['status'] for k in ('state','leverage','flows')})"   # live or snapshot each
ss -ltn | grep ':8896 ' | grep -v 127.0.0.1 && echo NOT-LOOPBACK || echo loopback-only
```

Rollback:

```bash
systemctl disable --now em-ticking-bridge
```

### 4. nginx: one location, `/api/public/em/` → 8896 (`nginx -t` + reload, never restart)

```bash
python3 /srv/skew/em-ticking-bridge/deploy/nginx_em_patch.py apply && nginx -t && systemctl reload nginx
```

Health:

```bash
curl -s https://api.ticking.dev/api/public/em/health | head -c 120
curl -s -o /dev/null -w '%{http_code}\n' 'https://ticking.dev/api/public/markets?view=table'   # 200, unchanged
```

Rollback:

```bash
python3 /srv/skew/em-ticking-bridge/deploy/nginx_em_patch.py revert && nginx -t && systemctl reload nginx   # byte-identical restore
```

### 5. Resolver release (new folder + drop-in, `ticking-intelligence` restart)

```bash
$P/deploy/verify_base.sh intelligence
L=$(systemctl show ticking-intelligence -p WorkingDirectory --value); NEW=intelligence-em-networks-$(date +%Y%m%d)-01
cp -a $L $R/.intelligence-releases/$NEW
python3 $P/apply_resolver_patch.py $R/.intelligence-releases/$NEW --check && python3 $P/apply_resolver_patch.py $R/.intelligence-releases/$NEW
grep -c "STAGING ONLY" $R/.intelligence-releases/$NEW/intelligence_app.py     # 0 (the Origin rule is untouched)
mkdir -p /etc/systemd/system/ticking-intelligence.service.d
sed "s#@RELEASE@#$NEW#" /srv/skew/em-ticking-bridge/deploy/ticking-intelligence-em.conf > /etc/systemd/system/ticking-intelligence.service.d/em-release.conf
systemctl daemon-reload && systemctl restart ticking-intelligence
```

Health. Each query is POSTed with `-H 'Origin: https://ticking.dev' -H 'Content-Type: application/json'`:

| Query | Expected |
|---|---|
| "NVDA holders" | `wallet_card` |
| "chart 0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec" | `#/token/robinhood%3A0xd060…` |
| "SOL chart" | `#/token/SOL` |
| "liquidation levels for NVDA" | `wallet_card` with an `economy` card |
| "why did NVDA move" | `wallet_card` with an `economy` card |
| any query with a foreign Origin | 403 |

Rollback:

```bash
rm /etc/systemd/system/ticking-intelligence.service.d/em-release.conf && systemctl daemon-reload && systemctl restart ticking-intelligence
```

### 6. Frontend release (new Vercel deployment, promote, pointer)

```bash
$P/deploy/verify_base.sh            # the frontend base must still be ticking-market-panel-20261019-02
cd $R && cp $P/em-map-release.py qa/em-map-release.py
python3 qa/em-map-release.py build --package $P --name ticking-em-networks-$(date +%Y%m%d)-01
python3 qa/em-map-release.py verify && python3 qa/em-map-release.py package --package $P    # build.json: only the patched src/sdk files + index.html changed
```

**[Mac]** with `PATH=/Users/huhyul/.local/node/bin:$PATH`:

```bash
rsync -a skew:$R/.releases/ticking-em-networks-<date>-01/.vercel/output/ .deploy/ticking-em-networks-<date>-01/.vercel/output/
cd .deploy/ticking-em-networks-<date>-01
printf '{"orgId":"team_wH8b6WWxEoF4cyi6C6aL79hS","projectId":"prj_H9mGBcGrDgqTjycpu1ZFeAYYbkAp"}' > .vercel/project.json
npx vercel@latest deploy --prebuilt --prod --skip-domain --yes --scope woon20020501-pixels-projects     # → deployment URL
npx vercel@latest promote <deployment-url> --yes --scope woon20020501-pixels-projects
```

Health (**[Mac]**, read-only):

```bash
pwvenv/bin/python firstclass/tests/e2e.py https://ticking.dev/ out-prod/
```

Expected:
- NVDA, ARB and WETH charts render, with the NVIDIA logo.
- The Executions tape shows Robinhood Chain swaps.
- The Markets network filter works.
- The SOL regression checks pass.
- No console errors.

Then update the pointer as DEPLOYMENT.md step 5 (same schema; the old object nested under `previous`; `cp current-public.json $K/` first).

Rollback, which is always safe because the old frontend never asks for `networks=all`:

```bash
npx vercel@latest promote <previous url from $K/current-public.json> --yes --scope woon20020501-pixels-projects
cp $K/current-public.json $R/.releases/current-public.json
```

### 7. Watch (first hour)

```bash
journalctl -u ticking-engine-bridge -u ticking-console -u em-ticking-bridge --since -1h | grep -E "read_failed|Traceback" | tail
python3 $P/tests/solana_regression.py --compare $K/solana-baseline.json http://127.0.0.1:19428
top -b -n1 | head -15
```

### Rollback order

Roll back in reverse: frontend (6) → resolver (5) → nginx (4) → wallet bridge (3) → console (2) → engine (1). Each step is independent:
- A new frontend on an old console still shows Solana normally; EVM routes answer 404.
- An old frontend on the new console never receives EVM rows.

## Test results (staging on the 2026-10-19 bases above)

| Suite | Result |
|---|---|
| e2e `firstclass/tests/e2e.py`, full run on the rebased staging (desktop light + dark, phone, errors; EVM; Economy panel, venues vs wallets, readable labels, overlays and fit, liquidation bands, 4 economy intents incl. the causal 'why' headline, market-aware placeholder and chips; Markets; home; Solana regression; map; wallets; Web Vitals) | **151/151**, 0 transient 503s. After that run, zero-debt ladder rows were dropped from the chart bands; the targeted re-run `E2E_ONLY=economy` (light + dark) passed **48/48**. |
| `tests/solana_regression.py` (production :19428 vs staging :19438: SOL token 1h/15m/1d, pool 1m, BTC exchange, markets/<id>/candles, tape shape, catalog) | **9/9**, byte-identical closed windows |
| `tests/test_engine_evm.py` (identity, hot/deep seam, hot-only minute pages, archive-stall fallback, tapes, bounded carry, resolve, commit deps) | **8/8** |
| `tests/test_ui_evm.py` (exact selection, review pinned by network, static identity view, bounds, icon names) | **5/5** |
| `node --test tests/test_networks.mjs tests/test_em_api.mjs tests/test_em_economy.mjs` | **10/10** |
| `apply_*_patch.py --check` against the recorded 2026-10-19 bases | all anchors match (engine 9, console 40, qa 6, resolver 9, frontend 20 files / 89 edits) |

### Web Vitals

Staging was measured from the Mac through an ssh tunnel, so production on Vercel's CDN will be faster. The SOL rows are the same-session baseline.

| Page | Device | LCP (ms) | CLS | Chart interactive (s) |
|---|---|---|---|---|
| NVDA workspace | desktop | 6632 | 0.0003 | 12.56 |
| NVDA workspace | phone | 1740 | 0.0132 | 8.02 |
| SOL workspace (baseline) | desktop | 2608 | 0.0001 | 9.53 |
| SOL workspace (baseline) | phone | 3096 | 0.0005 | 10.22 |
| NVDA token page | desktop | 3496 | 0.0465 | 7.22 |
| NVDA token page | phone | 2516 | 0.0881 | 6.23 |
| SOL token page (baseline) | desktop | 4504 | 0.0583 | 15.30 |
| SOL token page (baseline) | phone | 4872 | 0.0591 | 13.19 |

Measured in run fc-e2e11 on the rebased release (identity-first token page). The host load average was about 10–15 during the run, so the absolute times are noisy; read them against the same-session SOL baseline.

## Known limits

- **em-charts memory (outside this package):** `em-map-charts` (:8895) died once with SIGBUS at 03:42 BST on 2026-10-19. It peaked at 17.3 GB RSS with 1.9 GB swap while the EM data refresh was running, and systemd restarted it within 10 s. The data-refresh job now caps itself (DuckDB 9 GB, unit MemoryMax=12G). em-charts has no MemoryMax. Until its first analytics refresh after a restart (≤10 min), Ticking shows the map snapshot, a "Live analytics" chip only when live, and a disabled "Liquidation levels" toggle with an explanatory title.

- **Pool-level carried charts:** these (`aggregation=pool`, `gap_policy=carry`) cover the 3-day hot suffix, because the shared `chart_history` carry path reads SQL bars. Recorded pool charts and token references (the default view) cover full history.
- **"NVDA chart" alias:** the existing navigation alias still sends "NVDA chart" to the Solana xStock (`NVDAx`). An exact 0x address, or a pick in Markets, opens the Robinhood Chain market.
- **Icons:**
  - 39 Robinhood stock brands use Simple Icons 16.35.0 (CC0) tiles, including NVIDIA, Apple, Google, Tesla, Meta, Coinbase and SpaceX.
  - Microsoft, Amazon and renamed brands keep a Ticking monogram.
  - 53 Arbitrum tokens use exact-address token-list logos (TrustWallet, Uniswap, CoinGecko).
  - All icons go through `refresh-token-icons` with provenance in `manifest.networkIcons`.
- **Live analytics:** the em-charts token-analytics endpoints were not served yet when this was written (`analytics.py` was in progress). The Economy panel, overlays and intents therefore show map-snapshot data, labelled as such:
  - money state from the top holders by class;
  - Morpho Blue borrowers in place of a liquidation ladder;
  - decoded actions over the mapped history in place of a 1h/24h/7d class split;
  - recent decoded actions as events.

  They switch to live data automatically, section by section.
- **Phone layout:** the Economy panel uses the terminal's existing full-height side panel on phones, the same sheet the Wallets and Market-details views use.
- **Explicit network field:** Solana catalog rows now carry `network: "solana"`. This is the only Solana-visible change, and it is asserted by the regression test.
