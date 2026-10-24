import {TickChart} from './chart.js';
import {mountChartControls} from './chart-controls.js';
import {bindDataFeed} from '../sdk/data-feed.js';
import {terminalPage, mountTerminal} from './pro-terminal.js';
import {
  categoryChips,
  findMarket,
  parseMarketsPayload,
  selectMarkets,
  workspaceHash,
} from './market-categories.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const finite = value => typeof value === 'number' && Number.isFinite(value);
const PAGE_SIZE = 24;
const NETWORK_LABELS = {
  solana: 'Solana',
  robinhood: 'Robinhood',
  arbitrum: 'Arbitrum',
  exchange: 'Exchange',
  ethereum: 'Ethereum',
  base: 'Base',
  optimism: 'Optimism',
  polygon: 'Polygon',
  linea: 'Linea',
  unichain: 'Unichain',
  arc: 'Arc',
};

function networkLabel(network) {
  if (!network) return 'Unspecified network';
  return NETWORK_LABELS[network] || network;
}

function money(value) {
  if (!finite(value)) return '—';
  return value.toLocaleString('en-US', {style: 'currency', currency: 'USD', notation: Math.abs(value) >= 1000 ? 'compact' : 'standard', maximumFractionDigits: Math.abs(value) >= 1000 ? 2 : 4});
}

function pct(value) {
  if (!finite(value)) return '';
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function safeIcon(url) {
  if (typeof url !== 'string' || !url) return '';
  if (url.startsWith('/api/public/') || url.startsWith('/assets/')) return url;
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'https:' && !parsed.username && !parsed.password) return parsed.href;
  } catch { /* ignore non-URLs */ }
  return '';
}

async function loadPublicMarkets(signal) {
  const response = await fetch('/api/public/markets', {signal, credentials: 'omit', cache: 'no-store'});
  if (!response.ok) throw Error(`Markets read failed (${response.status}).`);
  const text = await response.text();
  if (text.length > 4 * 1024 * 1024) throw Error('Markets response exceeds the page memory bound.');
  return parseMarketsPayload(JSON.parse(text));
}

function marketsPage() {
  return `<section class="mk-markets" aria-label="Markets">
 <header class="mk-head"><div><p class="mk-kicker">Markets</p><h1>Categories</h1><p class="mk-lead">Stock, bond, RWA, and programmable assets rank ahead of stables and DeFi, then other assets, then memes. Turnover breaks ties inside a category.</p></div><a class="mk-terminal" href="#/terminal/SOL">Chart terminal</a></header>
 <div class="mk-tools"><label class="mk-search">Search<input type="search" placeholder="Symbol, name, or tag" aria-label="Search markets"></label><label class="mk-network">Network<select aria-label="Network"><option value="all">All networks</option></select></label></div>
 <div class="mk-chips" role="group" aria-label="Category"></div>
 <p class="mk-status" role="status">Loading markets…</p>
 <div class="mk-grid"></div>
 <nav class="mk-pager" aria-label="Market pages" hidden><button type="button" data-mk="page" data-direction="-1">Previous</button><span class="mk-page"></span><button type="button" data-mk="page" data-direction="1">Next</button></nav>
</section>`;
}

function card(row) {
  const icon = safeIcon(row.icon);
  const mark = icon ? `<img src="${esc(icon)}" alt="" width="28" height="28">` : `<span class="mk-mark" aria-hidden="true">${esc(row.symbol.slice(0, 2))}</span>`;
  const tags = row.subtags.length ? `<span class="mk-tags">${row.subtags.map(tag => `<i>${esc(tag)}</i>`).join('')}</span>` : '';
  const change = pct(row.change);
  return `<a class="mk-card" href="${esc(workspaceHash(row))}">${mark}<span class="mk-card-copy"><span class="mk-card-top"><b>${esc(row.symbol)}</b><span class="mk-pill">${esc(row.categoryLabel)}</span></span><span class="mk-name">${esc(row.name)}</span>${tags}<span class="mk-meta">${esc(networkLabel(row.network))}${finite(row.volume) ? ` · ${esc(money(row.volume))}` : ''}${change ? ` · <span class="${row.change >= 0 ? 'up' : 'down'}">${esc(change)}</span>` : ''}</span></span></a>`;
}

