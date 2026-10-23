/**
 * Rebuild the products static tree with this repo's track shell.
 *
 * Crawls the current ticking.dev static files, bundles terminal/src track
 * paint into a hashed shell, and points the workspace app at it.
 * Baseline names are the production files this build replaces.
 *
 *   SOURCE_DIR=/workspace STATIC_SEED=/tmp/seed node scripts/promote-track-static.mjs
 *   SOURCE_SHA=<40 hex> node scripts/promote-track-static.mjs
 */
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {existsSync, mkdirSync, readFileSync, readdirSync, unlinkSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

// chart trail silhouette — baseline is the live hop-loop chart deploy.
const BASE = {
  shell: 'track-shell-KTQN6OSF.js',
  agent: 'console-agent-VLHCXSXE.js',
  console: 'console-3PCHZ3GL.js',
  workspace: 'workspace-6Z5UX4JG.js',
  app: 'app-OL2I62PQ.js',
};
const ell = '\\u2026';
const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
const out = 'static';

function contentTag(bytes) {
  const digest = createHash('sha256').update(bytes).digest().subarray(0, 5);
  let n = 0n;
  for (const b of digest) n = (n << 8n) + BigInt(b);
  let s = '';
  for (let i = 0; i < 8; i++) { s = alphabet[Number(n & 31n)] + s; n >>= 5n; }
  return s;
}

function replaceOnce(source, from, to, label) {
  const count = source.split(from).length - 1;
  if (count !== 1) throw new Error(`${label} count ${count}`);
  return source.replace(from, to);
}

function mintSearchGo() {
  return 'if(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(String(e).trim())){const box=document.querySelector("[data-tkx-ta]");if(box)box.hidden=true;location.hash="#/console/workspace/"+encodeURIComponent("mint:"+String(e).trim())+"?chain=solana";return}'
    + 'if(/minted|minting|\\bmints?\\b|new\\s+(?:token|mint|coin|pair)|\\blaunchpad\\b|\\blaunch(?:ed|ing)?\\b|just\\s+listed|민트|신규\\s*(?:토큰|민트)|런치(?:패드)?/i.test(e)){const box=document.querySelector("[data-tkx-ta]");if(box)box.hidden=true;const raw=String(e);const evm=(raw.match(/0x[a-fA-F0-9]{40}/)||[])[0];const sol=evm?"":((raw.match(/[1-9A-HJ-NP-Za-km-z]{32,44}/)||[])[0]||"");const chain=/robinhood|\\brh\\b/i.test(raw)?"robinhood":/arbitrum|\\barb\\b/i.test(raw)?"arbitrum":(/solana|\\bsol\\b/i.test(raw)||sol)?"solana":"";const addr=sol||(evm?evm.toLowerCase():"");if(addr){location.hash="#/console/workspace/"+encodeURIComponent("mint:"+addr)+(chain?"?chain="+encodeURIComponent(chain):"");return}const sym=(raw.match(/\\b(?:mint(?:ed|ing)?|new|launch(?:ed|ing)?|launchpad|listed)\\s+(?:token\\s+|a\\s+|the\\s+)?([A-Za-z][A-Za-z0-9]{1,12})\\b/i)||[])[1];if(sym&&!/^(?:on|the|a|an|token|mint|coin|pair|pad|robinhood|solana|arbitrum|chain|chart)$/i.test(sym)){location.hash="#/console/workspace/"+encodeURIComponent(sym.toUpperCase())+"?launch=1"+(chain?"&chain="+encodeURIComponent(chain):"");return}location.hash="#/console/workspace/launch?q="+encodeURIComponent(raw.trim())+(chain?"&chain="+encodeURIComponent(chain):"");return}';
}

function injectMintSearch(source) {
  if (source.includes('encodeURIComponent("mint:"+')) return source;
  let next = source;
  let applied = 0;
  for (const fn of ['m', 'h']) {
    const from = `if(${fn}(e)){const box=document.querySelector("[data-tkx-ta]")`;
    if (!next.includes(from)) continue;
    next = next.replaceAll(from, mintSearchGo() + from);
    applied += 1;
  }
  if (!applied) throw new Error('search mint injection point missing');
  if (!next.includes('encodeURIComponent("mint:"+')) throw new Error('search mint route missing');
  return next;
}

function researchGo(fn) {
  return `if(${fn}(e)){const box=document.querySelector("[data-tkx-ta]");if(box)box.hidden=true;location.hash="#/console/workspace/"+encodeURIComponent("wallet:"+e.toLowerCase());return}`
    + `if(/^[a-z][a-z0-9]{1,15}:0x[0-9a-f]{40}$/i.test(e)){const box=document.querySelector("[data-tkx-ta]");if(box)box.hidden=true;location.hash="#/console/workspace/"+encodeURIComponent(e);return}`
    + `if(/^(?:sol|solana|arb|arbitrum|pump)(?:\\s+chart)?$/i.test(String(e).trim())){const box=document.querySelector("[data-tkx-ta]");if(box)box.hidden=true;location.hash="#/console/workspace/"+encodeURIComponent(/pump/i.test(e)?"PUMP":/arb/i.test(e)?"ARB":"SOL");return}`
    + `if(/^(?:chart|price|market)\\s+(?:of\\s+)?([A-Za-z][A-Za-z0-9]{1,12})$/i.test(String(e).trim())){const box=document.querySelector("[data-tkx-ta]");if(box)box.hidden=true;const sym=String(e).trim().match(/^(?:chart|price|market)\\s+(?:of\\s+)?([A-Za-z][A-Za-z0-9]{1,12})$/i);location.hash="#/console/workspace/"+encodeURIComponent(sym[1].toUpperCase());return}`
    + `if(/^(?:map|flow|wallet map)$/i.test(String(e).trim())){const box=document.querySelector("[data-tkx-ta]");if(box)box.hidden=true;location.hash="#/console/workspace/auto?q="+encodeURIComponent(String(e).trim());return}`
    + `if(/\\b(?:dumps?|dumped|dumping|suspect(?:ed)?)\\b|의심|덤프|where did the money go|follow (?:the )?(?:money|hop)|wallet trail|wallet activity|transaction trail|tx trail|flow of funds|whale trail|whale wallet|map this wallet|investigate this wallet|on-?chain activity|0x[a-fA-F0-9]{40}/i.test(e)){const box=document.querySelector("[data-tkx-ta]");if(box)box.hidden=true;const w=(String(e).match(/0x[a-fA-F0-9]{40}/)||[])[0];location.hash=w?"#/console/workspace/"+encodeURIComponent("wallet:"+w.toLowerCase()):"#/console/workspace/auto?q="+encodeURIComponent(e);return}`;
}

function applyLegacyAgent(source, shellName) {
  let next = source;
  const imported = next.match(/import\{mountTrackWorkspace\}from"\/_app\/chunks\/(track-shell-[A-Z2-7]{8}\.js)"/);
  if (!imported) throw new Error('legacy agent import missing');
  next = replaceOnce(
    next,
    imported[0],
    `import{mountTrackWorkspace,acceptTrackHandoff,resolveTrackQuery,classifyTrackIntent}from"/_app/chunks/${shellName}"`,
    'legacy agent import',
  );
  const pairs = [
    ['What would you like to track?', 'Track it on the chart'],
    [
      'Muse opens the asset chart, follows wallets and adds recorded activity to the same workspace.',
      'A wallet question paints hops on one workspace chart. Dump and suspect stay dump tags.',
    ],
    [
      `placeholder="PUMP, a wallet address, or a question${ell}"`,
      `placeholder="Wallet, or where did the money go from 0x${ell}"`,
    ],
    [
      'Charts stay in the centre. Wallet activity and your selected range stay with the conversation.',
      'Perceive, plan, and act stay on that chart.',
    ],
  ];
  pairs.forEach(([from, to], index) => {
    next = replaceOnce(next, from, to, `agent copy ${index}`);
  });
  const oldPrefix = `async function t(){if(n)return;let i;try{i=w(l.value)}catch(o){c.textContent=o.message;return}n=!0,s.querySelector("button").disabled=!0,c.textContent="Muse is selecting an asset and reading its chart source${ell}";try{`;
  const newPrefix = `async function t(){if(n)return;const intent=classifyTrackIntent(l.value);n=!0;s.querySelector("button").disabled=!0;try{if(intent.mode!=="other"){c.textContent=intent.mode==="dump"?"Reading dump tags${ell}":"Reading the trail${ell}";const y=await resolveTrackQuery(l.value,{signal:e.signal});if(e.signal.aborted)return;const route=acceptTrackHandoff(y)||intent.route;c.textContent=route?"Opening the workspace chart.":"Paste a 0x wallet to open its chart.";if(route)location.hash=route;return}let i;try{i=w(l.value)}catch(o){c.textContent=o.message;return}c.textContent="Muse is selecting an asset and reading its chart source${ell}";try{`;
  next = replaceOnce(next, oldPrefix, newPrefix, 'agent submit');
  next = replaceOnce(
    next,
    'if(e.signal.aborted)return;let p=y.workspace',
    'if(e.signal.aborted)return;const route=acceptTrackHandoff(y);if(route){location.hash=route;return}let p=y.workspace',
    'agent handoff',
  );
  next = replaceOnce(
    next,
    '}catch(o){e.signal.aborted||(c.textContent=o.message)}finally{n=!1,e.signal.aborted||(s.querySelector("button").disabled=!1)}}return s.addEventListener',
    '}catch(o){e.signal.aborted||(c.textContent=o.message)}}catch(o){e.signal.aborted||(c.textContent=o.message)}finally{n=!1,e.signal.aborted||(s.querySelector("button").disabled=!1)}}return s.addEventListener',
    'agent try',
  );
  return next;
}

export function patchAgent(source, shellName) {
  let next = source.includes('What would you like to track?') ? applyLegacyAgent(source, shellName) : source;
  const shells = [...new Set(next.match(/track-shell-[A-Z2-7]{8}\.js/g) || [])];
  if (shells.length !== 1) throw new Error(`agent shell ref ${shells.join(',') || 'missing'}`);
  if (shells[0] !== shellName) next = next.replaceAll(shells[0], shellName);
  // market gate: a symbol opens the workspace chart; track and dump still resolve.
  if (!next.includes('intent.mode==="market"')) {
    const gate = 'if(intent.mode!=="other"){';
    if (!next.includes(gate)) throw new Error('agent mode gate missing');
    next = next.replaceAll(
      gate,
      'if(intent.mode==="market"&&intent.route){location.hash=intent.route;return}if((intent.mode==="track"||intent.mode==="dump")&&intent.route&&!intent.wallet){location.hash=intent.route;return}if(intent.mode==="track"||intent.mode==="dump"){',
    );
  }
  next = next.replaceAll(
    'A wallet question paints hops on one workspace chart. Dump and suspect stay dump tags.',
    'A wallet, its trail, and the market stay on one workspace chart. Dump and suspect stay dump tags.',
  );
  next = next.replaceAll(
    'A wallet, its trail, and the market stay on one workspace chart. Dump and suspect stay dump tags.',
    'A wallet, its trail, and the market stay on one workspace chart. The hop loop paints on that chart. A question there updates the same incident. Dump and suspect stay dump tags.',
  );
  next = next.replaceAll('<h1>Track it on the chart</h1>', '<h1>One workspace chart</h1>');
  const oldPlaceholder = `placeholder="Wallet, or where did the money go from 0x${ell}"`;
  const newPlaceholder = `placeholder="Wallet, SOL, or where did the money go from 0x${ell}"`;
  if (next.includes(oldPlaceholder)) next = next.replaceAll(oldPlaceholder, newPlaceholder);
  if (!next.includes('<h1>One workspace chart</h1>')) throw new Error('agent heading missing');
  if (!next.includes('intent.mode==="market"')) throw new Error('market gate missing');
  if (next.includes('intent.mode!=="other"')) throw new Error('agent kept the broad mode gate');
  if (!next.includes('mountMintWorkspace')) {
    if (!next.includes('import{mountTrackWorkspace,')) throw new Error('agent track import missing');
    next = next.replace('import{mountTrackWorkspace,', 'import{mountTrackWorkspace,mountMintWorkspace,');
  }
  if (!next.includes('mountMintWorkspace(r,')) {
    const walletIf = 'if(e?.(),n=t,String(t.ident).startsWith("wallet:")';
    if (!next.includes(walletIf)) throw new Error('agent wallet branch missing');
    const mintIf = 'if(e?.(),n=t,String(t.ident).startsWith("mint:")||t.ident==="launch"||new URLSearchParams(String(c).split("?")[1]||"").get("launch")==="1"){const params=new URLSearchParams(String(c).split("?")[1]||"");const mint=String(t.ident).startsWith("mint:")?t.ident.slice(5):params.get("mint")||"";e=mountMintWorkspace(r,{ident:t.ident,mint,chain:t.chain,query:params.get("q")||"",sample:params.get("sample")==="1",symbol:String(t.ident).startsWith("mint:")||t.ident==="launch"?"":t.ident});document.title=(mint?mint.slice(0,4)+"…":t.ident)+" · Mint chart — ticking";return}if(String(t.ident).startsWith("wallet:")';
    next = next.replace(walletIf, mintIf);
  }
  const marketGate = 'if(intent.mode==="market"&&intent.route){location.hash=intent.route;return}';
  const mintSubmit = `if(intent.mode==="mint"){c.textContent="Opening the mint chart${ell}";let route=intent.route;try{const y=await resolveTrackQuery(l.value,{signal:e.signal});if(e.signal.aborted)return;route=acceptTrackHandoff(y)||intent.route}catch(o){if(e.signal.aborted)return}if(route)location.hash=route;return}${marketGate}`;
  if (!next.includes('let route=intent.route;try{const y=await resolveTrackQuery')) {
    if (next.includes('intent.mode==="mint"')) throw new Error('mint submit is missing the chart fallback');
    if (!next.includes(marketGate)) throw new Error('agent market gate missing for mint');
    next = next.replace(marketGate, mintSubmit);
  }
  if (!next.includes('mountMintWorkspace') || !next.includes('intent.mode==="mint"') || !next.includes('Opening the mint chart')) {
    throw new Error('agent mint chart missing');
  }
  return next;
}

export function patchExplorerSearch(source) {
  const walletReady = source.includes('Paint this wallet on the workspace chart') && source.includes('"#/console/workspace/"+encodeURIComponent("wallet:"+e.toLowerCase())');
  if (walletReady) return injectMintSearch(source);
  let next = source;
  const pairs = [
    [
      'subtitle:"Open this address on every mapped chain",route:"#/investigate/"+e.toLowerCase()',
      'subtitle:"Paint this wallet on the workspace chart",route:"#/console/workspace/"+encodeURIComponent("wallet:"+e.toLowerCase())',
    ],
    [
      '{...e,route:"#/investigate/"+s+"?"+t.toString()}',
      '{...e,route:"#/console/workspace/"+encodeURIComponent("wallet:"+s)+"?"+t.toString()}',
    ],
    [
      'e.type==="wallet"&&h(t)?{...e,route:"#/investigate/"+t}',
      'e.type==="wallet"&&h(t)?{...e,route:"#/console/workspace/"+encodeURIComponent("wallet:"+t)}',
    ],
    [
      'route:"#/investigate/"+String(e.id).toLowerCase()',
      'route:"#/console/workspace/"+encodeURIComponent(String(e.id).toLowerCase())',
    ],
    [
      'route:"#/investigate/"+encodeURIComponent(e.id)',
      'route:"#/console/workspace/"+encodeURIComponent(e.id)',
    ],
    [
      'route:"#/investigate/"+e.chain+":"+t',
      'route:"#/console/workspace/"+encodeURIComponent(e.chain+":"+t)',
    ],
  ];
  let applied = 0;
  for (const [from, to] of pairs) {
    const count = next.split(from).length - 1;
    if (!count) continue;
    next = next.replaceAll(from, to);
    applied += count;
  }
  for (const fn of ['m', 'h']) {
    const from = `if(${fn}(e)||/^[a-z][a-z0-9]{1,15}:0x[0-9a-f]{40}$/i.test(e)){location.hash="#/investigate/"+e.toLowerCase();return}`;
    if (!next.includes(from)) continue;
    next = next.replaceAll(from, researchGo(fn));
    applied += 1;
  }
  if (!applied) throw new Error('explorer search patch matched nothing');
  if (next.includes('location.hash="#/investigate/"+e.toLowerCase()')) throw new Error('search still opens investigate for a wallet');
  if (next.includes('Open this address on every mapped chain')) throw new Error('search subtitle still says mapped chain');
  if (!next.includes('Paint this wallet on the workspace chart')) throw new Error('search wallet route missing');
  return injectMintSearch(next);
}

export function patchMintStudio(source) {
  let next = source;
  const submitFrom = 'a.addEventListener("submit",async l=>{l.preventDefault();let w=d.value.trim();if(d.value=w,!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(w)){R("Enter the exact Solana token mint address."),d.focus();return}';
  const submitTo = 'a.addEventListener("submit",async l=>{l.preventDefault();let w=d.value.trim();d.value=w;const evm=/^0x[a-fA-F0-9]{40}$/.test(w),sol=/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(w);if(!evm&&!sol){R("Enter a Solana mint or a 0x token address."),d.focus();return}location.hash="#/console/workspace/"+encodeURIComponent("mint:"+(evm?w.toLowerCase():w))+(sol?"?chain=solana":"");return;';
  if (!next.includes('encodeURIComponent("mint:"+(evm?w.toLowerCase():w))')) {
    if (!next.includes(submitFrom)) throw new Error('mint studio submit missing');
    next = next.replace(submitFrom, submitTo);
  }
  next = next.replaceAll('<h3>From mint to market.</h3>', '<h3>The mint opens on the chart.</h3>');
  next = next.replaceAll(
    '<p>Paste a Solana token address. See the actual retained data, then bring the same chart into your product.</p>',
    '<p>A new mint opens on the workspace chart. Solana, Arbitrum, and Robinhood Chain come first.</p>',
  );
  if (!next.includes('The mint opens on the chart.')) throw new Error('mint studio heading missing');
  if (!next.includes('encodeURIComponent("mint:"+(evm?w.toLowerCase():w))')) throw new Error('mint studio route missing');
  if (!next.includes('Make this chart yours')) throw new Error('mint studio collection link missing');
  return next;
}

function patchResearchCopy(source) {
  const pairs = [
    [
      '<h1 id="flow-title">Onchain Explorer</h1><p>Map wallets across chains. Track token ownership, protocols, and transactions over time.</p>',
      '<h1 id="flow-title">Onchain research</h1><p>Wallet activity, transaction trails, and a new mint open on one workspace chart.</p>',
    ],
    [
      '<h1 id="flow-title">Onchain research</h1><p>Wallet activity, transaction trails, and market charts in one workspace.</p>',
      '<h1 id="flow-title">Onchain research</h1><p>Wallet activity, transaction trails, and a new mint open on one workspace chart.</p>',
    ],
    ['Nine-chain Whale Tracker', 'Workspace chart'],
    ['<b>Whale Tracker</b>', '<b>Workspace chart</b>'],
    ['"Whale Tracker"', '"Workspace chart"'],
    ['>Whale Tracker<', '>Workspace chart<'],
    ['Follow large purchases and transfers', 'Paint a wallet trail on one chart'],
    ['placeholder="Search by wallet address, token, protocol, or transaction"', 'placeholder="Wallet, trail, or market"'],
    ['placeholder="Search wallet, token, protocol or tx"', 'placeholder="Wallet, new mint, or market"'],
    ['placeholder="Wallet, trail, or market"', 'placeholder="Wallet, new mint, or market"'],
    ['aria-label="Search wallets, tokens, protocols and transactions"', 'aria-label="Search a wallet, trail, or market"'],
  ];
  let next = source;
  for (const [from, to] of pairs) {
    if (next.includes(from)) next = next.replaceAll(from, to);
  }
  if (!next.includes('Onchain research')) throw new Error('home hero missing');
  if (next.includes('Map wallets across chains')) throw new Error('home hero still maps wallets');
  if (next.includes('Whale Tracker')) throw new Error('home still says Whale Tracker');
  return next;
}

export function patchHomeChunk(source) {
  let next = source.includes('a new mint open on one workspace chart') ? source : patchResearchCopy(source);
  const helpFrom = 'Multichain search \\xB7 Token history \\xB7 Ask Ticking';
  const helpMid = 'Wallet, trail, or market \\xB7 one workspace chart';
  const helpTo = 'Wallet, trail, or a new mint \\xB7 one workspace chart';
  if (next.includes(helpFrom)) next = next.replaceAll(helpFrom, helpTo);
  else if (next.includes(helpMid)) next = next.replaceAll(helpMid, helpTo);
  else if (!next.includes(helpTo)) throw new Error('home search help missing');
  const statusFrom = 'Finding matching wallets, tokens and protocols\\u2026';
  const statusTo = 'Opening one workspace\\u2026';
  if (next.includes(statusFrom)) next = next.replaceAll(statusFrom, statusTo);
  else if (!next.includes(statusTo)) throw new Error('home search status missing');
  const observerFrom = 'let i=new IntersectionObserver(d=>{d.some(h=>h.isIntersecting)&&e()},{rootMargin:"150px"});i.observe(t.querySelector(".flow-market-section"));let l=new URLSearchParams(location.hash.split("?")[1]||"");return l.get("view")==="map"&&/^0x[a-fA-F0-9]{40}$/.test(l.get("address")||"")&&(location.hash="#/investigate/"+l.get("address"))';
  const observerTo = 'let i;const arm=()=>{i=new IntersectionObserver(d=>{d.some(h=>h.isIntersecting)&&e()},{rootMargin:"0px"});const section=t.querySelector(".flow-market-section");section&&i.observe(section)};window.requestIdleCallback?requestIdleCallback(arm,{timeout:1500}):setTimeout(arm,800);let l=new URLSearchParams(location.hash.split("?")[1]||"");return l.get("view")==="map"&&/^0x[a-fA-F0-9]{40}$/.test(l.get("address")||"")&&(location.hash="#/console/workspace/"+encodeURIComponent("wallet:"+String(l.get("address")).toLowerCase()))';
  if (next.includes(observerFrom)) next = next.replaceAll(observerFrom, observerTo);
  else if (!next.includes('requestIdleCallback') || next.includes('location.hash="#/investigate/"+l.get("address")')) throw new Error('home market observer missing');
  if (next.includes('const arm=()=>{let i=new IntersectionObserver')) {
    next = next.replaceAll('const arm=()=>{let i=new IntersectionObserver', 'let i;const arm=()=>{i=new IntersectionObserver');
  }
  if (next.includes('clearTimeout(u),i.disconnect()')) next = next.replaceAll('clearTimeout(u),i.disconnect()', 'clearTimeout(u),i?.disconnect()');
  if (!next.includes('let i;const arm=()=>{i=new IntersectionObserver') || !next.includes('i?.disconnect()')) {
    throw new Error('home observer cleanup lost its handle');
  }
  return next;
}

export function patchHomeHtml(source) {
  let next = source.includes('a new mint open on one workspace chart') ? source : patchResearchCopy(source);
  const helpFrom = 'Multichain search · Token history · Ask Ticking';
  const helpMid = 'Wallet, trail, or market · one workspace chart';
  const helpTo = 'Wallet, trail, or a new mint · one workspace chart';
  if (next.includes(helpFrom)) next = next.replaceAll(helpFrom, helpTo);
  else if (next.includes(helpMid)) next = next.replaceAll(helpMid, helpTo);
  else if (!next.includes(helpTo)) throw new Error('home html search help missing');
  return next;
}

export function patchRouteChunk(source) {
  let next = source;
  const from = 'return l&&/^0x[a-fA-F0-9]{40}$/.test(l)?"#/investigate/"+l+(e?"?chain="+e:""):"#/map"+';
  const to = 'return l&&/^0x[a-fA-F0-9]{40}$/.test(l)?"#/console/workspace/"+encodeURIComponent("wallet:"+l.toLowerCase())+(e?"?chain="+encodeURIComponent(e):""):"#/map"+';
  if (!next.includes('#/console/workspace/"+encodeURIComponent("wallet:"+l.toLowerCase())')) {
    if (!next.includes(from)) throw new Error('route chunk wallet branch missing');
    next = next.replaceAll(from, to);
  }
  if (!next.includes('o==="launch"')) {
    const qFrom = 'o==="auto"&&f&&r.set("q",String(f).slice(0,400))';
    if (!next.includes(qFrom)) throw new Error('route q builder missing');
    next = next.replace(qFrom, '(o==="auto"||o==="launch")&&f&&r.set("q",String(f).slice(0,400)),L==="1"&&r.set("launch","1")');
  }
  if (!next.includes('launch:L=null')) {
    const args = 'q:f=null,chain:i=null}={})';
    if (!next.includes(args)) throw new Error('route builder args missing');
    next = next.replace(args, 'q:f=null,chain:i=null,launch:L=null}={})');
  }
  if (!next.includes('launch:e.get("launch")')) {
    const call = 'q:e.get("q"),chain:e.get("chain")})';
    if (!next.includes(call)) throw new Error('route parse args missing');
    next = next.replace(call, 'q:e.get("q"),chain:e.get("chain"),launch:e.get("launch")})');
  }
  if (!next.includes('o==="launch"') || !next.includes('launch:e.get("launch")')) throw new Error('route dropped the mint launch query');
  return next;
}

async function mirror() {
  if (process.env.STATIC_SEED) {
    const {cpSync} = await import('node:fs');
    cpSync(process.env.STATIC_SEED, out, {recursive: true});
    return {mirrored: 0, failed: 0};
  }
  const origin = 'https://ticking.dev';
  const seen = new Set();
  const queue = ['/'];
  const ref = /(?:(?:src|href)=["']([^"']+)["']|url\(([^)]+)\)|["'](\/(?:_app|assets|sdk|fonts|icons|images)[^"'\s)]+)["'])/gi;
  const bare = /["'](\/(?:_app|assets|sdk)\/[^"'\s]+)["']/g;
  const moduleFrom = /from\s*["']([^"']+)["']/g;
  const moduleBare = /import\s*["']([^"']+)["']/g;
  const styleName = /(?:\$e|loadStyle)\(\s*["']([^"']+\.css)["']\s*\)/g;
  function queuePath(spec, fromPath) {
    if (!spec || /[\s${}]/.test(spec) || spec.startsWith('data:') || spec.startsWith('#')) return;
    let next = spec.split('#')[0].split('?')[0];
    if (next.startsWith('./') || next.startsWith('../')) {
      const base = fromPath.endsWith('/') ? fromPath : fromPath.replace(/[^/]*$/, '');
      next = new URL(next, `https://ticking.dev${base.startsWith('/') ? base : `/${base}`}`).pathname;
    }
    if (!next.startsWith('/') || next.includes('//')) return;
    if (!seen.has(next)) queue.push(next);
  }
  let saved = 0;
  let failed = 0;
  while (queue.length) {
    const path = queue.shift().split('#')[0].split('?')[0];
    if (!path.startsWith('/') || seen.has(path)) continue;
    seen.add(path);
    let response;
    let lastError = '';
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        response = await fetch(origin + path, {headers: {'user-agent': 'ticking-promote'}});
        if (response.ok || response.status === 404) break;
        lastError = String(response.status);
      } catch (error) {
        lastError = error.message;
        response = null;
      }
    }
    if (!response || !response.ok) {
      failed += 1;
      if (failed < 30) console.error('fetch', path, lastError || response?.status);
      continue;
    }
    const data = Buffer.from(await response.arrayBuffer());
    const type = response.headers.get('content-type') || '';
    if (type.includes('text/html') && path !== '/' && path !== '/index.html' && data.length > 80000 && data.subarray(0, 200).toString('latin1').toLowerCase().includes('<!doctype')) {
      failed += 1;
      continue;
    }
    let rel = path === '/' ? 'index.html' : path.slice(1);
    if (rel.endsWith('/')) rel += 'index.html';
    mkdirSync(dirname(join(out, rel)), {recursive: true});
    writeFileSync(join(out, rel), data);
    saved += 1;
    if (saved % 100 === 0) console.log('mirrored', saved);
    const textLike = /\.(js|mjs|css|html|json|svg|txt|webmanifest)$/.test(path) || path === '/' || /javascript|text\/|json/.test(type);
    if (!textLike) continue;
    const text = data.toString('utf8');
    const found = new Set();
    for (const match of text.matchAll(ref)) {
      const raw = (match[1] || match[2] || match[3] || '').replace(/["']/g, '');
      if (raw.startsWith('/')) found.add(raw.split('?')[0].split('#')[0]);
    }
    for (const match of text.matchAll(bare)) found.add(match[1].split('?')[0]);
    for (const match of text.matchAll(moduleFrom)) found.add(match[1]);
    for (const match of text.matchAll(moduleBare)) found.add(match[1]);
    for (const match of text.matchAll(styleName)) {
      const name = match[1];
      found.add(name.startsWith('/') ? name : `/${name}`);
    }
    for (const next of found) queuePath(next, path);
  }
  return {mirrored: saved, failed};
}

