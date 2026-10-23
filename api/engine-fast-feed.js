/** Same-origin Fast Feed tip. Fail-soft unless the charts edge returns samples. */

const WICK_BAND = 0.05;
const MAX_LIMIT = 120;
const MAX_SAMPLES = 2000;
const MAX_REJECTS = 480;
const MAX_BODY = 256 * 1024;
const TIMEOUT_MS = 1200;
const APPLIED_KEYS = ['bars_1s', 'candles', 'bars', 'prices', 'prints', 'samples', 'ticks'];
const REJECT_KEYS = ['wickRejects', 'rejected', 'rejects'];
const CHAIN = {arb: 'arbitrum', arbitrum: 'arbitrum', rh: 'robinhood', robinhood: 'robinhood', sol: 'solana', solana: 'solana'};

export function chainKey(value) {
  const text = String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  return CHAIN[text] || text;
}
export function isTipChain(chain) {
  const key = chainKey(chain);
  return key === 'arbitrum' || key === 'robinhood';
}
function roundPx(number) { return Math.round(number * 1e8) / 1e8; }
function num(value) {
  if (typeof value === 'boolean' || value == null || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
export function barTime(raw) {
  const value = raw && typeof raw === 'object' && !Array.isArray(raw)
    ? (raw.time ?? raw.t ?? raw.ts ?? raw.timestamp)
    : raw;
  if (value == null || value === '') return null;
  let number = num(value);
  if (number == null || number <= 0) {
    if (typeof value !== 'string') return null;
    number = Date.parse(value);
    if (!Number.isFinite(number) || number <= 0) return null;
  }
  if (number >= 1e18) number /= 1e6;
  else if (number >= 1e15) number /= 1e3;
  else if (number < 1e12) number *= 1000;
  return Math.floor(number / 1000) * 1000;
}
function readNum(raw, keys) {
  if (!raw || typeof raw !== 'object') return null;
  for (const key of keys) {
    const number = num(raw[key]);
    if (number != null) return number;
  }
  return null;
}
function isBinance(bar) {
  if (!bar || typeof bar !== 'object') return false;
  if (bar.source === 'binance_1s' || bar.venue === 'binance_1s') return true;
  return Array.isArray(bar.sources) && bar.sources.includes('binance_1s');
}
function ohlcOk(bar) {
  const open = num(bar?.open), high = num(bar?.high), low = num(bar?.low), close = num(bar?.close);
  if (!(open > 0 && high > 0 && low > 0 && close > 0)) return null;
  if (!(low <= Math.min(open, close) && Math.max(open, close) <= high)) return null;
  const volume = num(bar?.volume);
  return {open, high, low, close, volume: volume != null && volume >= 0 ? volume : 0};
}
export function decodeTipSample(raw) {
  if (Array.isArray(raw)) {
    const time = barTime(raw[0]);
    if (time == null) return null;
    const nums = [];
    for (let i = 1; i < raw.length && i < 6; i++) {
      const number = num(raw[i]);
      if (number == null) break;
      nums.push(number);
    }
    if (nums.length >= 4 && nums[0] > 0 && nums[3] > 0) {
      return {time, open: nums[0], high: nums[1], low: nums[2], close: nums[3], volume: nums[4] >= 0 ? nums[4] : 0};
    }
    if (nums.length >= 1 && nums[0] > 0) return {time, open: nums[0], high: nums[0], low: nums[0], close: nums[0], volume: 0};
    return null;
  }
  if (!raw || typeof raw !== 'object') return null;
  const time = barTime(raw);
  if (time == null) return null;
  const scale = readNum(raw, ['scale', 'pxScale']);
  const div = scale > 1 ? scale : 1;
  const scaled = keys => {
    const number = readNum(raw, keys);
    return number == null ? null : number / div;
  };
  const open = scaled(['open', 'o']);
  const close = scaled(['close', 'c']);
  const px = scaled(['px', 'price', 'p', 'mid', 'last', 'mark']);
  const bid = scaled(['bid', 'b']);
  const ask = scaled(['ask', 'a']);
  if (open != null && !(open > 0)) return null;
  if (close != null && !(close > 0)) return null;
  let bodyOpen = open;
  let bodyClose = close ?? (px > 0 ? px : null);
  if (!(bodyClose > 0) && bid > 0 && ask > 0) bodyClose = (bid + ask) / 2;
  if (!(bodyOpen > 0)) bodyOpen = bodyClose > 0 ? bodyClose : (px > 0 ? px : null);
  if (!(bodyClose > 0)) bodyClose = bodyOpen;
  if (!(bodyOpen > 0 && bodyClose > 0)) return null;
  let high = scaled(['high', 'h']);
  let low = scaled(['low', 'l']);
  if (!(high > 0) && !(low > 0) && bid > 0 && ask > 0) {
    high = Math.max(bid, ask, bodyOpen, bodyClose);
    low = Math.min(bid, ask, bodyOpen, bodyClose);
  }
  if (!(high > 0)) high = Math.max(bodyOpen, bodyClose);
  if (!(low > 0)) low = Math.min(bodyOpen, bodyClose);
  const volumeRaw = readNum(raw, ['volume', 'v', 'vol', 'qty', 'size']);
  const volume = volumeRaw != null && volumeRaw >= 0 ? volumeRaw : 0;
  return {time, open: bodyOpen, high, low, close: bodyClose, volume};
}
function finishTipBar(sample, band = WICK_BAND) {
  const {time, open, close} = sample;
  const top = Math.max(open, close);
  const bot = Math.min(open, close);
  let high = sample.high;
  let low = sample.low;
  if (!(high > 0)) high = top;
  if (!(low > 0)) low = bot;
  if (high < low) { const swap = high; high = low; low = swap; }
  if (high < top) high = top;
  if (low > bot) low = bot;
  high = Math.min(high, roundPx(top * (1 + band)));
  low = Math.max(low, roundPx(bot * (1 - band)));
  if (!(low > 0 && high > 0 && low <= bot && top <= high)) return null;
  return {
    time, open, high, low, close, volume: sample.volume >= 0 ? sample.volume : 0,
    source: 'tip_hot', coverage: 'tip_hot', latencyPaintOnly: true, synthetic: false,
  };
}
function sanitizeTipBar(raw) {
  const decoded = decodeTipSample(raw);
  return decoded ? finishTipBar(decoded) : null;
}
function normSymbol(value) { return String(value || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 16); }
function sameMarket(raw, symbol, mint) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return true;
  const sym = raw.symbol ?? raw.ticker ?? raw.pair;
  const id = raw.mint ?? raw.address ?? raw.token;
  if (symbol && sym) {
    const want = normSymbol(symbol);
    const got = normSymbol(sym);
    if (want && got && want !== got && !got.startsWith(want)) return false;
  }
  if (mint && id && String(id).toLowerCase() !== String(mint).toLowerCase()) return false;
  return true;
}
export function edgeBase(env = process.env) {
  const raw = String(env.TICKING_CHARTS_EDGE || '').trim();
  if (!raw) return '';
  let parts;
  try { parts = new URL(raw); } catch { return ''; }
  if (parts.username || parts.password || parts.search || parts.hash) return '';
  if (parts.protocol !== 'https:' && parts.protocol !== 'http:') return '';
  if (parts.protocol === 'http:' && parts.hostname !== '127.0.0.1' && parts.hostname !== 'localhost') return '';
  return raw.replace(/\/$/, '');
}
function take(obj, keys, symbol, mint, dest) {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return;
  for (const key of keys) {
    const rows = obj[key];
    if (!Array.isArray(rows) || !rows.length) continue;
    for (const row of rows.slice(-MAX_SAMPLES)) if (sameMarket(row, symbol, mint)) dest.push(row);
  }
}
function collect(body, symbol, mint) {
  const applied = [];
  const rejects = [];
  if (Array.isArray(body)) {
    for (const row of body.slice(-MAX_SAMPLES)) if (sameMarket(row, symbol, mint)) applied.push(row);
    return {applied, rejects};
  }
  take(body, APPLIED_KEYS, symbol, mint, applied);
  take(body, REJECT_KEYS, symbol, mint, rejects);
  take(body?.data, APPLIED_KEYS, symbol, mint, applied);
  take(body?.data, REJECT_KEYS, symbol, mint, rejects);
  return {applied: applied.slice(-MAX_SAMPLES), rejects: rejects.slice(-MAX_SAMPLES)};
}
export function shapeFeed(body, symbol, mint, limit) {
  const {applied, rejects} = collect(body, symbol, mint);
  const tips = [];
  const venue = [];
  const kept = [];
  for (const raw of applied) {
    if (isBinance(raw)) {
      const px = ohlcOk(raw);
      const time = barTime(raw);
      if (!px || time == null) continue;
      venue.push({time, open: px.open, high: px.high, low: px.low, close: px.close, volume: px.volume, source: 'binance_1s', synthetic: false, latencyPaintOnly: false});
    } else {
      const tip = sanitizeTipBar(raw);
      if (tip) tips.push(tip);
    }
  }
  for (const raw of rejects) {
    if (isBinance(raw)) continue;
    const tip = sanitizeTipBar(raw);
    if (tip) kept.push(tip);
  }
  const keptVenue = venue.slice(-limit);
  const keptTips = tips.slice(-Math.max(0, limit - keptVenue.length));
  return {
    ok: true,
    coverage: 'tip_hot',
    latencyPaintOnly: true,
    failSoft: true,
    interval: '1s',
    bars_1s: keptTips.concat(keptVenue),
    wickRejects: kept.slice(-MAX_REJECTS),
  };
}
function fail(reason, status = 503) {
  return {status, body: {ok: false, coverage: 'fail_soft', reason, latencyPaintOnly: true, failSoft: true, bars_1s: [], wickRejects: []}};
}
function idle(reason) {
  return {status: 200, body: {ok: true, coverage: 'idle', reason, latencyPaintOnly: true, failSoft: true, bars_1s: [], wickRejects: []}};
}
export function decodeBody(bytes) {
  const size = bytes?.byteLength ?? bytes?.length ?? 0;
  if (bytes == null || size > MAX_BODY) return {status: 503, body: null};
  try {
    const text = typeof bytes === 'string' ? bytes : new TextDecoder().decode(bytes);
    return {status: 200, body: JSON.parse(text)};
  } catch {
    return {status: 503, body: null};
  }
}
function clip(value, length) { return String(value || '').trim().slice(0, length); }
export async function buildFastFeed(query = {}, {transport, env = process.env} = {}) {
  const key = chainKey(query.chain);
  if (key === 'solana') return idle('solana-not-tip');
  if (!isTipChain(key)) return idle('not-tip');
  if (query.interval && query.interval !== '1s') return fail('interval-not-1s', 422);
  let limit = Number(query.limit);
  if (!Number.isFinite(limit)) limit = MAX_LIMIT;
  limit = Math.min(MAX_LIMIT, Math.max(1, Math.trunc(limit)));
  const symbol = clip(query.symbol, 32);
  const mint = clip(query.mint, 64);
  const base = edgeBase(env);
  if (!transport && !base) return fail('charts-edge-unavailable');
  const params = new URLSearchParams();
  params.set('chain', key);
  params.set('interval', '1s');
  params.set('coverage', 'tip_hot');
  params.set('limit', String(limit));
  if (symbol) params.set('symbol', symbol);
  if (mint) params.set('mint', mint);
  const url = base ? `${base}/api/public/engine/fast-feed?${params}` : '';
  let read;
  try {
    read = transport ? await transport(url, Object.fromEntries(params)) : await readEdge(url);
  } catch {
    return fail('charts-edge-unavailable');
  }
  if (!read || read.status !== 200 || !read.body || typeof read.body !== 'object') return fail('charts-edge-unavailable');
  const shaped = shapeFeed(read.body, symbol, mint, limit);
  if (!shaped.bars_1s.length && !shaped.wickRejects.length) return fail('tip-empty');
  return {status: 200, body: shaped};
}
async function readEdge(url) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {signal: ctrl.signal, headers: {accept: 'application/json'}, redirect: 'manual'});
    if (!res.ok) return {status: res.status, body: null};
    if (!res.body || !res.body.getReader) {
      const text = await res.text();
      return decodeBody(text);
    }
    const reader = res.body.getReader();
    const chunks = [];
    let size = 0;
    while (true) {
      const step = await reader.read();
      if (step.done) break;
      size += step.value.byteLength;
      if (size > MAX_BODY) {
        await reader.cancel();
        return {status: 503, body: null};
      }
      chunks.push(step.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return decodeBody(bytes);
  } catch {
    return {status: 503, body: null};
  } finally {
    clearTimeout(timer);
  }
}
function send(res, out) {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.status(out.status).json(out.body);
}
export default async function handler(req, res) {
  try {
    if (req.method !== 'GET') {
      send(res, fail('method', 405));
      return;
    }
    const params = new URL(req.url, 'https://ticking.dev').searchParams;
    send(res, await buildFastFeed({
      chain: params.get('chain') || '',
      symbol: params.get('symbol') || '',
      mint: params.get('mint') || '',
      interval: params.get('interval') || '1s',
      coverage: params.get('coverage') || 'tip_hot',
      limit: params.get('limit') || '120',
    }));
  } catch {
    send(res, fail('charts-edge-unavailable'));
  }
}
