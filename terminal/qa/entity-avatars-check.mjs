import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {
  avatarSvg,
  hashSeed,
  protocolCategory,
  protocolMark,
  safeLogoPath,
  traderCategory,
  traderMark,
  walletCategory,
  walletMark,
} from '../src/entity-avatars.js';
import {assembleExplore, patchExplore} from '../../scripts/patch-explore-avatars.mjs';

const PROTOCOL_MARK = '${B(d.name,22)||\'<span class="exp-mono" aria-hidden="true">\'+i((d.name||"?").slice(0,1))+"</span>"}';
const DETAIL_MARK = '${B(e.name,26)}';
const TRADER_MARK = '${I("eoa_user",22)}';
const WALLET_MARK = '${I(ye[l.class]||"eoa_user",20)}';

function fixtureExplore() {
  return `import{B as I,F as B}from"/_app/chunks/chunk-HGFXOIKS.js";
function me(){return \`${PROTOCOL_MARK}\`}
function fe(){return \`${DETAIL_MARK}\`}
function ge(){return \`${TRADER_MARK}\`}
function E(){return \`${WALLET_MARK}\`}
function Ce(){o.innerHTML="";let q=e=>o.querySelector(e),f=q("[data-exp-list]");C("protocols?");C("smart-money?");data-exp-row;data-exp-trader;}
function Ue(){text=\`.exp-name{display:inline-flex;align-items:center;gap:10px;min-width:0}\`}
export{Ce as mountExplorer};`;
}

test('the same entity always draws the same character', () => {
  const first = avatarSvg('ramses-v3-dex', {category: 'dex', size: 22});
  const second = avatarSvg('ramses-v3-dex', {category: 'dex', size: 22});
  assert.equal(first, second);
  assert.equal(hashSeed('ramses-v3-dex'), hashSeed('ramses-v3-dex'));
  assert.notEqual(avatarSvg('ramses-v3-dex', {category: 'dex'}), avatarSvg('layerbank', {category: 'lending'}));
});

test('category accessories stay inside an original mascot, not a copied logo', () => {
  const dex = avatarSvg('other-dexs', {category: 'dex', size: 22});
  const lending = avatarSvg('layerbank', {category: 'lending', size: 22});
  const whale = avatarSvg('0xabc', {category: 'whale', size: 22});
  const bot = avatarSvg('0xdef', {category: 'bot', size: 22});
  assert.match(dex, /data-kind="dex"/);
  assert.match(lending, /data-kind="lending"/);
  assert.match(whale, /data-kind="whale"/);
  assert.match(bot, /data-kind="bot"/);
  assert.notEqual(dex, lending);
  for (const svg of [dex, lending, whale, bot]) {
    assert.match(svg, /viewBox="0 0 32 32"/);
    assert.match(svg, /class="exp-avatar"/);
    assert.equal(svg.includes('uniswap'), false);
    assert.equal(svg.includes('coingecko'), false);
    assert.equal(svg.includes('<img'), false);
  }
  const kinds = ['dex', 'lending', 'perps', 'bridge', 'vault', 'staking', 'rwa', 'whale', 'bot', 'lp', 'exchange', 'trader', 'retail', 'contract', 'other'];
  const drawn = new Set(kinds.map(kind => avatarSvg('same-seed', {category: kind})));
  assert.equal(drawn.size, kinds.length);
});

test('a real logo path is kept and everything else becomes a character', () => {
  assert.equal(safeLogoPath('/assets/em/protocol-logos/uniswap-c2ba211911.svg'), '/assets/em/protocol-logos/uniswap-c2ba211911.svg');
  assert.equal(safeLogoPath('/assets/ecosystem/robinhood-chain.jpg'), '/assets/ecosystem/robinhood-chain.jpg');
  assert.equal(safeLogoPath('/assets/em/protocols/aave-77f55c9a3a2b.webp'), '/assets/em/protocols/aave-77f55c9a3a2b.webp');
  assert.equal(safeLogoPath('https://assets.coingecko.com/coins/images/12504/large/uniswap.png'), '');
  assert.equal(safeLogoPath('/assets/em/protocol-logos/../secret.svg'), '');
  assert.equal(safeLogoPath(null), '');

  const branded = '<img class="em-logo" src="/assets/em/protocols/aave.webp" width="22" height="22" alt="">';
  const kept = protocolMark({id: 'aave-v3', name: 'Aave V3', kind: 'lending', icon: null}, 22, branded);
  assert.match(kept, /src="\/assets\/em\/protocols\/aave.webp"/);
  assert.match(kept, /data-tk-avatar="aave-v3"/);
  assert.equal(kept.includes('<svg'), false);

  const apiLogo = protocolMark({
    id: 'uniswap-v3',
    name: 'Uniswap V3',
    kind: 'dex',
    icon: '/assets/em/protocol-logos/uniswap-c2ba211911.svg',
  }, 22, branded);
  assert.match(apiLogo, /src="\/assets\/em\/protocol-logos\/uniswap-c2ba211911.svg"/);
  assert.match(apiLogo, /loading="lazy"/);
  assert.equal(apiLogo.includes('aave.webp'), false);

  const missing = protocolMark({id: 'ramses-v3-dex', name: 'Ramses · V3 DEX', kind: 'dex', icon: null}, 22, '');
  assert.match(missing, /<svg/);
  assert.match(missing, /data-kind="dex"/);
  assert.match(missing, /width="22"/);
  assert.equal(protocolCategory({kind: 'perp', name: 'GMX V2'}), 'perps');
  assert.equal(protocolCategory({kind: '', name: 'Tokenized stocks'}), 'rwa');
});

