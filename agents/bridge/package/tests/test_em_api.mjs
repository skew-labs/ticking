// Unit checks for em-api.js validation and rendering (node --test). No DOM needed: only pure exports are exercised.
import test from 'node:test';
import assert from 'node:assert/strict';
const m = await import(process.env.EM_API || '../src/em-api.js');

test('address, chain, market and tx validation', () => {
  assert.equal(m.isAddress('0x' + 'a'.repeat(40)), true);
  assert.equal(m.isAddress('0x' + 'A'.repeat(40)), false, 'checksum case must be normalised first');
  assert.equal(m.normAddress(' 0x' + 'Ab'.repeat(20) + ' '), '0x' + 'ab'.repeat(20));
  for (const bad of ['', '0x123', '0x' + 'g'.repeat(40), '0x' + 'a'.repeat(41), null, 42, 'javascript:alert(1)']) assert.equal(m.normAddress(bad), null, String(bad));
  assert.equal(m.isChain('arbitrum'), true); assert.equal(m.isChain('robinhood'), true); assert.equal(m.isChain('solana'), false); assert.equal(m.isChain('__proto__'), false);
  for (const ok of ['robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec', 'arbitrum:0x82af49447d8a07e3bd95bd0d56f35241523fbab1']) assert.equal(m.isMarket(ok), true, ok);
  for (const bad of ['SOL', 'RH-NVDA', 'robinhood:0xD0601CE157DB5BDC3162BBAC2A2C8AF5320D9EEC', 'base:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec', 'robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec?x', 'robinhood:NVDA']) assert.equal(m.isMarket(bad), false, bad);
  assert.equal(m.isTx('0x' + 'f'.repeat(64)), true); assert.equal(m.isTx('0x' + 'f'.repeat(63)), false);
});

test('routes are built only from validated parts', () => {
  const a = '0x' + 'c'.repeat(40);
  assert.equal(m.workspaceWalletRoute('robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec', a), '#/console/workspace/robinhood%3A0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec?panel=wallets&wallet=' + a);
  assert.equal(m.workspaceWalletRoute('robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"><img', a), null);
  assert.equal(m.workspaceWalletRoute('robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec', a + 'x'), null);
  assert.equal(m.mapRoute('robinhood', a), '#/?view=map&chain=robinhood&address=' + a);
  assert.equal(m.mapRoute('evil', a), '#/?view=map');
  assert.equal(m.mapRoute('arbitrum', 'nope'), '#/?view=map&chain=arbitrum');
});

test('wallet card escapes untrusted text and never emits unvalidated hrefs', () => {
  const html = m.walletCard({ address: '0x' + 'd'.repeat(40), chain: 'robinhood', market: 'robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec', entity: '<img src=x onerror=alert(1)>',
    categoryName: 'Whale', sheet: { net_worth: 5e6, collateral: 4e6, debt: -1e6, liquid: 2e6 },
    positions: [{ kind: 'collateral', protocol: '<b>x</b>', asset: 'NVDA', usd: 1e6, flag: 'unverified_principal' }], flags: ['unverified_principal'] });
  assert.ok(!html.includes('<img src=x'), 'entity must be escaped');
  assert.ok(!html.includes('<b>x</b>'));
  assert.ok(html.includes('#/console/workspace/robinhood%3A0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec?panel=wallets&wallet=0x' + 'd'.repeat(40)));
  assert.ok(html.includes('Unverified'));
  const evil = m.walletCard({ address: 'javascript:alert(1)', chain: 'robinhood', market: 'robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec' });
  assert.ok(!/href="javascript/i.test(evil), 'no javascript: hrefs');
  assert.ok(!evil.includes('panel=wallets&wallet=javascript'));
});

test('assistant cards: known kinds only, rows validated', () => {
  assert.equal(m.assistantCard({ kind: 'unknown' }), '');
  assert.equal(m.assistantCard(null), '');
  const top = m.assistantCard({ kind: 'top', title: 'T', chain: 'robinhood', rows: [{ address: '0x' + 'e'.repeat(40), market: 'robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec', category: 'eoa_whale', collateral: { NVDA: 1e6 }, usdgDebt: 5e5, netWorth: 7e5 }, { address: '<script>', market: 'x' }] });
  assert.ok(top.includes('#/console/workspace/robinhood%3A0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec?panel=wallets&wallet=0x' + 'e'.repeat(40)));
  assert.ok(!top.includes('<script>'));
});

test('execution inspector only renders for EM markets and validates tx/wallet', () => {
  assert.equal(m.emInspector({ source: 'binance_spot' }, { time: 1 }), '');
  const html = m.emInspector({ network: 'arbitrum', mint: '0x912ce59144191c1204e64559fe8253a0e49e6548', symbol: 'ARB' }, { time: 1792300000, price: 0.5, base_volume: 10, side: 'B', tx: '0x' + '1'.repeat(64), wallet: 'bad' });
  assert.ok(html.includes('https://arbiscan.io/tx/0x' + '1'.repeat(64)));
  assert.ok(html.includes('Find the wallet behind this swap'));
  assert.ok(!html.includes('data-em-execution-wallet="bad"'));
});

test('formatting', () => {
  assert.equal(m.usd(1234567), '$1.23M'); assert.equal(m.usd(-2500), '-$2.5K'); assert.equal(m.usd(NaN), '—');
  assert.equal(m.short('0x' + 'a'.repeat(40)), '0xaaaa…aaaa');
  assert.equal(m.esc('<"\'&>'), '&lt;&quot;&#39;&amp;&gt;');
});