function sourceDir() {
  if (process.env.SOURCE_DIR) return resolve(process.env.SOURCE_DIR);
  const sha = process.env.SOURCE_SHA || '';
  if (!/^[0-9a-f]{40}$/.test(sha)) throw new Error('SOURCE_SHA must be 40 hex characters');
  const dest = 'source-repo';
  mkdirSync(dest, {recursive: true});
  const archive = 'source.tar.gz';
  const downloaded = spawnSync('curl', ['-fsSL', '-o', archive, `https://codeload.github.com/skew-labs/ticking/tar.gz/${sha}`], {stdio: 'inherit'});
  if (downloaded.status !== 0) throw new Error('source archive download failed');
  const extracted = spawnSync('tar', ['-xzf', archive, '-C', dest], {stdio: 'inherit'});
  if (extracted.status !== 0) throw new Error('source archive extract failed');
  const top = readdirSync(dest).find(name => name.startsWith('ticking-'));
  if (!top) throw new Error('source archive layout missing');
  return resolve(dest, top);
}

function writeEntry(dir) {
  const src = join(dir, 'terminal/src');
  const entry = `import {TickChart as LiveTickChart} from '/_app/chunks/chart-QO7JAFT4.js';
import {TrackPaintLayer, parseTrackPaint} from '${src}/track-paint.js';
export {mountTrackWorkspace, mountMintWorkspace, mountTrackerPage, parseWorkspaceHash, acceptTrackHandoff, resolveTrackQuery} from '${src}/track-workspace.js';
export {classifyTrackIntent} from '${src}/track-intent.js';

LiveTickChart.prototype.setTrackPaint=function(paint,hooks){
  if(!this.trackLayer)this.trackLayer=new TrackPaintLayer(this);
  if(hooks)this.trackLayer.setHooks(hooks);
  const n=this.series?.length||this.options?.series?.length||0;
  if(n>8){this.count=Math.min(480,n+8);this.offset=0;}
  this.trackLayer.setPaint(parseTrackPaint(paint));
};
const draw=LiveTickChart.prototype.draw;
if(!draw.__trackPaint){
  LiveTickChart.prototype.draw=function(){
    const out=draw.apply(this,arguments);
    this.trackLayer?.sync();
    return out;
  };
  LiveTickChart.prototype.draw.__trackPaint=true;
  const destroy=LiveTickChart.prototype.destroy;
  LiveTickChart.prototype.destroy=function(){
    this.trackLayer?.destroy();
    this.trackLayer=null;
    return destroy.apply(this,arguments);
  };
}
`;
  writeFileSync('track-shell-entry.js', entry);
}