test('traders and wallet rows get category characters with no logo slot', () => {
  const trader = traderMark({wallet: '0x46fbaf5ab4572ca79ada3aeceb70e8b0a5d454bc', labels: ['smart_money'], flags: []}, 22);
  assert.match(trader, /data-kind="trader"/);
  assert.match(trader, /width="22"/);
  const whale = traderMark({wallet: '0x1', labels: ['whale'], flags: ['leveraged']}, 22);
  assert.equal(traderCategory({labels: ['whale'], flags: ['leveraged']}), 'whale');
  assert.match(whale, /data-kind="whale"/);
  const bot = walletMark({address: '0x20fe', class: 'mev'}, 20);
  assert.equal(walletCategory({class: 'mev'}), 'bot');
  assert.match(bot, /data-kind="bot"/);
  assert.match(bot, /width="20"/);
  assert.notEqual(trader, whale);
});

test('explore patch keeps data reads and stays off the homepage', () => {
  const avatarHref = '/_app/chunks/entity-avatars-AAAAAAAA.js';
  const patched = patchExplore(fixtureExplore(), avatarHref);
  assert.match(patched, /from"\/_app\/chunks\/entity-avatars-AAAAAAAA\.js"/);
  assert.match(patched, /tkProto\(d,22,B\(d\.name,22\)\)/);
  assert.match(patched, /tkProto\(e,26,B\(e\.name,26\)\)/);
  assert.match(patched, /tkTrader\(r,22\)/);
  assert.match(patched, /tkWallet\(l,20\)/);
  assert.match(patched, /tkInstall\(o\)/);
  assert.match(patched, /protocols\?/);
  assert.match(patched, /smart-money\?/);
  assert.match(patched, /data-exp-row/);
  assert.match(patched, /data-exp-trader/);
  assert.equal(patched.includes('eoa_user'), false);
  assert.equal(patched.includes('exp-mono'), false);

  const built = assembleExplore({
    html: '<script type="module" src="/_app/app-UN5WLLFX.js"></script>',
    app: 'import("/_app/chunks/workspace-G5CM2ZT2.js")',
    workspace: 'import("/_app/chunks/console-JHLUHRPZ.js")',
    console: 'import("/_app/chunks/em-explore-WITN7FSJ.js")',
    explore: fixtureExplore(),
    appName: 'app-UN5WLLFX.js',
    workspaceName: 'workspace-G5CM2ZT2.js',
    consoleName: 'console-JHLUHRPZ.js',
    exploreName: 'em-explore-WITN7FSJ.js',
    avatarSource: 'export function protocolMark(){return "avatar"}',
  });
  assert.equal(built.html.includes('app-UN5WLLFX.js'), false);
  assert.equal(built.html.includes('entity-avatars-'), false);
  const app = built.files[`_app/${built.names.appName}`];
  assert.equal(app.includes('entity-avatars-'), false);
  assert.match(app, new RegExp(built.names.workspaceName));
  const explore = built.files[`_app/chunks/${built.names.exploreName}`];
  assert.match(explore, new RegExp(built.names.avatarName));
  assert.match(explore, /tkTrader\(r,22\)/);
  const source = readFileSync(new URL('../src/entity-avatars.js', import.meta.url), 'utf8');
  assert.equal(source.includes('market-overview-'), false);
  assert.equal(source.includes('icon-credits'), false);
  assert.ok(source.length < 12000);
});
