/**
 * Speed up the public homepage without touching the chart workspace,
 * Fast Feed, or Markets route modules.
 *
 * The marketing shell is the deployed static tree (ticking.dev), not this
 * repository. This pass rewrites only the homepage entry:
 *
 * - Drop the two font preloads. No stylesheet declares @font-face, so the
 *   woff2 files download and never apply.
 * - Stop modulepreloading the route style loader and the route style map.
 *   The homepage does not call them; the workspace route still loads
 *   core.css, then the workspace module, on navigation.
 * - Load icon-credit data only after load, and only when an image matches
 *   the credit pattern. Homepage market art does not.
 * - Paint the alerts badge after load, instead of pulling the watch bundle
 *   during first paint.
 * - Load the researched-artwork index (119KB, mostly per-icon fit rules)
 *   only when the overview payload is not already a page of 12. The live
 *   overview is 12 rows and already sends icon paths. Logos keep the
 *   existing 36px object-fit:contain rule.
 *
 * Charts stay on the same origin rewrite to https://api.ticking.dev.
 * Track shell, Fast Feed, and Markets chunks are not rewritten.
 */
import {createHash} from 'node:crypto';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve as pathResolve, sep} from 'node:path';
import {gzipSync} from 'node:zlib';

const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';

export function contentTag(text) {
  const digest = createHash('sha256').update(text).digest().subarray(0, 5);
  let n = 0n;
  for (const b of digest) n = (n << 8n) + BigInt(b);
  let s = '';
  for (let i = 0; i < 8; i++) {
    s = alphabet[Number(n & 31n)] + s;
    n >>= 5n;
  }
  return s;
}

function once(source, from, to, label) {
  const count = source.split(from).length - 1;
  if (count !== 1) throw new Error(`${label} count ${count}`);
  return source.replace(from, to);
}

export function patchHtml(html) {
  let next = html.replace(/\n[ \t]*<link rel="preload" href="\/_app\/[^"]+\.woff2"[^>]*>/g, '');
  if (next.includes('.woff2')) throw new Error('font preload remains');
  for (const name of ['chunk-RWCHFPJ4.js', 'chunk-37CIDBHI.js']) {
    next = once(
      next,
      `\n    <link rel="modulepreload" href="/_app/chunks/${name}" />`,
      '',
      `preload ${name}`,
    );
  }
  if (next.includes('chunk-37CIDBHI') || next.includes('chunk-RWCHFPJ4')) {
    throw new Error('homepage still preloads route-only chunks');
  }
  next = once(
    next,
    '<link rel="icon" href="./favicon.svg" type="image/svg+xml" />',
    '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 32 32\'%3E%3Crect width=\'32\' height=\'32\' rx=\'8\' fill=\'%235658e8\'/%3E%3C/svg%3E" />',
    'favicon',
  );
  if (!next.includes('rel="stylesheet" href="/_app/home-critical-')) throw new Error('critical css missing');
  if (!next.includes('type="module" src="/_app/app-')) throw new Error('app entry missing');
  return next;
}

export function patchApp(source) {
  const styleImport = source.match(/import\{a as v\}from"(\/_app\/chunks\/chunk-RWCHFPJ4\.js)";/);
  if (!styleImport) throw new Error('style loader import missing');
  let next = source.replace(styleImport[0], '');
  next = once(next, 'import"/_app/chunks/chunk-37CIDBHI.js";', '', 'style map import');
  const credits = next.match(/import\("(\/_app\/chunks\/icon-credits-[A-Z2-7]{8}\.js)"\)\.then\(e=>e\.installIconCredits\(\)\)\.catch\(\(\)=>\{\}\)/);
  if (!credits) throw new Error('icon credit import missing');
  const arm = `const tkBootCredits=()=>import("${credits[1]}").then(e=>e.installIconCredits()).catch(()=>{});const tkArmCredits=()=>{window.requestIdleCallback?requestIdleCallback(tkBootCredits,{timeout:2500}):setTimeout(tkBootCredits,2500)};document.readyState==="complete"?tkArmCredits():addEventListener("load",tkArmCredits,{once:!0});`;
  next = next.replace(credits[0], arm);
  next = once(
    next,
    'r=v("core.css").then(()=>import("',
    `r=import("${styleImport[1]}").then(e=>e.a("core.css")).then(()=>import("`,
    'workspace route',
  );
  if (next.includes('chunk-37CIDBHI')) throw new Error('app still imports the style map');
  if (next.includes('import{a as v}from')) throw new Error('app still statically imports the style loader');
  if (!next.includes('.a("core.css")')) throw new Error('core.css load missing');
  if (!next.includes('workspace-')) throw new Error('workspace import missing');
  if (next.includes('track-shell-') || next.includes('fast-feed')) throw new Error('app patch touched chart delivery');
  return next;
}

