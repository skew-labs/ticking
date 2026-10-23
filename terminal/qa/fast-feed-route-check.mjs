import assert from 'node:assert/strict';
import {buildFastFeed, decodeBody, shapeFeed} from '../../api/engine-fast-feed.js';

const T0 = 1_700_000_000_000;
const env = {TICKING_CHARTS_EDGE: 'http://127.0.0.1:8895'};

const missing = await buildFastFeed({chain: 'arbitrum', symbol: 'WETH'}, {env: {}});
assert.equal(missing.status, 503);
assert.equal(missing.body.coverage, 'fail_soft');
assert.deepEqual(missing.body.bars_1s, []);
assert.deepEqual(missing.body.wickRejects, []);

let calls = 0;
const sol = await buildFastFeed({chain: 'solana'}, {env, transport() { calls += 1; return {status: 200, body: {bars_1s: [{time: T0, px: 1}]}}; }});
assert.equal(sol.status, 200);
assert.equal(sol.body.reason, 'solana-not-tip');
assert.equal(calls, 0);

const painted = await buildFastFeed({chain: 'arb', symbol: 'WETH', interval: '1s', coverage: 'tip_hot', limit: '120'}, {
  env: {TICKING_CHARTS_EDGE: 'https://charts.example'},
  transport(url, query) {
    assert.match(url, /chain=arbitrum/);
    assert.match(url, /coverage=tip_hot/);
    assert.equal(query.symbol, 'WETH');
    return {status: 200, body: {
      bars_1s: [
        {time: T0, open: 100, high: 200, low: 100, close: 100},
        {time: T0 + 1000, px: 100},
        {time: T0 + 2000, open: 2, high: 20, low: 1, close: 2, source: 'binance_1s'},
        {time: T0 + 3000, px: 1, symbol: 'PEPE'},
      ],
      wickRejects: [{time: T0 + 4000, open: 10, high: 10.1, low: 9.9, close: 10.2, symbol: 'WETH'}],
    }};
  },
});
assert.equal(painted.status, 200);
assert.equal(painted.body.coverage, 'tip_hot');
assert.equal(painted.body.bars_1s[0].high, 105);
assert.equal(painted.body.bars_1s[0].low, 100);
assert.equal(painted.body.bars_1s[1].close, 100);
assert.equal(painted.body.bars_1s[2].high, 20);
assert.equal(painted.body.bars_1s[2].source, 'binance_1s');
assert.equal(painted.body.bars_1s[2].latencyPaintOnly, false);
assert.equal(painted.body.wickRejects[0].high, 10.2);
assert.equal(painted.body.wickRejects[0].low, 9.9);
assert.equal(painted.body.bars_1s.some(row => row.time === T0 + 3000), false);

const rows = Array.from({length: 5000}, (_, i) => ({time: T0 + i * 1000, px: 100}));
const capped = shapeFeed({bars_1s: rows}, '', '', 120);
assert.equal(capped.bars_1s.length, 120);
assert.equal(decodeBody(new Uint8Array(256 * 1024 + 1)).status, 503);

const empty = await buildFastFeed({chain: 'robinhood'}, {env, transport() { return {status: 200, body: {bars_1s: [], wickRejects: []}}; }});
assert.equal(empty.status, 503);
assert.equal(empty.body.reason, 'tip-empty');
console.log('fast-feed route ok');
