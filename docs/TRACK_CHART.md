# Track paint on the workspace chart

Schema: `ticking.chartMarkers.track.v1`.

The browser paints `chartMarkers`, `bands`, and `sideColumns` on the workspace TickChart. Search hands that payload over as `handoff.next.request` with `track: true` and `listAddresses: false`. The same fields come back from `POST /api/public/intelligence/resolve`. Loopback `POST /v1/track` on `:19480` already builds this payload; this tree does not reimplement it.

`#/console/workspace/wallet:0x…` is one incident. A chip click adds that wallet's charts in the same workspace. A dotted bridge chip or side column adds that chain's chart beside it. `muse.replay` / `replayMs` (default 900, at most 8 steps) plays a short timeline. `#/tracker` is a wallet form, not a Flow page and not a map: a wallet query opens the workspace chart, and an empty visit asks for a wallet.

Preview fixture only: `#/console/workspace/wallet:0x…?sample=1`.

## Hashed files

`node scripts/hash-track-build.mjs` writes `terminal/track-build.json` and:

- `/terminal/dist/_app/track-paint-<hash>.js`
- `/terminal/dist/_app/track-intent-<hash>.js`
- `/terminal/dist/_app/track-workspace-<hash>.js`

Current build, also recorded in `terminal/track-build.json`:

- `/terminal/dist/_app/track-paint-I57ED4YC.js`
- `/terminal/dist/_app/track-intent-4WDFIVLB.js`
- `/terminal/dist/_app/track-workspace-UUWECRKZ.js`

## Feel and guard

The workspace chart is a trail silhouette on the time axis: buys lift it, sells and dumps pull it down, and every bar stays synthetic with volume 0. It is the shape of the incident, not a market print. Nearby labels take separate lanes and carry a clock from the first marker (`+0m`, `+9m`). Hop bands are a light wash with a ribbon and a duration. Dump and whale tags are larger orange buttons. A dump on this wallet focuses that moment. A whale tag on another wallet adds that wallet's chart in the same case. A wallet chip does the same. A bridge chip or the side column opens the destination chain chart beside it (`eth` resolves to `ethereum`; the column reads “Open ETH beside”). Replay walks at most 8 steps, default 900ms, and the caption names the step (`Trail 4 of 8 · dump`) without printing a full address. Markers already passed stay put, the current one is marked, and later ones fade until the cursor reaches them. `#/tracker` is a one-row wallet form (`data-track-form`) and does not mount Flow.

`terminal/src/track-intent.js` classifies the query before resolve. Wallet activity, a transaction trail, and a market chart are one incident: track and dump call resolve and paint on the workspace chart; `SOL`, `ARB`, and `PUMP` open that workspace chart directly. Dump and suspect stay dump tags. Fast Feed with no wallet stays off the track resolve. A bare `map` or `flow` opens `#/console/workspace/auto`, not a map page and not investigate. Solana, Arbitrum, and Robinhood Chain are the chain hints on a wallet route. An empty live resolve does not replace a handoff that already has markers. Search responses with `searchGuard` `+track` and `route` `#/console/workspace/wallet:…` are stored and opened as that workspace.

The hash is 8 characters from the first 5 bytes of SHA-256, encoded with the same alphabet as the products `/_app` names.

## Promote

`scripts/promote-track-static.mjs` crawls the live static tree, bundles this repo's track sources, and rewrites the app, workspace, console, and agent hashes. Alias only after a wallet workspace with `?sample=1` shows markers, a hop chip opens a second chart in that incident, a bridge chip opens the chain chart, and `#/tracker` is the wallet form.

Production aliases `ticking.dev`, `skew.deals`, and `www.skew.deals` point at the products static build that paints track markers on the workspace TickChart.

| | |
| --- | --- |
| Deployment | `dpl_9h2rRE79J9YJJa8sf7KiSyURHHpk` |
| App entry | `/_app/app-6SXSQPWV.js` |
| Track shell | `/_app/chunks/track-shell-LI33TFB4.js` (`setTrackPaint`, `mountTrackWorkspace`, `mountTrackerPage`, `classifyTrackIntent`, sharp dump/whale tags, trail replay) |
| Wallet mount | `/_app/chunks/console-agent-EKD36Y3A.js` |
| Workspace router | `/_app/chunks/workspace-FTE7RKJ7.js` |
| Console | `/_app/chunks/console-LQ3AIWC2.js` |
| Home | `/_app/chunks/chunk-DHNHFGB4.js` |
| Rollback | `dpl_8kWWQ53PMJXzv6aA4m2T5UabAKaZ` (`/_app/app-L5AHZFB4.js`) |

Checked on `ticking.dev` after the alias: the homepage title is “Onchain research”; a wallet search opens `#/console/workspace/wallet:…`; `SOL` opens `#/console/workspace/SOL`; `fast feed` stays on search. A wallet workspace with `?sample=1` paints the trail silhouette, clocks, and sharp `dump · +9m` / `whale · +10m` tags. Replay captions the hop walk (`Trail 1 of 8 · buy` through `Trail 8 of 8 · unlabeled`). A dump click focuses that move on the same chart. A whale tag opens `WETH · 0x51c7…2a7f` as “Same case · wallet chart”. A bridge chip labeled `Open eth chart` opens the ETH chart as “Same case · chain chart”. The side column reads “Open ETH beside”. `#/tracker` is the wallet form (`data-track-form`) and does not mount Flow. The console nav says “Workspace chart”. Roll back by aliasing `dpl_8kWWQ53PMJXzv6aA4m2T5UabAKaZ`. The alias this deploy replaced was `dpl_2wEmx1aRwb9jyFF5Jf3HZAJAjYcQ`.
