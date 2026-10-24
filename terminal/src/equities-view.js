import {formatBps, formatPremiumPct, premiumWord} from './equities-ref.js';

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
}

export function ensureEquitiesCss() {
  if (typeof document === 'undefined' || document.querySelector('link[data-equities-css]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/terminal/equities.css';
  link.dataset.equitiesCss = '1';
  document.head.append(link);
}

export function sessionBadge(view) {
  if (!view) return '<span class="eq-session eq-session-pending">Pricing</span>';
  const session = view.session || 'closed';
  return `<span class="eq-session eq-session-${esc(session)}">${esc(view.sessionText || 'Closed')}</span>`;
}

/** Premium chip for a stock, RWA, or bond card. Null premium prints the reason. */
export function premiumChip(view, {compact = false} = {}) {
  if (!view) return '<span class="eq-chip eq-chip-pending">Pricing</span>';
  if (!view.showPremium) {
    const why = view.premiumReasonText || 'Premium unavailable';
    return `<span class="eq-chip eq-chip-unknown">${esc(why)}</span>`;
  }
  const tone = view.tone || 'flat';
  const bps = formatBps(view.premiumBps);
  if (compact) return `<span class="eq-chip eq-chip-${esc(tone)}">${esc(bps)}</span>`;
  const pct = formatPremiumPct(view.premiumBps);
  return `<span class="eq-chip eq-chip-${esc(tone)}">${esc(premiumWord(view.premiumBps))} ${esc(bps)} · ${esc(pct)}</span>`;
}
