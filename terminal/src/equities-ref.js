/** Tokenized-equity reference prices. Pure helpers plus the public equities reads.
 * Chart paint, track markers, and Fast Feed do not live here. */

import {parseInterval} from './intervals.js';

export const EQUITY_INTERVALS = ['1m', '5m', '1h', '1d'];
export const EQUITY_CATEGORIES = ['stock', 'rwa', 'bond'];
const CATEGORY_LABELS = {stock: 'Stock', rwa: 'RWA', bond: 'Bond'};
const SESSION_LABELS = {pre: 'Pre', regular: 'Regular', post: 'Post', closed: 'Closed'};
const NETWORK_LABELS = {robinhood: 'Robinhood', solana: 'Solana', ethereum: 'Ethereum', arbitrum: 'Arbitrum'};

const mapCache = {at: 0, body: null};

export function finite(value) {
  const n = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : NaN;
  return Number.isFinite(n) ? n : null;
}

export function categoryLabel(category) {
  const key = String(category || '').toLowerCase();
  return CATEGORY_LABELS[key] || (category ? String(category) : 'Equity');
}

export function isEquityCategory(category) {
  return EQUITY_CATEGORIES.includes(String(category || '').toLowerCase());
}

export function networkLabel(network) {
  const key = String(network || '').toLowerCase();
  return NETWORK_LABELS[key] || (network ? String(network) : 'Unspecified network');
}

export function sessionLabel(session) {
  const key = String(session || '').toLowerCase();
  return SESSION_LABELS[key] || (session ? String(session) : 'Closed');
}

export function reasonLabel(code) {
  if (!code) return '';
  const text = String(code);
  if (text === 'market_closed') return 'Market closed';
  if (text === 'ref_quote_stale') return 'Reference quote is stale';
  if (text === 'ref_no_price') return 'No reference price';
  if (text === 'suspect_unit_mismatch') return 'Unit check failed';
  if (/^ref_source_http_\d+$/.test(text)) return 'Reference source unavailable';
  return text.replaceAll('_', ' ');
}

export function formatPrice(value) {
  const n = finite(value);
  if (n == null) return '—';
  const abs = Math.abs(n);
  const digits = abs >= 1 ? 2 : abs >= 0.01 ? 4 : 6;
  return n.toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
}

export function formatVolume(value) {
  const n = finite(value);
  if (n == null) return '—';
  return new Intl.NumberFormat('en-US', {notation: 'compact', maximumFractionDigits: 2}).format(n);
}

export function formatChange(pct) {
  const n = finite(pct);
  if (n == null) return '—';
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`;
}

export function formatBps(bps) {
  const n = finite(bps);
  if (n == null) return '—';
  const digits = Math.abs(n) >= 100 ? 0 : 1;
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)} bps`;
}

/** Basis points are already (price/ref − 1) × 10,000, so percent is bps / 100. */
export function formatPremiumPct(bps) {
  const n = finite(bps);
  if (n == null) return '—';
  const pct = n / 100;
  return `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`;
}

export function premiumTone(bps) {
  const n = finite(bps);
  if (n == null) return 'unknown';
  if (n > 0.5) return 'premium';
  if (n < -0.5) return 'discount';
  return 'flat';
}

export function premiumWord(bps) {
  const tone = premiumTone(bps);
  if (tone === 'premium') return 'Premium';
  if (tone === 'discount') return 'Discount';
  if (tone === 'flat') return 'In line';
  return 'Premium unavailable';
}

export function formatAge(seconds) {
  const n = finite(seconds);
  if (n == null || n < 0) return '';
  if (n < 90) return `${Math.round(n)}s ago`;
  if (n < 3600) return `${Math.round(n / 60)}m ago`;
  if (n < 86400) return `${Math.round(n / 3600)}h ago`;
  return `${Math.round(n / 86400)}d ago`;
}

