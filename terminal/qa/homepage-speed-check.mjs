import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {
  assembleHomepage,
  patchApp,
  patchCredits,
  patchHome,
  patchHtml,
  patchOverview,
} from '../../scripts/optimize-homepage.mjs';

const html = `<!doctype html>
<html><head>
    <link rel="preload" href="/_app/space-grotesk-latin-LIGEH75C.woff2" as="font" type="font/woff2" crossorigin />
    <link rel="preload" href="/_app/dm-sans-latin-X7UMQEPN.woff2" as="font" type="font/woff2" crossorigin />
    <link rel="icon" href="./favicon.svg" type="image/svg+xml" />
    <link rel="stylesheet" href="/_app/home-critical-7P4DEXAI.css" />
    <link rel="modulepreload" href="/_app/chunks/chunk-JGQFL2MX.js" />
    <link rel="modulepreload" href="/_app/chunks/chunk-ULEMYHD5.js" />
    <link rel="modulepreload" href="/_app/chunks/chunk-RWCHFPJ4.js" />
    <link rel="modulepreload" href="/_app/chunks/chunk-37CIDBHI.js" />
    <script type="module" src="/_app/app-UN5WLLFX.js"></script>
</head><body><div id="app"></div></body></html>`;

const app = 'import{a as y,b as g,c,d as b,e as d}from"/_app/chunks/chunk-JGQFL2MX.js";import"/_app/chunks/chunk-CTR5IUKJ.js";import{a as v}from"/_app/chunks/chunk-RWCHFPJ4.js";import"/_app/chunks/chunk-B7OEYKGZ.js";import{c as h}from"/_app/chunks/chunk-ULEMYHD5.js";import"/_app/chunks/chunk-37CIDBHI.js";h();b();import("/_app/chunks/icon-credits-Z2BXMEFW.js").then(e=>e.installIconCredits()).catch(()=>{});async function E(){return r||(r=v("core.css").then(()=>import("/_app/chunks/workspace-G5CM2ZT2.js")).then(e=>e))}';

const home = 'function it(){import("/_app/chunks/explorer-watch-NT6ZHCUD.js").then(s=>s.unreadCount()).then(s=>{let c=t.querySelector("[data-tkx-unread]");c&&s>0&&(c.hidden=!1,c.textContent=s>99?"99+":String(s))}).catch(()=>{})}async function e(){let{mountMarketOverview:d}=await import("/_app/chunks/market-overview-YDEJGCWY.js");}';

const overview = 'import{a as P,c as H}from"/_app/chunks/chunk-BHZWAGYL.js";import{a as I,b as U}from"/_app/chunks/chunk-I55HGX35.js";import{c as m,d as F}from"/_app/chunks/chunk-CTR5IUKJ.js";import"/_app/chunks/chunk-GEE5L3CO.js";import"/_app/chunks/chunk-37CIDBHI.js";function Z(e){let n=U(e.network,e.mint)||null;return n}if(g.pageSize!==12){let k=K(g.tokens,_);A={...g,tokens:k.slice(M*12,(M+1)*12),total:k.length},await I()}';

const credits = 'import"/_app/chunks/chunk-37CIDBHI.js";var p=()=>fetch("/assets/em/icon-credits.json",{credentials:"omit"});function c(e){a.add(e),l||(l=!0,(window.requestIdleCallback||setTimeout)(()=>i?f():p().then(f),{timeout:400}))}export{A as installIconCredits};';

const workspace = 'import{a as ht}from"/_app/chunks/chunk-JGQFL2MX.js";export function mount(){return ht}';

test('homepage entry drops unused fonts and route-only preloads', () => {
  const next = patchHtml(html);
  assert.equal(next.includes('.woff2'), false);
  assert.equal(next.includes('chunk-37CIDBHI'), false);
  assert.equal(next.includes('chunk-RWCHFPJ4'), false);
  assert.equal(next.includes('favicon.svg'), false);
  assert.equal(next.includes('home-critical-7P4DEXAI.css'), true);
  assert.equal(next.includes('chunk-JGQFL2MX.js'), true);
  assert.equal(next.includes('chunk-ULEMYHD5.js'), true);
});