function writeHashed(folder, prefix, bytes) {
  const name = `${prefix}-${contentTag(bytes)}.js`;
  const rel = join(folder, name);
  mkdirSync(dirname(join(out, rel)), {recursive: true});
  writeFileSync(join(out, rel), bytes);
  return name;
}

function walkTextFiles(root) {
  const files = [];
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    for (const ent of readdirSync(dir, {withFileTypes: true})) {
      const path = join(dir, ent.name);
      if (ent.isDirectory()) stack.push(path);
      else if (/\.(js|mjs|html|css|json|svg|webmanifest|txt)$/.test(ent.name)) files.push(path);
    }
  }
  return files;
}

function replaceToken(from, to) {
  const touched = [];
  for (const file of walkTextFiles(out)) {
    const text = readFileSync(file, 'utf8');
    if (!text.includes(from)) continue;
    writeFileSync(file, text.replaceAll(from, to));
    touched.push(file);
  }
  return touched;
}

function owns(file) {
  const base = file.slice(file.lastIndexOf('/') + 1);
  return /^(?:track-shell|console-agent|workspace|app|explorer-search)-[A-Z2-7]{8}\.js$/.test(base)
    || /^console-[A-Z2-7]{8}\.js$/.test(base);
}

function sealDirty(start) {
  const owned = new Set(start);
  let pending = new Set(start);
  for (let pass = 0; pass < 8 && pending.size; pass++) {
    const batch = [...pending];
    pending = new Set();
    for (const file of batch) {
      if (!existsSync(file) || !file.endsWith('.js')) continue;
      const base = file.slice(file.lastIndexOf('/') + 1);
      const match = /^(.*)-[A-Z2-7]{8}\.js$/.exec(base);
      if (!match) continue;
      const bytes = readFileSync(file);
      const next = `${match[1]}-${contentTag(bytes)}.js`;
      if (next === base) continue;
      const dest = join(file.slice(0, file.lastIndexOf('/')), next);
      writeFileSync(dest, bytes);
      owned.add(dest);
      const touched = replaceToken(base, next);
      for (const item of touched) {
        if (item === file || !item.endsWith('.js')) continue;
        if (owned.has(item) || owns(item)) pending.add(item);
      }
    }
  }
  const unsettled = [...pending].filter(file => {
    if (!existsSync(file) || !file.endsWith('.js')) return false;
    const base = file.slice(file.lastIndexOf('/') + 1);
    const match = /^(.*)-[A-Z2-7]{8}\.js$/.exec(base);
    if (!match) return false;
    return `${match[1]}-${contentTag(readFileSync(file))}.js` !== base;
  });
  if (unsettled.length) throw new Error(`hash seal did not settle: ${unsettled.slice(0, 8).join(', ')}`);
}

