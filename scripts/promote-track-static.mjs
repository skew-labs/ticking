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
import {mkdirSync, readFileSync, readdirSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import * as esbuild from 'esbuild';

const BASE = {
  shell: 'track-shell-XQ7HHZ26.js',
  agent: 'console-agent-4BQUAKQK.js',
  console: 'console-SFPUDGQ2.js',
  workspace: 'workspace-BNB2WVBA.js',
  app: 'app-TYCMKHBN.js',
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

export function patchAgent(source, shellName) {
  let next = source;
  const pairs = [
    [
      `import{mountTrackWorkspace}from"/_app/chunks/${BASE.shell}"`,
      `import{mountTrackWorkspace,acceptTrackHandoff,resolveTrackQuery,classifyTrackIntent}from"/_app/chunks/${shellName}"`,
    ],
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
export {mountTrackWorkspace, mountTrackerPage, parseWorkspaceHash, acceptTrackHandoff, resolveTrackQuery} from '${src}/track-workspace.js';
export {classifyTrackIntent} from '${src}/track-intent.js';

LiveTickChart.prototype.setTrackPaint=function(paint,hooks){
  if(!this.trackLayer)this.trackLayer=new TrackPaintLayer(this);
  if(hooks)this.trackLayer.setHooks(hooks);
  const n=this.series?.length||this.options?.series?.length||0;
  if(n>8){this.count=Math.min(480,n+2);this.offset=0;}
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
const agentName = writeHashed('_app/chunks', 'console-agent', Buffer.from(agent));
let consoleJs = readFileSync(join(out, '_app/chunks', BASE.console), 'utf8');
consoleJs = replaceOnce(consoleJs, BASE.agent, agentName, 'console agent ref');
const consoleName = writeHashed('_app/chunks', 'console', Buffer.from(consoleJs));
let workspace = readFileSync(join(out, '_app/chunks', BASE.workspace), 'utf8');
if (!workspace.includes('mountTrackerPage') || workspace.includes('whale-tracker-3BQEZZ6J') || !workspace.includes('sample=1')) {
  throw new Error('workspace tracker gate missing');
}
workspace = replaceOnce(workspace, BASE.shell, shellName, 'workspace shell ref');
workspace = replaceOnce(workspace, BASE.console, consoleName, 'workspace console ref');
const workspaceName = writeHashed('_app/chunks', 'workspace', Buffer.from(workspace));
let app = readFileSync(join(out, '_app', BASE.app), 'utf8');
app = replaceOnce(app, BASE.workspace, workspaceName, 'app workspace ref');
const appName = writeHashed('_app', 'app', Buffer.from(app));
const shellText = shellBytes.toString('utf8');
for (const needle of ['setTrackPaint', 'mountTrackWorkspace', 'classifyTrackIntent', 'data-track-form', 'dumpTagsOnly', 'columnForBridge', 'Math.min(480']) {
  if (!shellText.includes(needle)) throw new Error(`shell missing ${needle}`);
}
if (shellText.includes('whale-tracker')) throw new Error('shell still names whale-tracker');
const agentText = readFileSync(join(out, '_app/chunks', agentName), 'utf8');
for (const needle of ['resolveTrackQuery', 'classifyTrackIntent', shellName, 'Track it on the chart', 'Reading the trail', 'wallet:']) {
  if (!agentText.includes(needle)) throw new Error(`agent missing ${needle}`);
}
if (agentText.includes('What would you like to track?')) throw new Error('agent kept the old prompt');
const workspaceText = readFileSync(join(out, '_app/chunks', workspaceName), 'utf8');
if (!workspaceText.includes(shellName) || !workspaceText.includes(consoleName) || !workspaceText.includes('mountTrackerPage') || workspaceText.includes('whale-tracker')) {
  throw new Error('workspace wiring missing');
}
const appText = readFileSync(join(out, '_app', appName), 'utf8');
if (!appText.includes(workspaceName)) throw new Error('app wiring missing');
let index = readFileSync(join(out, 'index.html'), 'utf8');
index = replaceOnce(index, `/_app/${BASE.app}`, `/_app/${appName}`, 'index app hash');
writeFileSync(join(out, 'index.html'), index);
const exploration = readFileSync(join(out, 'workspace-exploration.css'));
if (exploration.length < 1000 || !exploration.subarray(0, 80).toString('utf8').includes('market workspace')) {
  throw new Error('workspace-exploration.css missing');
}
console.log(JSON.stringify({
  marker: 'TRACK_PROMOTE_V2',
  ...report,
  shell: shellName,
  agent: agentName,
  console: consoleName,
  workspace: workspaceName,
  app: appName,
  exploration: exploration.length,
  shellBytes: shellBytes.length,
}));
