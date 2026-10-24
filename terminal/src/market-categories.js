/** Catalog category for Markets. Uses API category when it is present.
 * A missing category falls back to kind, then Other. Labels the API did not
 * send as the taxonomy enum stay as sent — they are not remapped. */

export const CATEGORY_ENUM = ['anchor', 'stock', 'bond', 'rwa', 'programmable', 'defi', 'stable', 'meme', 'other'];
export const CHIP_ORDER = ['stock', 'bond', 'rwa', 'programmable', 'defi', 'stable', 'meme', 'anchor', 'other'];
export const SURFACE_CAP = 500;

const LABELS = {
  anchor: 'Anchor',
  stock: 'Stock',
  bond: 'Bond',
  rwa: 'RWA',
  programmable: 'Programmable',
  defi: 'DeFi',
  stable: 'Stable',
  meme: 'Meme',
  other: 'Other',
};

const TURNOVER_KEYS = ['turnover', 'turnoverUsd', 'turnover_usd', 'volumeUsd', 'volume_usd', 'volume', 'quoteVolume', 'quote_volume'];

export function categoryLabel(key) {
  return LABELS[key] || key;
}

/** @returns {{key:string,label:string,source:'category'|'kind'|'fallback',raw:string}} */
export function resolveMarketCategory(row) {
  const raw = typeof row?.category === 'string' ? row.category.trim() : '';
  if (raw) {
    const key = raw.toLowerCase();
    if (CATEGORY_ENUM.includes(key)) return {key, label: categoryLabel(key), source: 'category', raw};
    return {key: raw, label: raw, source: 'category', raw};
  }
  const kind = typeof row?.kind === 'string' ? row.kind.trim().toLowerCase() : '';
  if (CATEGORY_ENUM.includes(kind)) return {key: kind, label: categoryLabel(kind), source: 'kind', raw: ''};
  return {key: 'other', label: 'Other', source: 'fallback', raw: ''};
}

export function normalizeSubtags(row) {
  if (!Array.isArray(row?.subtags)) return [];
  const out = [];
  for (const item of row.subtags) {
    if (typeof item !== 'string') continue;
    const tag = item.trim();
    if (tag && !out.includes(tag)) out.push(tag);
  }
  return out.slice(0, 8);
}

export function marketTurnover(row) {
  for (const key of TURNOVER_KEYS) {
    const value = row?.[key];
    const n = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : NaN;
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/** stock|bond|rwa|programmable, then stable|defi, then other (and any non-enum label), then meme. */
export function categoryBand(key) {
  const name = String(key || '').toLowerCase();
  if (name === 'stock' || name === 'bond' || name === 'rwa' || name === 'programmable') return 0;
  if (name === 'stable' || name === 'defi') return 1;
  if (name === 'meme') return 3;
  return 2;
}

export function compareMarkets(a, b) {
  const band = categoryBand(resolveMarketCategory(a).key) - categoryBand(resolveMarketCategory(b).key);
  if (band) return band;
  const left = marketTurnover(a);
  const right = marketTurnover(b);
  const delta = (right ?? -Infinity) - (left ?? -Infinity);
  if (delta) return delta;
  return String(a?.symbol || a?.id || '').localeCompare(String(b?.symbol || b?.id || ''));
}

function finiteNumber(value) {
  const n = typeof value === 'number' ? value : NaN;
  return Number.isFinite(n) ? n : null;
}

function marketList(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.markets)) return data.markets;
  if (Array.isArray(data?.tokens)) return data.tokens;
  if (Array.isArray(data?.items)) return data.items;
  return null;
}

export function parseMarketsPayload(data) {
  const list = marketList(data);
  if (!list) throw Error('Markets response has no list.');
  const seen = new Set();
  const rows = [];
  for (const row of list) {
    if (!row || typeof row !== 'object') continue;
    const symbol = typeof row.symbol === 'string' && row.symbol.trim() ? row.symbol.trim() : '';
    const id = typeof row.id === 'string' && row.id.trim() ? row.id.trim() : symbol;
    if (!id) continue;
    const network = typeof row.network === 'string' ? row.network.trim() : '';
    const dedupe = network.toLowerCase() + '\n' + id;
    if (seen.has(dedupe)) continue;
    seen.add(dedupe);
    const category = resolveMarketCategory(row);
    const verification = row.verification;
    rows.push({
      id,
      symbol: symbol || id,
      name: typeof row.name === 'string' && row.name.trim() ? row.name.trim() : (symbol || id),
      network,
      kind: typeof row.kind === 'string' ? row.kind.trim() : '',
      mint: typeof row.mint === 'string' ? row.mint : '',
      category: category.key,
      categoryLabel: category.label,
      categorySource: category.source,
      subtags: normalizeSubtags(row),
      price: finiteNumber(row.price),
      change: finiteNumber(row.change),
      volume: marketTurnover(row),
      icon: typeof row.icon === 'string' ? row.icon : '',
      reviewed: verification?.status === 'reviewed' && verification.id === id && verification.mint === row.mint,
    });
  }
  rows.sort(compareMarkets);
  return rows;
}

export function categoryChips(rows, {enumOnly = false} = {}) {
  const counts = new Map();
  for (const row of rows) {
    const key = resolveMarketCategory(row).key;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const chips = [];
  for (const key of CHIP_ORDER) {
    const count = counts.get(key);
    if (count) chips.push({key, label: categoryLabel(key), count});
  }
  if (!enumOnly) {
    for (const key of [...counts.keys()].filter(name => !CATEGORY_ENUM.includes(name)).sort((a, b) => a.localeCompare(b))) {
      chips.push({key, label: key, count: counts.get(key)});
    }
  }
  return chips;
}

export function selectMarkets(rows, {category = 'all', query = '', network = 'all', cap = SURFACE_CAP} = {}) {
  const needle = String(query || '').trim().toLowerCase();
  const filtered = rows.filter(row => {
    if (category && category !== 'all' && row.category !== category) return false;
    if (network && network !== 'all' && row.network !== network) return false;
    if (!needle) return true;
    const hay = `${row.symbol} ${row.name} ${row.id} ${row.mint} ${row.subtags.join(' ')}`.toLowerCase();
    return hay.includes(needle);
  });
  return {rows: filtered.slice(0, cap), total: filtered.length, capped: filtered.length > cap};
}

export function workspaceHash(row) {
  const symbol = row.symbol || row.id;
  const params = new URLSearchParams();
  if (row.id) params.set('id', row.id);
  if (row.network === 'arbitrum' || row.network === 'robinhood') params.set('chain', row.network);
  const query = params.toString();
  return `#/console/workspace/${encodeURIComponent(symbol)}${query ? `?${query}` : ''}`;
}

export function findMarket(rows, {symbol = '', id = '', chain = ''} = {}) {
  const ident = String(id || '').trim().toLowerCase();
  if (ident) {
    const hit = rows.find(row => row.id.toLowerCase() === ident);
    if (hit) return hit;
  }
  const sym = String(symbol || '').trim().toLowerCase();
  if (!sym) return null;
  const matches = rows.filter(row => row.symbol.toLowerCase() === sym || row.id.toLowerCase() === sym);
  const net = String(chain || '').trim().toLowerCase();
  if (net) {
    const chained = matches.filter(row => row.network.toLowerCase() === net);
    if (chained.length) return chained[0];
  }
  return matches[0] || null;
}
