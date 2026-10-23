# Track paint on the workspace chart

Schema: `ticking.chartMarkers.track.v1`.

The browser paints `chartMarkers`, `bands`, and `sideColumns` on the workspace TickChart. Search hands that payload over as `handoff.next.request` with `track: true` and `listAddresses: false`. The same fields come back from `POST /api/public/intelligence/resolve`. Loopback `POST /v1/track` on `:19480` already builds this payload; this tree does not reimplement it.

`#/console/workspace/wallet:0x…` is one incident. A chip click adds that wallet's charts in the same workspace. A dotted bridge chip or side column adds that chain's chart beside it. `muse.replay` / `replayMs` (default 1500) plays a short timeline. `#/tracker` is not a Flow page and not a map: a wallet query redirects onto the workspace route, and an empty visit asks for a wallet.

Preview fixture only: `#/console/workspace/wallet:0x…?sample=1`.

## Hashed files

`node scripts/hash-track-build.mjs` writes `terminal/track-build.json` and:

- `/terminal/dist/_app/track-paint-<hash>.js`
- `/terminal/dist/_app/track-workspace-<hash>.js`

Current build, also recorded in `terminal/track-build.json`:

- `/terminal/dist/_app/track-paint-KTW5KUBS.js`
- `/terminal/dist/_app/track-workspace-GLSXQH6B.js`

The hash is 8 characters from the first 5 bytes of SHA-256, encoded with the same alphabet as the products `/_app` names.

## Promote

Production aliases `ticking.dev`, `skew.deals`, and `www.skew.deals` point at the products static build that paints track markers on the workspace TickChart.

| | |
| --- | --- |
| Deployment | `dpl_3ReWzi9d84dBa2Hrmx4s8Be7M1X2` |
| App entry | `/_app/app-TYCMKHBN.js` |
| Track shell | `/_app/chunks/track-shell-XQ7HHZ26.js` (`setTrackPaint`, `mountTrackWorkspace`, `mountTrackerPage`) |
| Wallet mount | `/_app/chunks/console-agent-4BQUAKQK.js` |
| Rollback | `dpl_8kWWQ53PMJXzv6aA4m2T5UabAKaZ` (`/_app/app-L5AHZFB4.js`) |

Checked on production after the alias: a wallet workspace with `?sample=1` shows buy, sell, bridge, hop, CEX, and unlabeled markers; a hop chip opens a second chart in the same incident; `#/tracker` renders the retired workspace prompt and does not mount Flow chrome. A live resolve for that public wallet returned zero markers, so the sample query is the visual proof. Roll back by aliasing `dpl_8kWWQ53PMJXzv6aA4m2T5UabAKaZ`.
