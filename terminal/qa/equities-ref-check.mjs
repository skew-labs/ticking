import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  alignReference,
  clearEquityMapCache,
  findEquityToken,
  formatBps,
  formatPremiumPct,
  isEquityCategory,
  presentQuote,
  referenceFrame,
  researchRows,
} from '../src/equities-ref.js';

const period = 60_000;
const candles = [
  {time: 1_000, open: 100, high: 101, low: 99, close: 100.5},
  {time: 1_000 + period, open: 100.5, high: 102, low: 100, close: 101},
  {time: 1_000 + period * 2, open: 101, high: 101, low: 99, close: 100},
  {time: 1_000 + period * 10, open: 110, high: 112, low: 109, close: 111},
];
const series = [
  {time: 1_000, close: 100},
  {time: 1_000 + period, close: 100},
  {time: 1_000 + period * 10, close: 110},
  {time: 1_000 + period * 10, close: 110.5},
];
const points = alignReference(candles, series, period);
assert.deepEqual(points.map(point => point.time), [1_000, 1_000 + period, 1_000 + period * 10]);
assert.equal(points[1].close, 100);
assert.equal(points[1].gap, false);
assert.equal(points[2].gap, true, 'a closed-session hole must not bridge the line');
assert.equal(points[2].close, 110.5, 'duplicate bucket keeps the latest close');
assert.ok(Math.abs(points[0].premiumBps - 50) < 1e-9);
assert.equal(points.some(point => point.time === 1_000 + period * 2), false, 'missing ref is not forward-filled');

const closed = presentQuote({
  session: 'closed',
  is_live: false,
  reason: 'market_closed',
  ref_ts: Date.parse('2026-10-23T20:00:00Z'),
  ref_age_s: 3187,
  onchain_price: 761.88,
  ref_price: 761.11,
  premium_bps: 10.1,
  premium_vs: 'ref last trade (closed) — NOT live',
  ref_symbol: 'SPY',
  research: {prev_close: 763.25, day_change_pct: -0.43, day_high: 763.47, day_low: 758.68, volume: 33133505, week52_high: 781.62, week52_low: 629.28},
});
assert.equal(closed.live, false);
assert.equal(closed.priceKindLabel, 'Last close');
assert.equal(closed.sessionText, 'Closed');
assert.equal(/\blive\b/i.test(closed.statusLine), false);
assert.equal(/\blive\b/i.test(closed.priceKindLabel), false);
assert.ok(closed.statusLine.includes('Last close'));
assert.ok(closed.statusLine.includes('2026-10-23 20:00 UTC'));
assert.equal(closed.showPremium, true);
assert.equal(formatBps(closed.premiumBps), '+10.1 bps');
assert.equal(formatPremiumPct(closed.premiumBps), '+0.10%');
assert.equal(closed.tone, 'premium');

const stale = presentQuote({session: 'regular', is_live: false, reason: 'ref_quote_stale', premium_bps: null, ref_price: 10, onchain_price: 11});
assert.equal(stale.live, false);
assert.equal(stale.showPremium, false);
assert.equal(stale.premiumBps, null);
assert.equal(stale.premiumReasonText, 'Reference quote is stale');
assert.equal(/\blive\b/i.test(stale.statusLine), false);

const suspect = presentQuote({session: 'regular', is_live: true, premium_bps: null, unit_check: 'suspect_unit_mismatch', ref_price: 1, onchain_price: 50});
assert.equal(suspect.showPremium, false);
assert.equal(suspect.premiumReason, 'suspect_unit_mismatch');
assert.notEqual(suspect.premiumBps, 0);

const forced = presentQuote({session: 'closed', is_live: true, premium_bps: 5, ref_price: 1, onchain_price: 1});
assert.equal(forced.live, false);
assert.equal(forced.priceKindLabel, 'Last close');

