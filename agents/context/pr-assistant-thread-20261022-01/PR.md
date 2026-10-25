# PR assistant-thread-20261022-01 — per-user assistant thread (frontend)
Status: OPEN, awaiting owner approval. Not built, not published.
Base: public release .releases/ticking-multichain-ui-20261022-23 (dpl_JBpSs9wQm6MSMEJHLVZ8XuD1PCxi, published 15:00 BST).
Backend (already live): ticking-context-layer.service :19445 behind api.ticking.dev /api/public/intelligence/{resolve,thread}.
Overlay (declared source changes only):
- src/assistant-thread.js (new): fetch wrapper for same-origin /api/public/intelligence/*; sends X-Ticking-Thread
  from localStorage (key scoped by accountStorageKey -> per logged-in account), stores the id the API returns.
- src/conversation-state.js: imports it; clearConversation() also DELETEs the server thread;
  new restoreConversation() rebuilds an empty tab from GET /api/public/intelligence/thread.
Markets guard files (market-explorer.js, market-page.js, networks.js, market-snapshot.js): untouched.
Vercel proxy: .vercel/output/config.json routes /api/(.*) -> https://api.ticking.dev/api/$1 (external rewrite; custom headers pass).
Release steps still required (DEPLOYMENT.md / SHARED_RELEASE_GUARD / AGENTS.md):
 1. Recapture current public pointer (unified-tracker-20261022-25..28 candidates were being built 15:40-15:48 BST by another chat).
 2. Overlay these 2 files on that exact base; python3 qa/apply-required-console-layers.py <release>; bundle (qa/build-public.mjs).
 3. qa/check-market-network-filters.py on the candidate (Amsterdam browser gate).
 4. qa/prepare-vercel.py; vercel deploy --prebuilt --prod --skip-domain; recheck pointer/alias; vercel promote (owner's authenticated CLI).
 5. Live test: 2-turn follow-up on ticking.dev; update .releases/current-public.json.
