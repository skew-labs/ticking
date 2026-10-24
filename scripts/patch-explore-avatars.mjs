/**
 * Give every Explore protocol and trader row a mark.
 *
 * The live console (ticking.dev) is the deployed static tree, not this
 * repository. Explore paints a protocol logo only when Pe(name) matches a
 * short brand list, and it never reads the API `icon`. Traders always get a
 * generic wallet glyph. This pass keeps a genuine /assets logo and otherwise
 * draws a character from terminal/src/entity-avatars.js.
 *
 * The avatar module is imported by the explore chunk, which the console loads
 * only for #/console/explore. The homepage entry is renamed solely so the
 * new explore chunk is reachable; its bytes are otherwise unchanged.
 */
import {createHash} from 'node:crypto';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve as pathResolve, sep} from 'node:path';
import {gzipSync} from 'node:zlib';
import {fileURLToPath} from 'node:url';

const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
const here = dirname(fileURLToPath(import.meta.url));

const PROTOCOL_MARK = '${B(d.name,22)||\'<span class="exp-mono" aria-hidden="true">\'+i((d.name||"?").slice(0,1))+"</span>"}';
const DETAIL_MARK = '${B(e.name,26)}';
const TRADER_MARK = '${I("eoa_user",22)}';
const WALLET_MARK = '${I(ye[l.class]||"eoa_user",20)}';
const INSTALL_AT = 'let q=e=>o.querySelector(e),f=q("[data-exp-list]")';
const NAME_CSS = '.exp-name{display:inline-flex;align-items:center;gap:10px;min-width:0}';
const AVATAR_CSS = '.exp-avatar{display:block;flex:none;border-radius:50%}.exp-name>.em-logo,.exp-name>.exp-avatar{width:22px;height:22px;flex:none}.exp-detail-head .exp-name>.em-logo,.exp-detail-head .exp-name>.exp-avatar{width:26px;height:26px}.exp-wallets .exp-avatar,.exp-wallets .em-logo{width:20px;height:20px;flex:none}';

export function contentTag(text) {
  const digest = createHash('sha256').update(text).digest().subarray(0, 5);
  let n = 0n;
  for (const byte of digest) n = (n << 8n) + BigInt(byte);
  let out = '';
  for (let i = 0; i < 8; i++) {
    out = alphabet[Number(n & 31n)] + out;
    n >>= 5n;
  }
  return out;
}

function once(source, from, to, label) {
  const count = source.split(from).length - 1;
  if (count !== 1) throw new Error(`${label} count ${count}`);
  return source.replace(from, to);
}

export function avatarModuleSource() {
  return readFileSync(join(here, '../terminal/src/entity-avatars.js'), 'utf8');
}

export function patchExplore(source, avatarHref) {
  if (!avatarHref.startsWith('/_app/chunks/entity-avatars-') || !avatarHref.endsWith('.js')) {
    throw new Error('avatar href must be a hashed explore chunk');
  }
  let next = source;
  if (next.includes('entity-avatars-') || next.includes('tkProto(')) {
    throw new Error('explore chunk is already patched');
  }
  next = `import{installAvatarFallback as tkInstall,protocolMark as tkProto,traderMark as tkTrader,walletMark as tkWallet}from"${avatarHref}";${next}`;
  next = once(next, PROTOCOL_MARK, '${tkProto(d,22,B(d.name,22))}', 'protocol mark');
  next = once(next, DETAIL_MARK, '${tkProto(e,26,B(e.name,26))}', 'detail mark');
  next = once(next, TRADER_MARK, '${tkTrader(r,22)}', 'trader mark');
  next = once(next, WALLET_MARK, '${tkWallet(l,20)}', 'wallet mark');
  next = once(next, INSTALL_AT, `tkInstall(o);${INSTALL_AT}`, 'avatar fallback');
  next = once(next, NAME_CSS, NAME_CSS + AVATAR_CSS, 'avatar css');
  for (const keep of ['data-exp-row', 'data-exp-trader', 'protocols?', 'smart-money?', 'mountExplorer', 'B(d.name,22)', 'B(e.name,26)']) {
    if (!next.includes(keep)) throw new Error(`explore patch dropped ${keep}`);
  }
  if (next.includes('I("eoa_user"') || next.includes('I(ye[l.class]')) throw new Error('generic trader glyph remains');
  if (!next.includes(avatarHref)) throw new Error('avatar import missing');
  return next;
}

function named(source, pattern, label) {
  const match = source.match(pattern);
  if (!match) throw new Error(`${label} missing`);
  return match[1];
}

