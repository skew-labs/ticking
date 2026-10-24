import {
  EQUITY_CATEGORIES,
  categoryLabel,
  fetchEquityMap,
  fetchEquityRefsById,
  networkLabel,
  presentQuote,
} from './equities-ref.js';
import {ensureEquitiesCss, esc, premiumChip, sessionBadge} from './equities-view.js';

const PAGE_SIZE = 24;

function page() {
  return `<section class="eq-markets" aria-label="Equity markets">
 <header class="eq-banner eq-markets-head">
  <div class="eq-identity">
   <p class="eq-kicker">Markets</p>
   <h1>Equities</h1>
   <p class="eq-sub">Stock, RWA, and bond tokens against the underlying print. Premium is how far the on-chain price sits from that print.</p>
  </div>
  <a class="eq-terminal" href="#/terminal/SOL">Chart terminal</a>
 </header>
 <div class="eq-tools">
  <label>Search<input type="search" placeholder="Symbol or underlying" aria-label="Search equities"></label>
  <div class="eq-intervals" role="group" aria-label="Category">
   <button type="button" data-eq-category="all" aria-pressed="true">All</button>
   ${EQUITY_CATEGORIES.map(category => `<button type="button" data-eq-category="${category}" aria-pressed="false">${esc(categoryLabel(category))}</button>`).join('')}
  </div>
 </div>
 <p class="eq-status" role="status" data-eq="status">Loading mapped tokens…</p>
 <div class="eq-grid" data-eq="grid"></div>
 <nav class="eq-pager" aria-label="Equity pages" hidden data-eq="pager"><button type="button" data-eq-page="-1">Previous</button><span data-eq="page"></span><button type="button" data-eq-page="1">Next</button></nav>
</section>`;
}

function card(token, quote) {
  const view = quote ? presentQuote(quote) : null;
  const href = `#/console/workspace/${encodeURIComponent(token.symbol)}?id=${encodeURIComponent(token.id)}&network=${encodeURIComponent(token.network)}`;
  const name = quote?.research?.name || token.underlying;
  return `<a class="eq-card" href="${esc(href)}" data-equity-category="${esc(token.category)}" data-equity-id="${esc(token.id)}">
 <span class="eq-card-top"><b>${esc(token.symbol)}</b><span class="eq-cat">${esc(categoryLabel(token.category))}</span></span>
 <span class="eq-card-name">${esc(name)}</span>
 <span class="eq-card-meta">${esc(networkLabel(token.network))} · ${esc(token.underlying)}</span>
 <span class="eq-card-badges">${premiumChip(view, {compact: true})}${sessionBadge(view)}</span>
</a>`;
}

export function mountEquityMarkets(root) {
  ensureEquitiesCss();
  const abort = new AbortController();
  let disposed = false;
  let tokens = [];
  let quotes = new Map();
  let category = 'all';
  let query = '';
  let pageIndex = 0;
  const requested = new Set();
  root.innerHTML = page();
  const el = selector => root.querySelector(selector);

  function priceIds(ids) {
    const missing = ids.filter(id => id && !quotes.has(id) && !requested.has(id));
    if (!missing.length || disposed) return;
    missing.forEach(id => requested.add(id));
    fetchEquityRefsById(missing, {signal: abort.signal}).then(batch => {
      if (disposed) return;
      quotes = new Map([...quotes, ...batch]);
      render();
    }).catch(error => {
      if (disposed || error.name === 'AbortError') return;
      missing.forEach(id => requested.delete(id));
      const status = el('[data-eq="status"]');
      if (status) status.textContent = error.message;
    });
  }

  function selected() {
    const needle = query.trim().toLowerCase();
    return tokens.filter(token => {
      if (category !== 'all' && token.category !== category) return false;
      if (!needle) return true;
      return `${token.symbol} ${token.underlying} ${token.network} ${token.issuer}`.toLowerCase().includes(needle);
    });
  }

  function render() {
    if (disposed) return;
    const rows = selected();
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    pageIndex = Math.max(0, Math.min(pageIndex, pages - 1));
    const visible = rows.slice(pageIndex * PAGE_SIZE, (pageIndex + 1) * PAGE_SIZE);
    el('[data-eq="grid"]').innerHTML = visible.map(token => card(token, quotes.get(token.id))).join('') || '<p class="eq-empty">No tokens match this filter.</p>';
    const pager = el('[data-eq="pager"]');
    pager.hidden = pages < 2;
    el('[data-eq="page"]').textContent = `${pageIndex + 1} / ${pages}`;
    pager.querySelectorAll('button').forEach(button => {
      const direction = Number(button.dataset.eqPage);
      button.disabled = direction < 0 ? pageIndex === 0 : pageIndex >= pages - 1;
    });
    const priced = rows.filter(token => quotes.has(token.id)).length;
    el('[data-eq="status"]').textContent = tokens.length
      ? `${rows.length} ${category === 'all' ? 'stock, RWA, and bond tokens' : categoryLabel(category).toLowerCase() + ' tokens'} · ${priced} priced`
      : 'No mapped equity tokens.';
    priceIds(visible.map(token => token.id));
  }

  root.addEventListener('click', event => {
    const categoryButton = event.target.closest('[data-eq-category]');
    if (categoryButton) {
      category = categoryButton.dataset.eqCategory || 'all';
      pageIndex = 0;
      root.querySelectorAll('[data-eq-category]').forEach(button => button.setAttribute('aria-pressed', String(button === categoryButton)));
      render();
      return;
    }
    const pageButton = event.target.closest('[data-eq-page]');
    if (pageButton) {
      pageIndex += Number(pageButton.dataset.eqPage) || 0;
      render();
    }
  }, {signal: abort.signal});
  root.addEventListener('input', event => {
    if (!event.target.matches('input[type="search"]')) return;
    query = event.target.value;
    pageIndex = 0;
    render();
  }, {signal: abort.signal});

  (async () => {
    try {
      const map = await fetchEquityMap({signal: abort.signal});
      if (disposed) return;
      tokens = map.items.slice().sort((a, b) => EQUITY_CATEGORIES.indexOf(a.category) - EQUITY_CATEGORIES.indexOf(b.category) || a.symbol.localeCompare(b.symbol));
      render();
    } catch (error) {
      if (disposed || error.name === 'AbortError') return;
      const status = el('[data-eq="status"]');
      if (status) status.textContent = error.message;
    }
  })();

  return () => { disposed = true; abort.abort(); };
}