export function formatTimestamp(ms) {
  const n = finite(ms);
  if (n == null) return '';
  const date = new Date(n);
  if (Number.isNaN(date.getTime())) return '';
  return date.toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

/** Live only when the service says so and the session is not closed.
 * A missing premium stays missing — it is never rendered as zero. */
export function presentQuote(item, now = Date.now()) {
  if (!item || typeof item !== 'object') return null;
  const session = String(item.session || 'closed').toLowerCase();
  const live = item.is_live === true && session !== 'closed';
  const refTs = finite(item.ref_ts);
  const reportedAge = finite(item.ref_age_s);
  const ageSeconds = reportedAge != null ? reportedAge : refTs != null ? Math.max(0, Math.round((now - refTs) / 1000)) : null;
  const premiumBps = finite(item.premium_bps);
  const unit = typeof item.unit_check === 'string' ? item.unit_check : '';
  const premiumReason = premiumBps == null
    ? (item.reason || (unit && unit !== 'ok' ? unit : 'premium_unavailable'))
    : null;
  const when = formatTimestamp(refTs);
  const age = formatAge(ageSeconds);
  const statusLine = live
    ? ['Live', session === 'regular' ? 'Regular session' : sessionLabel(session), age ? `quote ${age}` : ''].filter(Boolean).join(' · ')
    : ['Last close', when ? `as of ${when}` : '', age].filter(Boolean).join(' · ');
  return {
    live,
    session,
    sessionText: sessionLabel(session),
    priceKindLabel: live ? 'Live' : 'Last close',
    statusLine,
    asOf: refTs,
    ageSeconds,
    onchain: finite(item.onchain_price),
    refPrice: finite(item.ref_price),
    premiumBps,
    showPremium: premiumBps != null,
    tone: premiumTone(premiumBps),
    premiumWord: premiumWord(premiumBps),
    premiumReason,
    premiumReasonText: reasonLabel(premiumReason),
    premiumVs: typeof item.premium_vs === 'string' ? item.premium_vs : '',
    reason: typeof item.reason === 'string' ? item.reason : null,
    reasonText: reasonLabel(item.reason),
    refSymbol: typeof item.ref_symbol === 'string' ? item.ref_symbol : '',
    research: item.research && typeof item.research === 'object' ? item.research : null,
  };
}

export function normalizeMapItem(row) {
  if (!row || typeof row !== 'object') return null;
  const id = typeof row.id === 'string' ? row.id.trim() : '';
  const symbol = typeof row.symbol === 'string' ? row.symbol.trim() : '';
  const network = typeof row.network === 'string' ? row.network.trim().toLowerCase() : '';
  const address = typeof row.address === 'string' ? row.address.trim() : '';
  const category = typeof row.category === 'string' ? row.category.trim().toLowerCase() : '';
  if (!id || !symbol || !network || !address || !isEquityCategory(category)) return null;
  return {
    id,
    network,
    address,
    symbol,
    category,
    underlying: typeof row.underlying === 'string' && row.underlying.trim() ? row.underlying.trim() : symbol,
    issuer: typeof row.issuer === 'string' && row.issuer.trim() ? row.issuer.trim() : '',
  };
}

export function findEquityToken(items, {symbol = '', id = '', network = ''} = {}) {
  const list = Array.isArray(items) ? items : [];
  const ident = String(id || '').trim().toLowerCase();
  if (ident) {
    const hit = list.find(item => item.id.toLowerCase() === ident);
    if (hit) return hit;
  }
  const sym = String(symbol || '').trim().toLowerCase();
  if (!sym) return null;
  const matches = list.filter(item => item.symbol.toLowerCase() === sym);
  const net = String(network || '').trim().toLowerCase();
  if (net) return matches.find(item => item.network === net) || null;
  return matches[0] || null;
}

/** Join ref closes onto candle bucket starts. Missing prints are omitted.
 * A later print is marked `gap` so the line does not bridge a closed session. */
export function alignReference(candles, series, periodMs) {
  const byTime = new Map();
  for (const bar of series || []) {
    const time = finite(bar?.time);
    const close = finite(bar?.close);
    if (time == null || close == null || close <= 0) continue;
    byTime.set(time, close);
  }
  const period = finite(periodMs) ?? 0;
  const points = [];
  let prevTime = null;
  for (const candle of candles || []) {
    const time = finite(candle?.time);
    if (time == null) continue;
    const ref = byTime.get(time);
    if (ref == null) continue;
    const onchain = finite(candle?.close);
    const gap = prevTime != null && period > 0 && time - prevTime > period * 1.5;
    points.push({
      time,
      close: ref,
      onchain: onchain != null && onchain > 0 ? onchain : null,
      premiumBps: onchain != null && onchain > 0 ? (onchain / ref - 1) * 10000 : null,
      gap,
    });
    prevTime = time;
  }
  return points;
}

/** Keep the last underlying print inside the window, with a few later
 * on-chain bars to the right, and do not invent prices across the gap. */
export function referenceFrame(candles, points, count = 150) {
  const bars = Math.max(24, Math.min(480, count || 150));
  if (!candles?.length || !points?.length) return {offset: 0, count: bars};
  const lastRef = points[points.length - 1].time;
  let index = -1;
  for (let i = candles.length - 1; i >= 0; i--) {
    if (candles[i].time === lastRef) { index = i; break; }
  }
  if (index < 0) return {offset: 0, count: bars};
  const after = candles.length - 1 - index;
  const context = 18;
  if (after + context < bars) return {offset: 0, count: bars};
  return {offset: Math.max(0, after - context), count: bars};
}

export function candlePath(token, interval) {
  const params = new URLSearchParams({aggregation: 'token', interval, limit: '500'});
  if (token.network === 'solana') params.set('mint', token.address);
  else params.set('market', `${token.network}:${token.address}`);
  return `/api/public/engine/candles?${params}`;
}

export function seriesPath(token, interval) {
  const params = new URLSearchParams({id: token.id, interval});
  return `/api/public/equities/ref/series?${params}`;
}

export function refPath(token) {
  return `/api/public/equities/ref?id=${encodeURIComponent(token.id)}`;
}

export function candlesFromPayload(body) {
  const rows = Array.isArray(body?.candles) ? body.candles : [];
  const out = [];
  for (const row of rows) {
    let time = finite(row?.time);
    const open = finite(row?.open), high = finite(row?.high), low = finite(row?.low), close = finite(row?.close);
    if (time == null || open == null || high == null || low == null || close == null) continue;
    if (!(open > 0 && high > 0 && low > 0 && close > 0)) continue;
    if (time > 0 && time < 1e12) time *= 1000;
    const volume = finite(row?.volume);
    out.push({time, open, high, low, close, volume: volume != null && volume >= 0 ? volume : 0});
  }
  return out;
}

export function seriesFromPayload(body) {
  const rows = Array.isArray(body?.series) ? body.series : [];
  const out = [];
  for (const row of rows) {
    let time = finite(row?.time);
    const close = finite(row?.close);
    if (time == null || close == null || close <= 0) continue;
    if (time > 0 && time < 1e12) time *= 1000;
    out.push({time, close});
  }
  return out;
}

async function readJson(url, {fetchImpl = fetch, signal, limit = 4 * 1024 * 1024} = {}) {
  const response = await fetchImpl(url, {signal, credentials: 'omit', cache: 'no-store', headers: {accept: 'application/json'}});
  if (!response.ok) throw Error(`Equity data failed (${response.status}).`);
  const text = await response.text();
  if (text.length > limit) throw Error('Equity response exceeds the page memory bound.');
  return JSON.parse(text);
}

export async function fetchEquityMap({fetchImpl = fetch, signal, now = Date.now()} = {}) {
  if (mapCache.body && now - mapCache.at < 15 * 60 * 1000) return mapCache.body;
  const body = await readJson('/api/public/equities/map', {fetchImpl, signal, limit: 1024 * 1024});
  const items = [];
  for (const row of body?.items || []) {
    const item = normalizeMapItem(row);
    if (item) items.push(item);
    if (items.length >= 500) break;
  }
  const next = {count: items.length, items};
  mapCache.body = next;
  mapCache.at = now;
  return next;
}

export function clearEquityMapCache() {
  mapCache.at = 0;
  mapCache.body = null;
}

export async function fetchEquityRef(token, options) {
  const body = await readJson(refPath(token), options);
  const items = Array.isArray(body?.items) ? body.items : [];
  return items.find(item => item?.id === token.id) || items[0] || null;
}

export async function fetchEquitySeries(token, interval, options) {
  const body = await readJson(seriesPath(token, interval), {...options, limit: 6 * 1024 * 1024});
  return {series: seriesFromPayload(body), reason: body?.reason || null, interval: body?.interval || interval};
}

export async function fetchEquityCandles(token, interval, options) {
  const body = await readJson(candlePath(token, interval), {...options, limit: 6 * 1024 * 1024});
  return candlesFromPayload(body);
}

export async function fetchEquityRefsById(ids, {fetchImpl = fetch, signal} = {}) {
  const unique = [...new Set((ids || []).filter(id => typeof id === 'string' && id))];
  const out = new Map();
  const size = 30;
  for (let i = 0; i < unique.length; i += size) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const chunk = unique.slice(i, i + size);
    const body = await readJson(`/api/public/equities/ref?id=${encodeURIComponent(chunk.join(','))}`, {fetchImpl, signal, limit: 2 * 1024 * 1024});
    for (const item of body?.items || []) {
      if (item?.id) out.set(item.id, item);
    }
  }
  return out;
}

export function intervalPeriod(interval) {
  return parseInterval(interval).milliseconds;
}

export function researchRows(token, quote) {
  const research = quote?.research || {};
  const change = finite(research.day_change_pct);
  return [
    {label: 'Underlying', value: token?.underlying || quote?.refSymbol || '—'},
    {label: 'Prev close', value: formatPrice(research.prev_close)},
    {label: 'Day change', value: formatChange(change), tone: change == null ? '' : change > 0 ? 'up' : change < 0 ? 'down' : ''},
    {label: 'Day range', value: researchRange(research.day_low, research.day_high)},
    {label: 'Volume', value: formatVolume(research.volume)},
    {label: '52w range', value: researchRange(research.week52_low, research.week52_high)},
  ];
}

function researchRange(low, high) {
  const left = formatPrice(low), right = formatPrice(high);
  if (left === '—' && right === '—') return '—';
  return `${left} – ${right}`;
}

export function barPremium(points, time) {
  if (!points?.length || !Number.isFinite(time)) return null;
  for (let i = points.length - 1; i >= 0; i--) {
    if (points[i].time === time) return points[i];
  }
  return null;
}
