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

- `/terminal/dist/_app/track-paint-FKRTISRS.js`
- `/terminal/dist/_app/track-workspace-5O47HKEK.js`

The hash is 8 characters from the first 5 bytes of SHA-256, encoded with the same alphabet as the products `/_app` names.

## Promote

Observed products shell on 2026-10-23, before this paint:

| | |
| --- | --- |
| Deployment | `dpl_8kWWQ53PMJXzv6aA4m2T5UabAKaZ` |
| App entry | `/_app/app-L5AHZFB4.js` |
| Workspace mount | `/_app/chunks/console-agent-BQIUIGP4.js` (`mountConsoleAgent`) |
| Chart class | `/_app/chunks/chunk-WHR7ZWX6.js` |

That shell does not call `setTrackPaint`. Promote by rebuilding the products static output with this TickChart (`setTrackPaint`) and the hashed `track-paint` module, then upload a new Vercel deployment of that output. Alias it to production only after a wallet workspace shows markers from a resolve handoff, a chip opens a second chart without leaving the incident, and `#/tracker` no longer mounts Flow chrome. Record the new `dpl_` id next to `terminal/track-build.json`. Roll back by aliasing the deployment above.