export function patchHome(source) {
  const unread = source.match(/import\("(\/_app\/chunks\/explorer-watch-[A-Z2-7]{8}\.js)"\)\.then\(s=>s\.unreadCount\(\)\)\.then\(s=>\{let c=t\.querySelector\("\[data-tkx-unread\]"\);c&&s>0&&\(c\.hidden=!1,c\.textContent=s>99\?"99\+":String\(s\)\)\}\)\.catch\(\(\)=>\{\}\)/);
  if (!unread) throw new Error('unread badge import missing');
  const arm = `const tkPaintUnread=()=>import("${unread[1]}").then(s=>s.unreadCount()).then(s=>{let c=t.querySelector("[data-tkx-unread]");c&&s>0&&(c.hidden=!1,c.textContent=s>99?"99+":String(s))}).catch(()=>{});document.readyState==="complete"?(window.requestIdleCallback?requestIdleCallback(tkPaintUnread,{timeout:2500}):setTimeout(tkPaintUnread,2500)):addEventListener("load",()=>{window.requestIdleCallback?requestIdleCallback(tkPaintUnread,{timeout:2500}):setTimeout(tkPaintUnread,2500)},{once:!0})`;
  const next = source.replace(unread[0], arm);
  if (!next.includes(unread[1])) throw new Error('watch chunk reference dropped');
  if (next.split('tkPaintUnread').length < 3) throw new Error('unread import was not deferred');
  if (!next.includes('market-overview-')) throw new Error('market overview import missing');
  return next;
}

export function patchOverview(source) {
  let next = once(
    source,
    'import{a as I,b as U}from"/_app/chunks/chunk-I55HGX35.js";',
    'let I=async()=>null,U=()=>null;const tkArt=()=>tkArt.p||=import("/_app/chunks/chunk-I55HGX35.js").then(e=>(I=e.a,U=e.b,e));',
    'artwork import',
  );
  next = once(next, 'import"/_app/chunks/chunk-37CIDBHI.js";', '', 'overview style map');
  next = once(next, 'await I()', 'await tkArt(),await I()', 'artwork load');
  if (next.includes('import{a as I,b as U}')) throw new Error('artwork chunk is still eager');
  if (!next.includes('chunk-I55HGX35.js')) throw new Error('artwork chunk can no longer load');
  if (!next.includes('U(e.network,e.mint)')) throw new Error('artwork lookup missing');
  if (!next.includes('pageSize!==12')) throw new Error('overview page guard missing');
  return next;
}

export function patchCredits(source) {
  let next = once(source, 'import"/_app/chunks/chunk-37CIDBHI.js";', '', 'credit style map');
  next = once(
    next,
    'function c(e){a.add(e),l||(l=!0,(window.requestIdleCallback||setTimeout)(()=>i?f():p().then(f),{timeout:400}))}',
    'function tkCreditHit(e){if(!e||e.nodeType!==1)return!1;if(e.tagName==="IMG")return!!y(e);if(!e.querySelectorAll)return!1;for(const n of e.querySelectorAll("img"))if(y(n))return!0;return!1}function c(e){if(!i&&!tkCreditHit(e))return;a.add(e),l||(l=!0,(window.requestIdleCallback||setTimeout)(()=>i?f():p().then(f),{timeout:400}))}',
    'credit fetch gate',
  );
  if (next.includes('chunk-37CIDBHI')) throw new Error('credits still import the style map');
  if (!next.includes('installIconCredits')) throw new Error('credit installer missing');
  if (!next.includes('/assets/em/icon-credits.json')) throw new Error('credit catalog missing');
  return next;
}