const live = presentQuote({session: 'pre', is_live: true, ref_age_s: 12, premium_bps: -33.2, ref_price: 200, onchain_price: 199.3, ref_ts: Date.now()});
assert.equal(live.live, true);
assert.equal(live.priceKindLabel, 'Live');
assert.equal(live.sessionText, 'Pre');
assert.ok(live.statusLine.startsWith('Live'));
assert.equal(live.tone, 'discount');
assert.equal(formatBps(live.premiumBps), '-33.2 bps');
assert.equal(formatPremiumPct(live.premiumBps), '-0.33%');

const rows = researchRows({underlying: 'SPY'}, closed);
assert.deepEqual(rows.map(row => row.label), ['Underlying', 'Prev close', 'Day change', 'Day range', 'Volume', '52w range']);
assert.equal(rows[0].value, 'SPY');
assert.equal(rows[2].tone, 'down');
assert.match(rows[4].value, /33(\.13)?M|33,133,505/);

const map = [
  {id: 'robinhood:0xspy', symbol: 'SPY', network: 'robinhood', category: 'stock', underlying: 'SPY'},
  {id: 'solana:spyx', symbol: 'SPYx', network: 'solana', category: 'rwa', underlying: 'SPY'},
  {id: 'robinhood:0xbond', symbol: 'SGOV', network: 'robinhood', category: 'bond', underlying: 'SGOV'},
];
assert.equal(findEquityToken(map, {symbol: 'SPY'}).id, 'robinhood:0xspy');
assert.equal(findEquityToken(map, {symbol: 'SPYx'}).network, 'solana');
assert.equal(findEquityToken(map, {symbol: 'spy', network: 'solana'}), null);
assert.equal(findEquityToken(map, {id: 'solana:spyx'}).symbol, 'SPYx');
assert.equal(findEquityToken(map, {symbol: 'BTC'}), null);
assert.equal(isEquityCategory('meme'), false);
assert.equal(isEquityCategory('RWA'), true);
assert.equal(isEquityCategory('bond'), true);

const frame = referenceFrame(
  Array.from({length: 200}, (_, i) => ({time: i * period})),
  [{time: 40 * period, close: 1}],
  150,
);
assert.ok(frame.offset > 0, 'a print behind a long on-chain tail stays in view');
assert.equal(referenceFrame([{time: 1}], [{time: 1, close: 1}], 150).offset, 0);

const html = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8');
assert.doesNotMatch(html, /from ['"]\/terminal\/src\/equities-/);
assert.match(html, /import\('\/terminal\/src\/equities-workspace\.js'\)/);
assert.match(html, /import\('\/terminal\/src\/equities-markets\.js'\)/);
const chart = fs.readFileSync(new URL('../src/chart.js', import.meta.url), 'utf8');
assert.match(chart, /setReferenceOverlay/);
assert.doesNotMatch(chart, /equities-ref|equities-overlay|equities-workspace/);
const terminal = fs.readFileSync(new URL('../src/pro-terminal.js', import.meta.url), 'utf8');
assert.doesNotMatch(terminal, /equities-/);

clearEquityMapCache();
let mapCalls = 0;
globalThis.fetch = async url => {
  mapCalls += 1;
  assert.match(String(url), /\/api\/public\/equities\/map$/);
  return new Response(JSON.stringify({count: 1, items: [{id: 'robinhood:0x1', network: 'robinhood', address: '0x1', symbol: 'NVDA', category: 'stock', underlying: 'NVDA'}]}), {status: 200});
};
const {fetchEquityMap} = await import('../src/equities-ref.js');
const first = await fetchEquityMap({now: 1_000});
const second = await fetchEquityMap({now: 2_000});
assert.equal(mapCalls, 1);
assert.equal(second.items[0].symbol, 'NVDA');
assert.equal(first, second);
clearEquityMapCache();

console.log(JSON.stringify({passed: true, aligned: points.length, liveSuppressedWhenClosed: true, nullPremiumKept: true, lazyEquitiesImport: true}));
