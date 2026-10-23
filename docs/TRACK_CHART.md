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

- `/terminal/dist/_app/track-paint-TLDU4CLQ.js`
- `/terminal/dist/_app/track-intent-VXMKGBGD.js`
- `/terminal/dist/_app/track-workspace-FA3BO4LE.js`

## Feel and guard

The workspace chart is a trail silhouette on the time axis: buys lift it, sells and dumps pull it down, and every bar stays synthetic with volume 0. It is the shape of the incident, not a market print. Nearby labels take separate lanes and carry a clock from the first marker (`+0m`, `+9m`). Hop bands are a light wash with a ribbon and a duration. Dump and whale tags are larger orange buttons. A dump on this wallet focuses that moment. A whale tag on another wallet adds that wallet's chart in the same case. A wallet chip does the same. A bridge chip or the side column opens the destination chain chart beside it (`eth` resolves to `ethereum`; the column reads “Open ETH beside”). Replay walks at most 8 steps, default 900ms, and the caption names the step (`Trail 4 of 8 · dump`) without printing a full address. Markers already passed stay put, the current one is marked, and later ones fade until the cursor reaches them. `#/tracker` is a one-row wallet form (`data-track-form`) and does not mount Flow.

`terminal/src/track-intent.js` classifies the query before resolve. Wallet activity, a transaction trail, and a market chart are one incident: track and dump call resolve and paint on the workspace chart; `SOL`, `ARB`, and `PUMP` open that workspace chart directly. Dump and suspect stay dump tags. Fast Feed with no wallet stays off the track resolve. A bare `map` or `flow` opens `#/console/workspace/auto`, not a map page and not investigate. Solana, Arbitrum, and Robinhood Chain are the chain hints on a wallet route. An empty live resolve does not replace a handoff that already has markers. Search responses with `searchGuard` `+track` and `route` `#/console/workspace/wallet:…` are stored and opened as that workspace.

A hop graph is the same workspace chart. When resolve returns `graph` (nodes, edges, or a loop) and no `chartMarkers`, the browser maps that loop onto `ticking.chartMarkers.track.v1`: hop chips, a loop band, bridge chips, and a side column. Replay walks the mapped trail. Markers that already arrived stay as they are. An empty graph adds nothing. The chart asks from inside the incident (`data-track-ask`). The question resolves with `stay: true` and `graph: "loop"`, then paints the answer on that same chart. A zero-marker answer leaves the paint in place and reads “This question stays on this chart.” A mint question asked there stays on the mint chart.

Fast Feed is latency paint for Arbitrum and Robinhood. After 2026-10-24 01:00 Asia/Singapore the mint chart polls `GET /api/public/engine/fast-feed` (`interval=1s`, `coverage=tip_hot`) and safe-merges `bars_1s` into the series. A tip wick is clamped to 5% of the body. A `binance_1s` bar on that second is kept, and a later Binance bar replaces a tip. A missed or non-OK pull leaves the series in place. Solana does not call the tip URL. `?sample=1` does not poll. The first accepted tip switches that mint chart to the 1s timeframe. A market route that names Arbitrum or Robinhood keeps `?chain=arbitrum` or `?chain=robinhood` so a short-timeframe products chart can take the same merge. A 1m candle series is left intact.

A mint is the same workspace chart. `minted PEPE`, `new token on robinhood`, `robinhood launchpad`, and a bare Solana mint classify as `mint` and resolve with `track: true`. The route is `#/console/workspace/mint:…` when the query has a mint address, `#/console/workspace/SYMBOL?launch=1` when it names a ticker, and `#/console/workspace/launch?q=…` for the phrase. A Solana mint loads price from `GET /api/public/engine/candles`. Dump and whale tags, hop chips, and side charts paint only when resolve returns markers. A zero-marker resolve does not invent tags, and an Arbitrum or Robinhood mint does not invent candles. `?sample=1` on a mint route paints the mint fixture (`Sample mint on this workspace chart.`). The developers mint form opens that chart instead of waiting on a text preview. Bare `SOL` stays a market route. Bare `RH` stays unclassified. A bare `0x` wallet stays a wallet trail.

The hash is 8 characters from the first 5 bytes of SHA-256, encoded with the same alphabet as the products `/_app` names.

## Promote

`scripts/promote-track-static.mjs` crawls the live static tree, bundles this repo's track sources, and rewrites the app, workspace, console, and agent hashes. Alias only after a wallet workspace with `?sample=1` shows markers, a hop chip opens a second chart in that incident, a bridge chip opens the chain chart, and `#/tracker` is the wallet form.

Production aliases `ticking.dev`, `skew.deals`, and `www.skew.deals` point at the products static build that paints track markers on the workspace TickChart.

| | |
| --- | --- |
| Deployment | `dpl_68mtTVqtdT5EWEPksr1R9JL6MY7S` |
| App entry | `/_app/app-OL2I62PQ.js` |
| Track shell | `/_app/chunks/track-shell-KTQN6OSF.js` (`setTrackPaint`, `mountTrackWorkspace`, `mountMintWorkspace`, `mountTrackerPage`, `classifyTrackIntent`, `data-track-ask`) |
| Wallet mount | `/_app/chunks/console-agent-VLHCXSXE.js` |
| Workspace router | `/_app/chunks/workspace-6Z5UX4JG.js` |
| Console | `/_app/chunks/console-3PCHZ3GL.js` |
| Home | `/_app/chunks/chunk-JGQFL2MX.js` |
| Route | `/_app/chunks/chunk-OJVUMUFG.js` |
| Rollback | `dpl_8kWWQ53PMJXzv6aA4m2T5UabAKaZ` (`/_app/app-L5AHZFB4.js`) |

Checked on `ticking.dev` after the alias: the homepage heading is “Onchain research”. `#/console/workspace/wallet:…?sample=1` paints “Sample paint on this workspace chart.” and shows “Ask this chart”. A question whose resolve returns no markers stays on that hash and reads “This question stays on this chart.” A resolve body that carries a hop graph paints “Hop loop on this chart.” and opens the next hop as “Same case · wallet chart”. A dump tag stays a focus button. `#/console/workspace/mint:…?sample=1` paints “Sample mint on this workspace chart.” on the same ask form. Roll back by aliasing `dpl_8kWWQ53PMJXzv6aA4m2T5UabAKaZ`. The alias this deploy replaced was `dpl_CxGb7iij23HfMzYKYJrZxUt2sFFV`.
