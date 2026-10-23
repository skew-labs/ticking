import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {existsSync, readFileSync, writeFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {patchAgent, patchExplorerSearch, patchHomeChunk, patchHomeHtml, patchRouteChunk} from '../../scripts/promote-track-static.mjs';

const root = new URL('../..', import.meta.url);
const live = {
  search: '/tmp/research/explorer-search.js',
  lazy: '/tmp/research/explorer-search-lazy.js',
  home: '/tmp/research/chunk-HTODKWZW.js',
  html: '/tmp/research/index.html',
  route: '/tmp/research/chunk-YYIE6Q3F.js',
  agent: '/tmp/research/console-agent.js',
};
for (const file of Object.values(live)) assert.equal(existsSync(file), true, file);

const search = patchExplorerSearch(readFileSync(live.search, 'utf8'));
const lazy = patchExplorerSearch(readFileSync(live.lazy, 'utf8'));
for (const text of [search, lazy]) {
  assert.equal(text.includes('Open this address on every mapped chain'), false);
  assert.equal(text.includes('location.hash="#/investigate/"+e.toLowerCase()'), false);
  assert.equal(text.includes('Paint this wallet on the workspace chart'), true);
  assert.equal(text.includes('#/console/workspace/"+encodeURIComponent("wallet:"+e.toLowerCase())'), true);
  assert.equal(text.includes('encodeURIComponent("mint:"+'), true);
  assert.equal(text.includes('#/console/workspace/launch?q='), true);
  assert.equal(text.includes('route:"#/investigate/'), false);
}
assert.equal(lazy.includes('#/console/workspace/"+encodeURIComponent(e.id)'), true);
assert.equal(search.includes('#/console/workspace/"+encodeURIComponent("wallet:"+s)'), true);

const home = patchHomeChunk(readFileSync(live.home, 'utf8'));
assert.equal(home.includes('Onchain research'), true);
assert.equal(home.includes('Map wallets across chains'), false);
assert.equal(home.includes('Whale Tracker'), false);
assert.equal(home.includes('requestIdleCallback'), true);
assert.equal(home.includes('let i;const arm=()=>{i=new IntersectionObserver'), true);
assert.equal(home.includes('i?.disconnect()'), true);
assert.equal(home.includes('()=>{let i=new IntersectionObserver'), false);
assert.equal(home.includes('rootMargin:"150px"'), false);
assert.equal(home.includes('#/investigate/'), false);
assert.equal(home.includes('Wallet, trail, or a new mint \\xB7 one workspace chart'), true);
assert.equal(home.includes('a new mint open on one workspace chart'), true);
assert.equal(home.includes('Opening one workspace\\u2026'), true);

const html = patchHomeHtml(readFileSync(live.html, 'utf8'));
assert.equal(html.includes('<h1 id="flow-title">Onchain research</h1>'), true);
assert.equal(html.includes('Whale Tracker'), false);
assert.equal(html.includes('Wallet, trail, or a new mint · one workspace chart'), true);
assert.equal(html.includes('placeholder="Wallet, new mint, or market"'), true);
assert.equal(html.includes('a new mint open on one workspace chart'), true);

const route = patchRouteChunk(readFileSync(live.route, 'utf8'));
assert.equal(route.includes('#/investigate/'), false);
assert.equal(route.includes('#/console/workspace/"+encodeURIComponent("wallet:"+l.toLowerCase())'), true);
assert.equal(route.includes('"#/map"'), true);
assert.equal(route.includes('o==="launch"'), true);
assert.equal(route.includes('launch:e.get("launch")'), true);
assert.equal(route.includes('L==="1"&&r.set("launch","1")'), true);

const agent = patchAgent(readFileSync(live.agent, 'utf8'), 'track-shell-TESTHASH.js');
assert.equal(agent.includes('track-shell-TESTHASH.js'), true);
assert.equal(agent.includes('track-shell-SPIWJDUZ.js'), false);
assert.equal(agent.includes('intent.mode==="market"'), true);
assert.equal(agent.includes('intent.mode!=="other"'), false);
assert.equal(agent.includes('<h1>One workspace chart</h1>'), true);
assert.equal(agent.includes('A wallet, its trail, and the market stay on one workspace chart.'), true);
assert.equal(agent.includes('The hop loop paints on that chart.'), true);
assert.equal(agent.includes('A question there updates the same incident.'), true);
assert.equal(agent.includes('intent.mode==="mint"'), true);
assert.equal(agent.includes('mountMintWorkspace'), true);
assert.equal(agent.includes('Opening the mint chart'), true);
assert.equal(agent.includes('let route=intent.route;try{const y=await resolveTrackQuery'), true);
writeFileSync('/tmp/research/agent-patched.mjs', agent);
writeFileSync('/tmp/research/search-patched.mjs', search);
writeFileSync('/tmp/research/lazy-patched.mjs', lazy);
writeFileSync('/tmp/research/home-patched.mjs', home);
writeFileSync('/tmp/research/route-patched.mjs', route);
for (const file of ['agent-patched.mjs', 'search-patched.mjs', 'lazy-patched.mjs', 'home-patched.mjs', 'route-patched.mjs']) {
  const probe = spawnSync(process.execPath, ['--input-type=module', '-e', `await import(${JSON.stringify(pathToFileURL(`/tmp/research/${file}`).href)})`], {encoding: 'utf8'});
  const output = `${probe.stderr || ''}\n${probe.stdout || ''}`;
  assert.equal(output.includes('SyntaxError') || output.includes('Missing catch'), false, `${file}\n${output.slice(0, 500)}`);
}

const promote = readFileSync(new URL('../../scripts/promote-track-static.mjs', import.meta.url), 'utf8');
assert.equal(promote.includes('patchResearchEntry'), true);
assert.equal(promote.includes('market gate'), true);
assert.equal(promote.includes('track-shell-QSAI55JM.js'), true);
assert.equal(promote.includes('console tracker label missing'), true);
assert.equal(promote.includes('chart trail silhouette'), true);
assert.equal(promote.includes('mountMintWorkspace'), true);
assert.equal(promote.includes('Opening the mint chart'), true);
assert.equal(promote.includes('patchMintStudio'), true);
console.log(JSON.stringify({passed: true, root: root.pathname}));