export function assembleExplore(files) {
  const avatarSource = files.avatarSource || avatarModuleSource();
  const avatarName = `entity-avatars-${contentTag(avatarSource)}.js`;
  const avatarHref = `/_app/chunks/${avatarName}`;
  const explore = patchExplore(files.explore, avatarHref);
  const exploreName = `em-explore-${contentTag(explore)}.js`;
  const consoleOut = once(files.console, files.exploreName, exploreName, 'console explore import');
  const consoleName = `console-${contentTag(consoleOut)}.js`;
  const workspaceOut = once(files.workspace, files.consoleName, consoleName, 'workspace console import');
  const workspaceName = `workspace-${contentTag(workspaceOut)}.js`;
  const appOut = once(files.app, files.workspaceName, workspaceName, 'app workspace import');
  const appName = `app-${contentTag(appOut)}.js`;
  const htmlOut = once(files.html, files.appName, appName, 'html app script');
  for (const [text, name, label] of [
    [htmlOut, appName, 'html app'],
    [appOut, workspaceName, 'app workspace'],
    [workspaceOut, consoleName, 'workspace console'],
    [consoleOut, exploreName, 'console explore'],
    [explore, avatarName, 'explore avatars'],
  ]) {
    if (!text.includes(name)) throw new Error(`${label} missing ${name}`);
  }
  if (htmlOut.includes('entity-avatars-') || appOut.includes('entity-avatars-')) {
    throw new Error('homepage loads the avatar module');
  }
  if (htmlOut.includes(files.appName) || appOut.includes(files.workspaceName)) {
    throw new Error('stale entry name remains');
  }
  return {
    html: htmlOut,
    files: {
      [`_app/${appName}`]: appOut,
      [`_app/chunks/${workspaceName}`]: workspaceOut,
      [`_app/chunks/${consoleName}`]: consoleOut,
      [`_app/chunks/${exploreName}`]: explore,
      [`_app/chunks/${avatarName}`]: avatarSource,
    },
    names: {appName, workspaceName, consoleName, exploreName, avatarName},
  };
}

async function fetchText(origin, path) {
  const response = await fetch(origin + path, {headers: {'user-agent': 'ticking-explore-avatars'}});
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.text();
}

export async function patchOrigin(origin) {
  const html = await fetchText(origin, '/');
  const appName = named(html, /src="\/_app\/(app-[A-Z2-7]{8}\.js)"/, 'app');
  const app = await fetchText(origin, `/_app/${appName}`);
  const workspaceName = named(app, /import\("\/_app\/chunks\/(workspace-[A-Z2-7]{8}\.js)"\)/, 'workspace');
  const workspace = await fetchText(origin, `/_app/chunks/${workspaceName}`);
  const consoleName = named(workspace, /import\("\/_app\/chunks\/(console-[A-Z2-7]{8}\.js)"\)/, 'console');
  const consoleSource = await fetchText(origin, `/_app/chunks/${consoleName}`);
  const exploreName = named(consoleSource, /import\("\/_app\/chunks\/(em-explore-[A-Z2-7]{8}\.js)"\)/, 'explore');
  const explore = await fetchText(origin, `/_app/chunks/${exploreName}`);
  return assembleExplore({
    html, app, workspace, console: consoleSource, explore,
    appName, workspaceName, consoleName, exploreName,
  });
}

export function writeExplore(outDir, built) {
  mkdirSync(outDir, {recursive: true});
  writeFileSync(join(outDir, 'index.html'), built.html);
  for (const [path, text] of Object.entries(built.files)) {
    const file = join(outDir, path);
    mkdirSync(dirname(file), {recursive: true});
    writeFileSync(file, text);
  }
  writeFileSync(join(outDir, 'explore-avatars.json'), JSON.stringify(built.names, null, 2) + '\n');
}

function contentType(path) {
  if (path.endsWith('.html')) return 'text/html; charset=utf-8';
  if (path.endsWith('.js')) return 'text/javascript; charset=utf-8';
  if (path.endsWith('.css')) return 'text/css; charset=utf-8';
  if (path.endsWith('.json')) return 'application/json; charset=utf-8';
  if (path.endsWith('.svg')) return 'image/svg+xml';
  if (path.endsWith('.woff2')) return 'font/woff2';
  if (path.endsWith('.webp')) return 'image/webp';
  if (path.endsWith('.jpg') || path.endsWith('.jpeg')) return 'image/jpeg';
  return 'application/octet-stream';
}

export function startPreview(outDir, {port = 4173, origin = 'https://ticking.dev'} = {}) {
  return import('node:http').then(({createServer}) => new Promise(resolve => {
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
          const compressible = !/\.(?:webp|woff2|png|jpe?g)$/i.test(rel);
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
          // Every asset this pass does not rewrite stays on the live origin.
        }
        const upstream = await fetch(origin + url.pathname + url.search, {
          headers: {'user-agent': 'ticking-explore-preview', 'accept-encoding': 'identity'},
        });
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
  const origin = process.env.EXPLORE_ORIGIN || 'https://ticking.dev';
  const outDir = process.env.EXPLORE_OUT || 'static/explore';
  const built = await patchOrigin(origin);
  writeExplore(outDir, built);
  console.log(JSON.stringify({outDir, ...built.names}));
  if (process.env.EXPLORE_SERVE === '1') {
    const port = Number(process.env.EXPLORE_PORT || 4173);
    await startPreview(outDir, {port, origin});
    console.log(`preview http://127.0.0.1:${port}/#/console/explore?chain=robinhood&tab=protocols`);
    await new Promise(() => {});
  }
}

if (process.argv[1] && process.argv[1].endsWith('patch-explore-avatars.mjs')) await main();