test('workspace still loads core.css, after the homepage stops importing it', () => {
  const next = patchApp(app);
  assert.equal(next.includes('chunk-37CIDBHI'), false);
  assert.equal(next.includes('import{a as v}from"/_app/chunks/chunk-RWCHFPJ4.js"'), false);
  assert.match(next, /import\("\/_app\/chunks\/chunk-RWCHFPJ4\.js"\)\.then\(e=>e\.a\("core\.css"\)\)\.then\(\(\)=>import\("\/_app\/chunks\/workspace-G5CM2ZT2\.js"\)\)/);
  assert.match(next, /tkBootCredits/);
  assert.equal(next.includes('track-shell-'), false);
  assert.equal(next.includes('fast-feed'), false);
});

test('alerts badge and artwork index wait until they are needed', () => {
  const nextHome = patchHome(home);
  assert.match(nextHome, /requestIdleCallback\(tkPaintUnread/);
  assert.match(nextHome, /explorer-watch-NT6ZHCUD\.js/);
  assert.match(nextHome, /market-overview-YDEJGCWY\.js/);
  const nextOverview = patchOverview(overview);
  assert.equal(nextOverview.includes('import{a as I,b as U}'), false);
  assert.equal(nextOverview.includes('chunk-37CIDBHI'), false);
  assert.match(nextOverview, /import\("\/_app\/chunks\/chunk-I55HGX35\.js"\)/);
  assert.match(nextOverview, /await tkArt\(\),await I\(\)/);
  assert.match(nextOverview, /pageSize!==12/);
  assert.match(nextOverview, /U\(e\.network,e\.mint\)/);
});

test('icon credits do not fetch the catalog for ordinary homepage images', () => {
  const next = patchCredits(credits);
  assert.equal(next.includes('chunk-37CIDBHI'), false);
  assert.match(next, /function tkCreditHit/);
  assert.match(next, /if\(!i&&!tkCreditHit\(e\)\)return/);
  assert.match(next, /\/assets\/em\/icon-credits\.json/);
  assert.match(next, /installIconCredits/);
});

test('renamed homepage files keep the workspace and markets imports', () => {
  const built = assembleHomepage({
    html, app, home, overview, credits, workspace,
    appName: 'app-UN5WLLFX.js',
    homeName: 'chunk-JGQFL2MX.js',
    overviewName: 'market-overview-YDEJGCWY.js',
    creditsName: 'icon-credits-Z2BXMEFW.js',
    workspaceName: 'workspace-G5CM2ZT2.js',
  });
  assert.equal(built.html.includes('app-UN5WLLFX.js'), false);
  assert.equal(built.html.includes('chunk-JGQFL2MX.js'), false);
  assert.match(built.html, /src="\/_app\/app-[A-Z2-7]{8}\.js"/);
  const appOut = built.files[`_app/${built.names.appName}`];
  const homeOut = built.files[`_app/chunks/${built.names.homeName}`];
  const workspaceOut = built.files[`_app/chunks/${built.names.workspaceName}`];
  assert.match(appOut, /e\.a\("core\.css"\)/);
  assert.match(appOut, new RegExp(built.names.workspaceName));
  assert.match(appOut, new RegExp(built.names.homeName));
  assert.match(homeOut, new RegExp(built.names.overviewName));
  assert.match(homeOut, /explorer-watch-NT6ZHCUD\.js/);
  assert.match(workspaceOut, new RegExp(built.names.homeName));
  assert.equal(workspaceOut.includes('chunk-JGQFL2MX.js'), false);
  assert.equal(JSON.stringify(built).includes('track-shell-'), false);
  assert.equal(JSON.stringify(built).includes('api.ticking.dev'), false);
});

test('deploy config keeps the charts edge and caches hashed art', () => {
  const config = JSON.parse(readFileSync(new URL('../../vercel.json', import.meta.url), 'utf8'));
  const rewrites = config.rewrites.map((rule) => `${rule.source} -> ${rule.destination}`);
  assert.deepEqual(rewrites, [
    '/api/public/engine/fast-feed -> /api/engine-fast-feed',
    '/api/:path* -> https://api.ticking.dev/api/:path*',
  ]);
  const researched = config.headers.find((rule) => rule.source === '/assets/tokens/researched/(.*)');
  assert.equal(researched.headers[0].value, 'public, max-age=31536000, immutable');
  const fastFeed = config.headers.find((rule) => rule.source === '/api/public/engine/fast-feed');
  assert.equal(fastFeed.headers[0].value, 'no-store');
  assert.equal(JSON.stringify(config).includes('TICKING_CHARTS_EDGE'), false);
});
