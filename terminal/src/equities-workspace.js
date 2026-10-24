import {TickChart} from './chart.js';
import {createReferenceOverlay} from './equities-overlay.js';
import {
  EQUITY_INTERVALS,
  barPremium,
  categoryLabel,
  fetchEquityCandles,
  fetchEquityMap,
  fetchEquityRef,
  fetchEquitySeries,
  findEquityToken,
  formatPrice,
  intervalPeriod,
  networkLabel,
  presentQuote,
  referenceFrame,
  researchRows,
  alignReference,
} from './equities-ref.js';
import {ensureEquitiesCss, esc, premiumChip, sessionBadge} from './equities-view.js';

const QUOTE_MS = 20000;
const SERIES_MS = 120000;

function shell(token) {
  const issuer = token.issuer ? ` · ${esc(token.issuer)}` : '';
  return `<section class="eq-workspace" aria-label="Equity research">
 <header class="eq-banner">
  <div class="eq-identity">
   <p class="eq-kicker"><a href="#/markets">Markets</a></p>
   <h1>${esc(token.symbol)} <span class="eq-cat">${esc(categoryLabel(token.category))}</span></h1>
   <p class="eq-sub"><span data-eq="name">Underlying ${esc(token.underlying)}</span> · ${esc(networkLabel(token.network))}${issuer}</p>
  </div>
  <div class="eq-compare" aria-live="polite">
   <div><span>On-chain</span><strong data-eq="onchain">—</strong></div>
   <div><span data-eq="ref-label">Real price</span><strong data-eq="ref">—</strong></div>
   <div class="eq-compare-meta"><span data-eq="session"></span><p data-eq="status"></p><p data-eq="reason"></p></div>
  </div>
  <div class="eq-premium-block" data-eq="premium"></div>
 </header>
 <section class="eq-research" aria-label="Underlying research"><dl data-eq="research"></dl></section>
 <section class="eq-chart-card chart-card" aria-label="${esc(token.symbol)} chart">
  <div class="eq-chart-bar">
   <div class="eq-intervals" role="group" aria-label="Interval">${EQUITY_INTERVALS.map(interval => `<button type="button" data-eq-interval="${interval}" aria-pressed="${interval === '1m'}">${interval}</button>`).join('')}</div>
   <p class="eq-legend"><i class="eq-swatch eq-swatch-chain"></i> On-chain <i class="eq-swatch eq-swatch-ref"></i> Real price</p>
   <p class="eq-bar-read" data-eq="bar"></p>
  </div>
  <div class="eq-chart" data-eq="chart" style="height:560px;position:relative"></div>
  <p class="eq-note">The line is the underlying print at each bar’s open. Bars without a print are left blank — a closed session is not filled forward.</p>
 </section>
</section>`;
}

function fillQuote(root, token, item) {
  const view = presentQuote(item);
  const research = item?.research || {};
  const name = root.querySelector('[data-eq="name"]');
  if (name) {
    const title = typeof research.name === 'string' && research.name.trim() ? research.name.trim() : `Underlying ${token.underlying}`;
    const venue = [research.exchange, research.instrument].filter(value => typeof value === 'string' && value.trim()).join(' · ');
    name.textContent = venue ? `${title} · ${venue}` : title;
  }
  const onchain = root.querySelector('[data-eq="onchain"]');
  const ref = root.querySelector('[data-eq="ref"]');
  const refLabel = root.querySelector('[data-eq="ref-label"]');
  if (onchain) onchain.textContent = formatPrice(view?.onchain);
  if (ref) ref.textContent = formatPrice(view?.refPrice);
  if (refLabel) refLabel.textContent = view?.live ? 'Real price' : 'Real price · last close';
  const session = root.querySelector('[data-eq="session"]');
  const status = root.querySelector('[data-eq="status"]');
  const reason = root.querySelector('[data-eq="reason"]');
  if (session) session.innerHTML = sessionBadge(view);
  if (status) status.textContent = view?.statusLine || 'Reference price unavailable';
  if (reason) {
    const extra = !view?.live && view?.reasonText ? view.reasonText : '';
    reason.textContent = extra && extra !== view?.statusLine ? extra : '';
  }
  const premium = root.querySelector('[data-eq="premium"]');
  if (premium) {
    const versus = view?.premiumVs || (!view?.live ? 'Not a live session' : '');
    premium.innerHTML = `${premiumChip(view)}<p>${esc(versus)}</p>`;
  }
  const list = root.querySelector('[data-eq="research"]');
  if (list) {
    list.innerHTML = researchRows(token, view).map(row => `<div><dt>${esc(row.label)}</dt><dd class="${esc(row.tone || '')}">${esc(row.value)}</dd></div>`).join('');
  }
}

function describeBar(point) {
  if (!point) return 'No underlying print on this bar.';
  const bps = point.premiumBps;
  if (!Number.isFinite(bps)) return `Real ${formatPrice(point.close)}`;
  const word = bps > 0.5 ? 'premium' : bps < -0.5 ? 'discount' : 'in line';
  const sign = bps > 0 ? '+' : '';
  return `Real ${formatPrice(point.close)} · ${sign}${bps.toFixed(1)} bps ${word}`;
}

