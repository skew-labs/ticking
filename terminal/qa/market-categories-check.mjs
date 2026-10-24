import assert from 'node:assert/strict';
import {
  categoryBand,
  categoryChips,
  findMarket,
  parseMarketsPayload,
  resolveMarketCategory,
  selectMarkets,
  workspaceHash,
} from '../src/market-categories.js';

const meme = {id: 'DOGE', symbol: 'DOGE', name: 'Dogecoin', category: 'meme', volume: 9_000_000, network: 'solana'};
const stock = {id: 'NVDA', symbol: 'NVDA', name: 'NVIDIA', category: 'stock', subtags: ['equity', 'equity'], volume: 10, network: 'robinhood', kind: 'stock'};
const rwa = {id: 'GLD', symbol: 'GLD', name: 'Gold', category: 'RWA', volume: 20, network: 'robinhood', subtags: ['commodity']};
const bond = {id: 'TBILL', symbol: 'TBILL', name: 'T-Bill', category: 'bond', volume: 5, network: 'robinhood'};
const programmable = {id: 'PERM', symbol: 'PERM', name: 'Permissioned', category: 'programmable', volume: 1, subtags: ['permissioned', 4, '']};
const stable = {id: 'USDC', symbol: 'USDC', name: 'USD Coin', category: 'stable', volume: 50_000_000};
const defi = {id: 'JUP', symbol: 'JUP', name: 'Jupiter', category: 'DeFi', volume: 4_000_000};
const other = {id: 'SOL', symbol: 'SOL', name: 'Solana', category: 'Layer 1', volume: 80_000_000};
const untagged = {id: 'RAW', symbol: 'RAW', name: 'Raw', kind: 'token', volume: 70_000_000};
const kindStock = {id: 'AAPL', symbol: 'AAPL', name: 'Apple', kind: 'stock', volume: 3, network: 'robinhood'};
const anchor = {id: 'WETH', symbol: 'WETH', name: 'Wrapped Ether', category: 'anchor', volume: 99};

const rows = parseMarketsPayload({markets: [meme, other, untagged, stable, defi, anchor, kindStock, programmable, bond, rwa, stock]});
assert.deepEqual(rows.map(row => row.symbol), ['GLD', 'NVDA', 'TBILL', 'AAPL', 'PERM', 'USDC', 'JUP', 'SOL', 'RAW', 'WETH', 'DOGE']);
assert.equal(rows.find(row => row.symbol === 'NVDA').subtags.join(','), 'equity');
assert.equal(rows.find(row => row.symbol === 'PERM').subtags.join(','), 'permissioned');
assert.equal(rows.find(row => row.symbol === 'GLD').category, 'rwa');
assert.equal(rows.find(row => row.symbol === 'GLD').categoryLabel, 'RWA');
assert.deepEqual(resolveMarketCategory(other), {key: 'Layer 1', label: 'Layer 1', source: 'category', raw: 'Layer 1'});
assert.equal(resolveMarketCategory({category: 'Memes'}).key, 'Memes');
assert.equal(categoryBand('Memes'), 2);
assert.equal(categoryBand('meme'), 3);
assert.equal(resolveMarketCategory(kindStock).source, 'kind');
assert.deepEqual(resolveMarketCategory(untagged), {key: 'other', label: 'Other', source: 'fallback', raw: ''});
assert.equal(resolveMarketCategory({}).key, 'other');
assert.equal(resolveMarketCategory({category: '   '}).key, 'other');

const chips = categoryChips(rows);
assert.deepEqual(chips.map(chip => chip.key), ['stock', 'bond', 'rwa', 'programmable', 'defi', 'stable', 'meme', 'anchor', 'other', 'Layer 1']);
assert.equal(chips.find(chip => chip.key === 'stock').count, 2);
assert.deepEqual(categoryChips(rows, {enumOnly: true}).map(chip => chip.key), ['stock', 'bond', 'rwa', 'programmable', 'defi', 'stable', 'meme', 'anchor', 'other']);

const surface = selectMarkets(rows, {cap: 4});
assert.deepEqual(surface.rows.map(row => row.symbol), ['GLD', 'NVDA', 'TBILL', 'AAPL']);
assert.equal(surface.capped, true);
assert.equal(surface.total, 11);
const memes = selectMarkets(rows, {category: 'meme', cap: 4});
assert.deepEqual(memes.rows.map(row => row.symbol), ['DOGE']);
const found = selectMarkets(rows, {query: 'doge', cap: 4});
assert.deepEqual(found.rows.map(row => row.symbol), ['DOGE']);

assert.equal(workspaceHash(stock), '#/console/workspace/NVDA?id=NVDA&chain=robinhood');
assert.equal(findMarket(rows, {symbol: 'NVDA', chain: 'robinhood'}).id, 'NVDA');
assert.equal(findMarket(rows, {symbol: 'SI', id: 'SI-9aqmJj'}), null);

const tokens = parseMarketsPayload({
  tokens: [
    {id: 'SI', symbol: 'SI', name: 'Super Inu', category: 'Memes', volume: 100, network: 'solana', mint: 'mint-a', verification: {status: 'reviewed', id: 'SI', mint: 'mint-a'}},
    {id: 'SI-9aqmJj', symbol: 'SI', name: 'Super Intelligence', category: 'Other', volume: 10, network: 'solana', mint: 'mint-b', verification: {status: 'reviewed', id: 'SI-9aqmJj', mint: 'mint-b'}},
  ],
});
assert.equal(findMarket(tokens, {symbol: 'SI', id: 'SI-9aqmJj'}).name, 'Super Intelligence');
assert.equal(tokens.find(row => row.id === 'SI').reviewed, true);
assert.equal(workspaceHash(tokens[1]).includes('id=SI-9aqmJj'), true);

const bare = parseMarketsPayload([{symbol: 'BOND1', kind: 'bond', turnover: 7}]);
assert.equal(bare[0].category, 'bond');
assert.equal(bare[0].categorySource, 'kind');
assert.equal(bare[0].volume, 7);
assert.throws(() => parseMarketsPayload({scope: 'empty'}));

console.log(JSON.stringify({passed: true, checks: ['Category bands rank stock, bond, RWA, and programmable above meme volume', 'Missing category falls back to kind, then Other', 'Non-enum API labels such as Layer 1 and Memes are kept', 'Subtags, chips, surface cap, and workspace lookup']}));