function rename(text, prefix) {
  return `${prefix}${contentTag(text)}.js`;
}

export function assembleHomepage(files) {
  const html = patchHtml(files.html);
  const app = patchApp(files.app);
  const home = patchHome(files.home);
  const overview = patchOverview(files.overview);
  const credits = patchCredits(files.credits);
  let workspace = files.workspace;
  const old = {
    app: files.appName,
    home: files.homeName,
    overview: files.overviewName,
    credits: files.creditsName,
    workspace: files.workspaceName,
  };
  const overviewName = `market-overview-${contentTag(overview)}.js`;
  const creditsName = `icon-credits-${contentTag(credits)}.js`;
  let homeOut = home.replaceAll(old.overview, overviewName);
  const homeName = `chunk-${contentTag(homeOut)}.js`;
  let workspaceOut = workspace.replaceAll(old.home, homeName);
  const workspaceName = `workspace-${contentTag(workspaceOut)}.js`;
  let appOut = app
    .replaceAll(old.home, homeName)
    .replaceAll(old.credits, creditsName)
    .replaceAll(old.workspace, workspaceName);
  const appName = `app-${contentTag(appOut)}.js`;
  let htmlOut = html.replaceAll(old.app, appName).replaceAll(old.home, homeName);
  const expect = [
    [htmlOut, appName, 'html app'],
    [htmlOut, homeName, 'html home'],
    [appOut, homeName, 'app home'],
    [appOut, workspaceName, 'app workspace'],
    [appOut, creditsName, 'app credits'],
    [homeOut, overviewName, 'home overview'],
    [workspaceOut, homeName, 'workspace home'],
  ];
  for (const [text, name, label] of expect) {
    if (!text.includes(name)) throw new Error(`${label} missing ${name}`);
  }
  for (const stale of Object.values(old)) {
    if (htmlOut.includes(stale) || appOut.includes(stale)) throw new Error(`stale name ${stale}`);
  }
  return {
    html: htmlOut,
    files: {
      [`_app/${appName}`]: appOut,
      [`_app/chunks/${homeName}`]: homeOut,
      [`_app/chunks/${overviewName}`]: overview,
      [`_app/chunks/${creditsName}`]: credits,
      [`_app/chunks/${workspaceName}`]: workspaceOut,
    },
    names: {appName, homeName, overviewName, creditsName, workspaceName},
  };
}

function named(source, pattern, label) {
  const match = source.match(pattern);
  if (!match) throw new Error(`${label} missing`);
  return match[1];
}

async function fetchText(origin, path) {
  const response = await fetch(origin + path, {headers: {'user-agent': 'ticking-home-optimize'}});
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.text();
}

export async function optimizeOrigin(origin) {
  const html = await fetchText(origin, '/');
  const appName = named(html, /src="\/_app\/(app-[A-Z2-7]{8}\.js)"/, 'app');
  const app = await fetchText(origin, `/_app/${appName}`);
  const homeName = named(app, /from"\/_app\/chunks\/(chunk-[A-Z2-7]{8}\.js)"/, 'home chunk');
  const home = await fetchText(origin, `/_app/chunks/${homeName}`);
  const overviewName = named(home, /import\("\/_app\/chunks\/(market-overview-[A-Z2-7]{8}\.js)"\)/, 'overview');
  const creditsName = named(app, /import\("\/_app\/chunks\/(icon-credits-[A-Z2-7]{8}\.js)"\)/, 'credits');
  const workspaceName = named(app, /import\("\/_app\/chunks\/(workspace-[A-Z2-7]{8}\.js)"\)/, 'workspace');
  const [overview, credits, workspace] = await Promise.all([
    fetchText(origin, `/_app/chunks/${overviewName}`),
    fetchText(origin, `/_app/chunks/${creditsName}`),
    fetchText(origin, `/_app/chunks/${workspaceName}`),
  ]);
  return assembleHomepage({
    html, app, home, overview, credits, workspace,
    appName, homeName, overviewName, creditsName, workspaceName,
  });
}

