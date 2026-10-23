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

- `/terminal/dist/_app/track-paint-MMRLVUHN.js`
- `/terminal/dist/_app/track-intent-JIG26E7Z.js`
- `/terminal/dist/_app/track-workspace-W32A4OSN.js`

## Feel and guard

Wallet chips and bridge chips are buttons. A wallet chip adds that wallet's chart in the same incident. A bridge chip or side column opens the destination chain chart (`eth` resolves to `ethereum`). Replay defaults to 900ms and at most 8 steps. `#/tracker` is a one-row wallet form (`data-track-form`) and does not mount Flow.

`terminal/src/track-intent.js` classifies the query before resolve. Track intent sends `mode:"track"`, `listAddresses:false`, and a Perceive / Plan / Act guard that paints hops on the workspace chart. Dump and suspect send `mode:"dump"` and keep dump tags only. A bare token such as PUMP stays on the asset chart. An empty live resolve does not replace a handoff that already has markers. Search responses with `searchGuard` `+track` and `route` `#/console/workspace/wallet:…` are stored and opened as that workspace.

The hash is 8 characters from the first 5 bytes of SHA-256, encoded with the same alphabet as the products `/_app` names.

## Promote

`scripts/promote-track-static.mjs` crawls the live static tree, bundles this repo's track sources, and rewrites the app, workspace, console, and agent hashes. Alias only after a wallet workspace with `?sample=1` shows markers, a hop chip opens a second chart in that incident, a bridge chip opens the chain chart, and `#/tracker` is the wallet form.

Production aliases `ticking.dev`, `skew.deals`, and `www.skew.deals` point at the products static build that paints track markers on the workspace TickChart.

| | |
| --- | --- |
| Deployment | `dpl_AYi1Zr7i4JdA4veARfA1JLtMf2Ss` |
| App entry | `/_app/app-UVYRWMGQ.js` |
| Track shell | `/_app/chunks/track-shell-SPIWJDUZ.js` (`setTrackPaint`, `mountTrackWorkspace`, `mountTrackerPage`, `classifyTrackIntent`) |
| Wallet mount | `/_app/chunks/console-agent-XFNF7DZA.js` |
| Workspace router | `/_app/chunks/workspace-KPIZJF5T.js` |
| Rollback | `dpl_8kWWQ53PMJXzv6aA4m2T5UabAKaZ` (`/_app/app-L5AHZFB4.js`) |

Checked on `ticking.dev` after the alias: a wallet workspace with `?sample=1` paints the sample markers; a hop chip opens a second chart in that same incident; a bridge chip labeled `Open eth chart` opens the ETH chain chart beside it; `#/tracker` is the wallet form (`data-track-form`) and does not mount Flow. A stored handoff plus `POST /api/public/intelligence/resolve` kept the handoff summary when the live body had no markers. Roll back by aliasing `dpl_8kWWQ53PMJXzv6aA4m2T5UabAKaZ`. The alias this deploy replaced was `dpl_3ReWzi9d84dBa2Hrmx4s8Be7M1X2`.