export function mountEquitiesWorkspace(root, {symbol = '', id = '', network = ''} = {}, hooks = {}) {
  ensureEquitiesCss();
  const abort = new AbortController();
  let disposed = false;
  let chart = null;
  let overlay = null;
  let points = [];
  let token = null;
  let interval = '1m';
  let framed = false;
  let quoteTimer = 0;
  let seriesTimer = 0;
  root.innerHTML = `<section class="eq-workspace"><p class="eq-status" role="status">Checking the equity underlying…</p></section>`;

  const fail = message => {
    if (disposed) return;
    root.innerHTML = `<section class="eq-workspace"><h1>Equity workspace unavailable</h1><p class="eq-status">${esc(message)}</p><a href="#/markets">Back to markets</a></section>`;
  };

  const paintSeries = (candles, series) => {
    if (!chart || disposed) return;
    points = alignReference(candles, series, intervalPeriod(interval));
    const first = !framed;
    chart.setSeries(candles, {preserve: framed && chart.offset > 0});
    if (first) {
      const frame = referenceFrame(candles, points, 150);
      chart.count = frame.count;
      chart.offset = frame.offset;
      framed = true;
    }
    overlay?.setPoints(points);
    const note = root.querySelector('[data-eq="bar"]');
    if (note && !note.dataset.pinned) note.textContent = points.length ? `${points.length} bars aligned to the underlying.` : 'No underlying prints align with these bars.';
  };

  let seriesGen = 0;
  const loadSeries = async () => {
    if (!token || disposed) return;
    const gen = ++seriesGen;
    const current = interval;
    const [candles, series] = await Promise.all([
      fetchEquityCandles(token, current, {signal: abort.signal}),
      fetchEquitySeries(token, current, {signal: abort.signal}),
    ]);
    if (disposed || gen !== seriesGen) return;
    if (!candles.length) {
      chart?.setSeries([]);
      const note = root.querySelector('.eq-note');
      if (note) note.textContent = 'No on-chain candles for this interval yet. The reference quote above is still the underlying print.';
      return;
    }
    paintSeries(candles, series.series);
  };

  const loadQuote = async () => {
    if (!token || disposed) return;
    const item = await fetchEquityRef(token, {signal: abort.signal});
    if (disposed) return;
    if (!item) throw Error('No reference quote for this token.');
    fillQuote(root, token, item);
  };

  (async () => {
    try {
      const map = await fetchEquityMap({signal: abort.signal});
      if (disposed) return;
      token = findEquityToken(map.items, {symbol, id, network});
      if (!token) {
        hooks.onUnmapped?.();
        return;
      }
      root.innerHTML = shell(token);
      const host = root.querySelector('[data-eq="chart"]');
      chart = new TickChart(host, {symbol: token.symbol, market: token.id}, {
        interval,
        series: [],
        editable: false,
        volume: true,
        storageScope: 'equity',
        emptyMessage: 'Loading on-chain candles…',
      });
      overlay = createReferenceOverlay(chart);
      host.addEventListener('candlehover', event => {
        const note = root.querySelector('[data-eq="bar"]');
        if (!note) return;
        note.dataset.pinned = '1';
        note.textContent = describeBar(barPremium(points, event.detail?.time));
      }, {signal: abort.signal});
      root.addEventListener('click', event => {
        const button = event.target.closest('[data-eq-interval]');
        if (!button || button.dataset.eqInterval === interval) return;
        interval = button.dataset.eqInterval;
        framed = false;
        root.querySelectorAll('[data-eq-interval]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
        chart.set({interval});
        const note = root.querySelector('[data-eq="bar"]');
        if (note) { delete note.dataset.pinned; note.textContent = 'Aligning the underlying…'; }
        loadSeries().catch(error => {
          if (disposed || error.name === 'AbortError') return;
          const note = root.querySelector('.eq-note');
          if (note) note.textContent = error.message;
        });
      }, {signal: abort.signal});
      const [quoteResult, seriesResult] = await Promise.allSettled([loadQuote(), loadSeries()]);
      if (disposed) return;
      if (quoteResult.status === 'rejected' && seriesResult.status === 'rejected' && quoteResult.reason?.name !== 'AbortError') {
        fail(quoteResult.reason?.message || seriesResult.reason?.message || 'Equity workspace failed.');
        return;
      }
      if (quoteResult.status === 'rejected' && quoteResult.reason?.name !== 'AbortError') {
        const status = root.querySelector('[data-eq="status"]');
        if (status) status.textContent = quoteResult.reason.message;
      }
      if (seriesResult.status === 'rejected' && seriesResult.reason?.name !== 'AbortError') {
        const note = root.querySelector('.eq-note');
        if (note) note.textContent = seriesResult.reason.message;
      }
      quoteTimer = setInterval(() => { if (!document.hidden) loadQuote().catch(() => {}); }, QUOTE_MS);
      seriesTimer = setInterval(() => { if (!document.hidden) loadSeries().catch(() => {}); }, SERIES_MS);
    } catch (error) {
      if (disposed || error.name === 'AbortError') return;
      fail(error.message || 'Equity workspace failed.');
    }
  })();

  return () => {
    disposed = true;
    abort.abort();
    clearInterval(quoteTimer);
    clearInterval(seriesTimer);
    chart?.destroy();
  };
}