export function writeHomepage(outDir, built) {
  mkdirSync(outDir, {recursive: true});
  writeFileSync(join(outDir, 'index.html'), built.html);
  for (const [path, text] of Object.entries(built.files)) {
    const file = join(outDir, path);
    mkdirSync(dirname(file), {recursive: true});
    writeFileSync(file, text);
  }
  writeFileSync(join(outDir, 'home-optimize.json'), JSON.stringify(built.names, null, 2) + '\n');
}

function contentType(path) {
  if (path.endsWith('.html')) return 'text/html; charset=utf-8';
  if (path.endsWith('.js')) return 'text/javascript; charset=utf-8';
  if (path.endsWith('.css')) return 'text/css; charset=utf-8';
  if (path.endsWith('.json')) return 'application/json; charset=utf-8';
  if (path.endsWith('.svg')) return 'image/svg+xml';
  if (path.endsWith('.woff2')) return 'font/woff2';
  if (path.endsWith('.webp')) return 'image/webp';
  return 'application/octet-stream';
}

export function startPreview(outDir, {port = 4173, origin = 'https://ticking.dev'} = {}) {
  return import('node:http').then(({createServer}) => new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      try {
        const url = new URL(req.url, 'http://127.0.0.1');
        const rel = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname).replace(/^\/+/, '');
        const root = pathResolve(outDir);
        const local = pathResolve(outDir, rel);
        if (rel.includes('..') || !local.startsWith(root + sep)) {
          res.writeHead(400, {'content-type': 'text/plain; charset=utf-8'});
          res.end('bad path');
          return;
        }
        const sendBody = (status, type, body) => {
          const accept = String(req.headers['accept-encoding'] || '');
          const compressible = !/\.(?:webp|woff2|png|jpg|jpeg)$/i.test(rel);
          if (status === 200 && compressible && accept.includes('gzip') && body.length > 256) {
            const gz = gzipSync(body);
            res.writeHead(status, {'content-type': type, 'content-encoding': 'gzip', 'cache-control': 'no-store', vary: 'accept-encoding'});
            res.end(gz);
            return;
          }
          res.writeHead(status, {'content-type': type, 'cache-control': 'no-store'});
          res.end(body);
        };
        try {
          sendBody(200, contentType(rel), readFileSync(local));
          return;
        } catch {
          // Fall through to the live site for every asset this pass does not rewrite.
        }
        const upstream = await fetch(origin + url.pathname + url.search, {headers: {'user-agent': 'ticking-home-preview', 'accept-encoding': 'identity'}});
        const bytes = Buffer.from(await upstream.arrayBuffer());
        sendBody(upstream.status, upstream.headers.get('content-type') || contentType(rel), bytes);
      } catch (error) {
        res.writeHead(502, {'content-type': 'text/plain; charset=utf-8'});
        res.end(String(error && error.message || error));
      }
    });
    server.listen(port, '127.0.0.1', () => resolve(server));
  }));
}

async function main() {
  const origin = process.env.HOME_ORIGIN || 'https://ticking.dev';
  const outDir = process.env.HOME_OUT || 'static/home';
  const built = await optimizeOrigin(origin);
  writeHomepage(outDir, built);
  console.log(JSON.stringify({outDir, ...built.names}));
  if (process.env.HOME_SERVE === '1') {
    const port = Number(process.env.HOME_PORT || 4173);
    await startPreview(outDir, {port, origin});
    console.log(`preview http://127.0.0.1:${port}/`);
    await new Promise(() => {});
  }
}

if (process.argv[1] && process.argv[1].endsWith('optimize-homepage.mjs')) await main();