export function mountMarkets(root) {
  const abort = new AbortController();
  let disposed = false;
  let rows = [];
  let category = 'all';
  let query = '';
  let network = 'all';
  let page = 0;
  root.innerHTML = marketsPage();
  const el = selector => root.querySelector(selector);

  function render() {
    if (disposed) return;
    const chips = categoryChips(rows);
    if (category !== 'all' && !chips.some(chip => chip.key === category)) category = 'all';
    el('.mk-chips').innerHTML = [`<button type="button" data-mk="category" data-category="all" aria-pressed="${category === 'all'}">All</button>`, ...chips.map(chip => `<button type="button" data-mk="category" data-category="${esc(chip.key)}" aria-pressed="${category === chip.key}">${esc(chip.label)} <small>${chip.count}</small></button>`)].join('');
    const networks = [...new Set(rows.map(row => row.network).filter(Boolean))].sort();
    const select = el('.mk-network select');
    const previous = select.value || network;
    select.innerHTML = `<option value="all">All networks</option>${networks.map(name => `<option value="${esc(name)}">${esc(networkLabel(name))}</option>`).join('')}`;
    select.value = networks.includes(previous) || previous === 'all' ? previous : 'all';
    network = select.value;
    const selected = selectMarkets(rows, {category, query, network});
    const pages = Math.max(1, Math.ceil(selected.rows.length / PAGE_SIZE));
    page = Math.max(0, Math.min(page, pages - 1));
    const visible = selected.rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
    el('.mk-grid').innerHTML = visible.map(card).join('') || `<p class="mk-empty">${rows.length ? 'No markets match this filter.' : 'No markets loaded.'}</p>`;
    const pager = el('.mk-pager');
    pager.hidden = pages < 2;
    el('.mk-page').textContent = `${page + 1} / ${pages}`;
    pager.querySelectorAll('button').forEach(button => { button.disabled = Number(button.dataset.direction) < 0 ? page === 0 : page >= pages - 1; });
    const shown = selected.total === selected.rows.length ? `${selected.total}` : `${selected.rows.length} of ${selected.total}`;
    el('.mk-status').textContent = rows.length
      ? `${shown} markets · category, then turnover${selected.capped ? ' · first 500 of this view' : ''}`
      : 'No markets in this catalog.';
  }

  root.addEventListener('click', event => {
    const button = event.target.closest('[data-mk]');
    if (!button) return;
    if (button.dataset.mk === 'category') {
      category = button.dataset.category || 'all';
      page = 0;
      render();
    } else if (button.dataset.mk === 'page') {
      page += Number(button.dataset.direction) > 0 ? 1 : -1;
      render();
      el('.mk-grid')?.scrollIntoView({block: 'nearest'});
    }
  }, {signal: abort.signal});
  root.addEventListener('input', event => {
    if (!event.target.matches('.mk-search input')) return;
    query = event.target.value;
    page = 0;
    render();
  }, {signal: abort.signal});
  root.addEventListener('change', event => {
    if (!event.target.matches('.mk-network select')) return;
    network = event.target.value;
    page = 0;
    render();
  }, {signal: abort.signal});

  (async () => {
    try {
      rows = await loadPublicMarkets(abort.signal);
      if (!disposed) render();
    } catch (error) {
      if (disposed || error.name === 'AbortError') return;
      el('.mk-status').textContent = error.message;
      el('.mk-grid').innerHTML = `<p class="mk-empty">${esc(error.message)}</p>`;
    }
  })();

  return () => { disposed = true; abort.abort(); };
}

function chartWorkspace(row) {
  const tags = row.subtags.length ? row.subtags.map(tag => `<i>${esc(tag)}</i>`).join('') : '';
  return `<section class="mk-workspace" aria-label="Workspace chart">
 <header class="mk-head"><div><p class="mk-kicker"><a href="#/markets">Markets</a></p><h1>${esc(row.symbol)} <span class="mk-pill">${esc(row.categoryLabel)}</span></h1><p class="mk-lead">${esc(row.name)} · ${esc(networkLabel(row.network))}${row.categorySource === 'kind' ? ' · category from kind' : row.categorySource === 'fallback' ? ' · category not on this row' : ''}</p>${tags ? `<p class="mk-tags">${tags}</p>` : ''}</div></header>
 <section class="mk-chart-card chart-card" aria-label="${esc(row.symbol)} chart"><div class="mk-chart"></div><p class="mk-chart-status" role="status">Opening recorded history…</p></section>
</section>`;
}

function mountSparseChart(root, row) {
  root.innerHTML = chartWorkspace(row);
  const host = root.querySelector('.mk-chart');
  const solana = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(row.mint || '');
  const exchange = row.network === 'exchange' && row.id;
  const chart = new TickChart(host, {symbol: row.symbol, market: row.mint || row.id}, {
    editable: true,
    interval: '1m',
    series: [],
    storageScope: 'workspace',
    emptyMessage: solana || exchange ? 'Loading recorded history…' : 'No retained candle series is attached for this category row.',
  });
  let controls = null;
  let feed = null;
  const status = root.querySelector('.mk-chart-status');
  if (solana || exchange) {
    controls = mountChartControls(chart, root.querySelector('.mk-chart-card'), {quickIntervals: ['1m', '5m', '15m', '1h', '1d'], customIntervals: false});
    feed = bindDataFeed(chart, {
      ...(exchange ? {market: row.id, aggregation: 'exchange'} : {mint: row.mint, aggregation: 'token'}),
      apiBase: location.origin,
      onStatus(update) {
        if (!status) return;
        status.textContent = update.error ? update.error : update.loading ? 'Loading recorded history…' : `${row.categoryLabel} · recorded history`;
      },
    });
  } else if (status) {
    status.textContent = `${row.categoryLabel} is on the catalog row. This workspace has no retained candle feed for ${networkLabel(row.network)} yet.`;
  }
  return () => { feed?.(); controls?.(); chart.destroy(); };
}

export function mountWorkspace(root, {symbol = '', id = '', chain = ''} = {}) {
  const abort = new AbortController();
  let disposed = false;
  let inner = null;
  root.innerHTML = `<section class="mk-workspace"><p class="mk-status" role="status">Opening workspace chart…</p></section>`;
  (async () => {
    try {
      const rows = await loadPublicMarkets(abort.signal);
      if (disposed) return;
      const row = findMarket(rows, {symbol, id, chain});
      if (!row) {
        root.innerHTML = `<section class="mk-workspace"><h1>Market not in the catalog</h1><p class="mk-lead">${esc(symbol || id)} is not on /api/public/markets.</p><a href="#/markets">Back to markets</a></section>`;
        return;
      }
      if (row.reviewed) {
        root.innerHTML = terminalPage();
        inner = mountTerminal(root, {ident: row.id});
        return;
      }
      inner = mountSparseChart(root, row);
    } catch (error) {
      if (disposed || error.name === 'AbortError') return;
      root.innerHTML = `<section class="mk-workspace"><h1>Workspace unavailable</h1><p class="mk-lead">${esc(error.message)}</p><a href="#/markets">Back to markets</a></section>`;
    }
  })();
  return () => { disposed = true; abort.abort(); inner?.(); };
}