function patchResearchEntry() {
  const chunk = name => join(out, '_app/chunks', name);
  const searches = readdirSync(join(out, '_app/chunks')).filter(name => name.startsWith('explorer-search-') && name.endsWith('.js'));
  const dirty = [];
  let searchReady = 0;
  for (const name of searches) {
    const path = chunk(name);
    const source = readFileSync(path, 'utf8');
    const patched = patchExplorerSearch(source);
    if (patched !== source) {
      writeFileSync(path, patched);
      dirty.push(path);
    }
    if (patched.includes('Paint this wallet on the workspace chart') && patched.includes('encodeURIComponent("mint:"+')) searchReady += 1;
  }
  if (!searchReady) throw new Error('no search chunk patched');
  const chunkNames = readdirSync(join(out, '_app/chunks')).filter(name => name.endsWith('.js'));
  const homeName = chunkNames.filter(name => {
    const text = readFileSync(chunk(name), 'utf8');
    return text.includes('id="flow-title"') && (text.includes('Onchain Explorer') || text.includes('Onchain research'));
  });
  const routeName = chunkNames.filter(name => {
    const text = readFileSync(chunk(name), 'utf8');
    return text.includes('function k(o)') && (text.includes('#/investigate/"+l') || text.includes('wallet:"+l.toLowerCase()'));
  });
  if (homeName.length !== 1 || routeName.length !== 1) throw new Error(`home/route chunks ${homeName.length}/${routeName.length}`);
  for (const [name, patch] of [[homeName[0], patchHomeChunk], [routeName[0], patchRouteChunk]]) {
    const path = chunk(name);
    writeFileSync(path, patch(readFileSync(path, 'utf8')));
    dirty.push(path);
  }
  const indexPath = join(out, 'index.html');
  writeFileSync(indexPath, patchHomeHtml(readFileSync(indexPath, 'utf8')));
  sealDirty(dirty);
  const index = readFileSync(indexPath, 'utf8');
  if (!index.includes('Onchain research') || index.includes('Map wallets across chains') || index.includes('Whale Tracker')) {
    throw new Error('index research copy missing');
  }
  const app = (index.match(/\/_app\/(app-[A-Z2-7]{8}\.js)/) || [])[1];
  if (!app) throw new Error('index app hash missing');
  const chunks = readdirSync(join(out, '_app/chunks'));
  const home = chunks.find(name => index.includes(name) && readFileSync(chunk(name), 'utf8').includes('id="flow-title">Onchain research'));
  if (!home) throw new Error('home chunk missing after seal');
  const homeText = readFileSync(chunk(home), 'utf8');
  if (!homeText.includes('requestIdleCallback') || homeText.includes('rootMargin:"150px"') || homeText.includes('#/investigate/') || !homeText.includes('i?.disconnect()')) {
    throw new Error('home chunk still eager or still investigates');
  }
  const search = chunks.filter(name => name.startsWith('explorer-search-'));
  for (const name of search) {
    const text = readFileSync(chunk(name), 'utf8');
    if (text.includes('location.hash="#/investigate/"+e.toLowerCase()') || text.includes('Open this address on every mapped chain')) {
      throw new Error(`search chunk ${name} still investigates`);
    }
    if (text.includes('Paint this wallet on the workspace chart') && !text.includes('encodeURIComponent("mint:"+')) {
      throw new Error(`search chunk ${name} missing mint chart`);
    }
  }
  const routeHits = chunks.filter(name => readFileSync(chunk(name), 'utf8').includes('wallet:"+l.toLowerCase()'));
  const route = routeHits.find(hit => chunks.some(name => name !== hit && readFileSync(chunk(name), 'utf8').includes(hit)));
  if (!route) throw new Error('route chunk wallet branch missing after seal');
  if (!readFileSync(chunk(route), 'utf8').includes('o==="launch"')) throw new Error('route chunk dropped the mint launch query');
  if (!index.includes(home) || !readFileSync(join(out, '_app', app), 'utf8').includes(home)) {
    throw new Error('home chunk is not on the app graph');
  }
  return {home, route, search, app};
}

async function main() {
const esbuild = await import('esbuild');
const report = await mirror();
const dir = sourceDir();
writeEntry(dir);
await esbuild.build({
  entryPoints: ['track-shell-entry.js'],
  bundle: true,
  format: 'esm',
  outfile: 'track-shell.js',
  external: ['/_app/chunks/chart-QO7JAFT4.js'],
  legalComments: 'none',
  logLevel: 'silent',
});
const shellBytes = readFileSync('track-shell.js');
const shellName = writeHashed('_app/chunks', 'track-shell', shellBytes);
const agentSource = readFileSync(join(out, '_app/chunks', BASE.agent), 'utf8');
const agent = patchAgent(agentSource, shellName);
writeFileSync('agent-syntax.mjs', agent);
const syntax = spawnSync(process.execPath, ['--input-type=module', '-e', `await import(${JSON.stringify(pathToFileURL(resolve('agent-syntax.mjs')).href)})`], {encoding: 'utf8'});
const syntaxErr = `${syntax.stderr || ''}\n${syntax.stdout || ''}`;
if (syntaxErr.includes('SyntaxError') || syntaxErr.includes('Missing catch')) throw new Error(`agent syntax\n${syntaxErr.slice(0, 600)}`);
const agentName = writeHashed('_app/chunks', 'console-agent', Buffer.from(agent));
let consoleJs = readFileSync(join(out, '_app/chunks', BASE.console), 'utf8');
if (consoleJs.includes('["tracker","Whale Tracker","feed"]')) {
  consoleJs = replaceOnce(consoleJs, '["tracker","Whale Tracker","feed"]', '["tracker","Workspace chart","chart"]', 'console tracker label');
} else if (!consoleJs.includes('["tracker","Workspace chart","chart"]')) {
  throw new Error('console tracker label missing');
}
consoleJs = replaceOnce(consoleJs, BASE.agent, agentName, 'console agent ref');
const consoleName = writeHashed('_app/chunks', 'console', Buffer.from(consoleJs));
let workspace = readFileSync(join(out, '_app/chunks', BASE.workspace), 'utf8');
if (!workspace.includes('mountTrackerPage') || workspace.includes('whale-tracker-3BQEZZ6J') || !workspace.includes('sample=1')) {
  throw new Error('workspace tracker gate missing');
}
workspace = patchMintStudio(workspace);
workspace = replaceOnce(workspace, BASE.shell, shellName, 'workspace shell ref');
workspace = replaceOnce(workspace, BASE.console, consoleName, 'workspace console ref');
const workspaceName = writeHashed('_app/chunks', 'workspace', Buffer.from(workspace));
let app = readFileSync(join(out, '_app', BASE.app), 'utf8');
app = replaceOnce(app, BASE.workspace, workspaceName, 'app workspace ref');
const appName = writeHashed('_app', 'app', Buffer.from(app));
const shellText = shellBytes.toString('utf8');
for (const needle of ['setTrackPaint', 'mountTrackWorkspace', 'mountMintWorkspace', 'classifyTrackIntent', 'data-track-form', 'data-track-ask', 'dumpTagsOnly', 'columnForBridge', 'Math.min(480', 'Fast Feed is latency paint only', 'Map the hop loop onto that chart', 'data-track-focus', 'Replay trail', 'Waiting for the first prints on this mint.', 'This question stays on this chart.', 'Hop loop on this chart.']) {
  if (!shellText.includes(needle)) throw new Error(`shell missing ${needle}`);
}
if (shellText.includes('whale-tracker')) throw new Error('shell still names whale-tracker');
const agentText = readFileSync(join(out, '_app/chunks', agentName), 'utf8');
for (const needle of ['resolveTrackQuery', 'classifyTrackIntent', shellName, 'One workspace chart', 'Reading the trail', 'wallet:', 'intent.mode==="market"', 'intent.mode==="mint"', 'Opening the mint chart', 'mountMintWorkspace']) {
  if (!agentText.includes(needle)) throw new Error(`agent missing ${needle}`);
}
if (agentText.includes('What would you like to track?')) throw new Error('agent kept the old prompt');
const workspaceText = readFileSync(join(out, '_app/chunks', workspaceName), 'utf8');
if (!workspaceText.includes(shellName) || !workspaceText.includes(consoleName) || !workspaceText.includes('mountTrackerPage') || workspaceText.includes('whale-tracker') || !workspaceText.includes('The mint opens on the chart.') || !workspaceText.includes('encodeURIComponent("mint:"+(evm?w.toLowerCase():w))')) {
  throw new Error('workspace wiring missing');
}
const appText = readFileSync(join(out, '_app', appName), 'utf8');
if (!appText.includes(workspaceName)) throw new Error('app wiring missing');
let index = readFileSync(join(out, 'index.html'), 'utf8');
index = replaceOnce(index, `/_app/${BASE.app}`, `/_app/${appName}`, 'index app hash');
writeFileSync(join(out, 'index.html'), index);
for (const stale of [BASE.shell, BASE.agent, BASE.console, BASE.workspace]) {
  const stalePath = join(out, '_app/chunks', stale);
  if (existsSync(stalePath)) unlinkSync(stalePath);
}
const staleApp = join(out, '_app', BASE.app);
if (existsSync(staleApp)) unlinkSync(staleApp);
const research = patchResearchEntry();
const chunkText = name => readFileSync(join(out, '_app/chunks', name), 'utf8');
const liveAppText = readFileSync(join(out, '_app', research.app), 'utf8');
const liveWorkspace = (liveAppText.match(/workspace-[A-Z2-7]{8}\.js/) || [])[0];
if (!liveWorkspace || !chunkText(liveWorkspace).includes('mountTrackerPage')) throw new Error('app lost the workspace');
const liveWorkspaceText = chunkText(liveWorkspace);
const liveShell = (liveWorkspaceText.match(/track-shell-[A-Z2-7]{8}\.js/) || [])[0];
const liveConsole = (liveWorkspaceText.match(/console-[A-Z2-7]{8}\.js/) || [])[0];
if (!liveShell || !chunkText(liveShell).includes('setTrackPaint')) throw new Error('workspace lost the shell');
if (!liveConsole || !/^console-[A-Z2-7]{8}\.js$/.test(liveConsole)) throw new Error('workspace lost the console');
const liveAgent = (chunkText(liveConsole).match(/console-agent-[A-Z2-7]{8}\.js/) || [])[0];
if (!liveAgent || !chunkText(liveAgent).includes('One workspace chart') || !chunkText(liveAgent).includes('intent.mode==="market"')) {
  throw new Error('console lost the research agent');
}
const exploration = readFileSync(join(out, 'workspace-exploration.css'));
if (exploration.length < 1000 || !exploration.subarray(0, 80).toString('utf8').includes('market workspace')) {
  throw new Error('workspace-exploration.css missing');
}
console.log(JSON.stringify({
  marker: 'TRACK_PROMOTE_V2',
  patchResearchEntry: true,
  ...report,
  shell: liveShell,
  agent: liveAgent,
  console: liveConsole,
  workspace: liveWorkspace,
  app: research.app,
  home: research.home,
  route: research.route,
  search: research.search,
  exploration: exploration.length,
  shellBytes: readFileSync(join(out, '_app/chunks', liveShell)).length,
}));
}

function invokedDirectly() {
  const arg = process.argv[1] || '';
  if (!arg) return false;
  if (/\/(?:build|promote|promote-track-static)\.mjs$/.test(arg)) return true;
  try { return pathToFileURL(resolve(arg)).href === import.meta.url; } catch { return false; }
}

if (invokedDirectly()) await main();
