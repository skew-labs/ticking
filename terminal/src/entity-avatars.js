/**
 * Deterministic web3 mascot avatars for Explore rows that have no logo.
 *
 * A real asset (API icon or an existing brand <img>) is kept. Everything else
 * gets an original character: palette and face come from the entity id, and
 * the accessory comes from the category (DEX, lending, perps, bridge, vault,
 * staking, RWA, whale, bot, …). Inline SVG, no network, no trademark artwork.
 */

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));

const PALETTES = [
  {main: '#6d5efc', light: '#e4e0ff', deep: '#2a215c', blush: '#ff8fb8'},
  {main: '#14b8a6', light: '#d5fbf4', deep: '#134e4a', blush: '#fb7185'},
  {main: '#f59e0b', light: '#fff3c4', deep: '#78350f', blush: '#fb7185'},
  {main: '#f43f5e', light: '#ffe0e6', deep: '#881337', blush: '#fda4af'},
  {main: '#3b82f6', light: '#dbeafe', deep: '#1e3a8a', blush: '#fb7185'},
  {main: '#22c55e', light: '#dcfce7', deep: '#14532d', blush: '#fb7185'},
  {main: '#d946ef', light: '#fae8ff', deep: '#701a75', blush: '#fda4af'},
  {main: '#06b6d4', light: '#cffafe', deep: '#164e63', blush: '#fb7185'},
  {main: '#f97316', light: '#ffedd5', deep: '#7c2d12', blush: '#fda4af'},
  {main: '#8b5cf6', light: '#ede9fe', deep: '#4c1d95', blush: '#fda4af'},
];

const KIND_CATEGORY = {
  dex: 'dex', amm: 'dex', swap: 'dex',
  lending: 'lending', lend: 'lending', borrow: 'lending', cdp: 'lending',
  perp: 'perps', perps: 'perps', perpetual: 'perps', derivatives: 'perps',
  bridge: 'bridge',
  vault: 'vault',
  stake: 'staking', staking: 'staking',
  rwa: 'rwa', stock: 'rwa', stocks: 'rwa', equity: 'rwa', equities: 'rwa',
};

const NAME_HINTS = [
  [/perp|future/, 'perps'],
  [/lend|borrow/, 'lending'],
  [/bridge|gateway/, 'bridge'],
  [/vault|erc-?4626/, 'vault'],
  [/stak|restak/, 'staking'],
  [/stock|equit|\brwa\b|treasur/, 'rwa'],
  [/swap|\bdex\b|\bamm\b/, 'dex'],
];

const WALLET_CATEGORY = {
  retail: 'retail',
  whale: 'whale',
  mev: 'bot',
  contract: 'contract',
  exchange: 'exchange',
  bridge: 'bridge',
  lp: 'lp',
};

const LOGO_PATH = /^\/assets\/(?:em\/protocol-logos|em\/protocols|em\/chains|ecosystem)\/[A-Za-z0-9._-]+\.(?:svg|webp|png|jpe?g)$/;

export function hashSeed(value) {
  const text = String(value ?? '');
  let hash = 2166136261;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

export function safeLogoPath(url) {
  return typeof url === 'string' && LOGO_PATH.test(url) ? url : '';
}

export function protocolCategory(row) {
  const kind = String(row?.kind || '').toLowerCase();
  if (KIND_CATEGORY[kind]) return KIND_CATEGORY[kind];
  const name = String(row?.name || '').toLowerCase();
  for (const [pattern, category] of NAME_HINTS) {
    if (pattern.test(name)) return category;
  }
  return 'other';
}

export function traderCategory(row) {
  const blob = [
    ...(Array.isArray(row?.labels) ? row.labels : []),
    ...(Array.isArray(row?.flags) ? row.flags : []),
    row?.class || '',
  ].join(' ').toLowerCase();
  if (/mev|\bbot\b|sandwich/.test(blob)) return 'bot';
  if (/whale/.test(blob)) return 'whale';
  if (/leverag|perp/.test(blob)) return 'perps';
  if (/\blp\b|liquid/.test(blob)) return 'lp';
  if (/bridge/.test(blob)) return 'bridge';
  if (/exchange|\bcex\b/.test(blob)) return 'exchange';
  if (/contract/.test(blob)) return 'contract';
  return 'trader';
}

export function walletCategory(row) {
  const key = String(row?.class || '').toLowerCase();
  return WALLET_CATEGORY[key] || traderCategory(row);
}

function box(size) {
  const n = Number(size);
  if (!Number.isFinite(n)) return 22;
  return Math.max(16, Math.min(64, Math.round(n)));
}

function eyes(style, ink, shift) {
  const x = 11.2 + shift;
  const y = 15;
  if (style === 1) {
    return `<rect x="${(x - 1.6).toFixed(1)}" y="13.2" width="3.2" height="3.2" rx=".4" fill="${ink}"/><rect x="${(x + 8.6).toFixed(1)}" y="13.2" width="3.2" height="3.2" rx=".4" fill="${ink}"/><rect x="${(x - 0.7).toFixed(1)}" y="13.8" width="1.1" height="1.1" fill="#fff"/><rect x="${(x + 9.5).toFixed(1)}" y="13.8" width="1.1" height="1.1" fill="#fff"/>`;
  }
  if (style === 2) {
    return `<path d="M${(x - 2).toFixed(1)} ${y}c1.2-1.5 2.8-1.5 4 0" fill="none" stroke="${ink}" stroke-width="1.5" stroke-linecap="round"/><path d="M${(x + 7.6).toFixed(1)} ${y}c1.2-1.5 2.8-1.5 4 0" fill="none" stroke="${ink}" stroke-width="1.5" stroke-linecap="round"/>`;
  }
  if (style === 3) {
    return `<ellipse cx="${x.toFixed(1)}" cy="${y}" rx="2.3" ry="2.7" fill="${ink}"/><ellipse cx="${(x + 9.6).toFixed(1)}" cy="${y}" rx="2.3" ry="2.7" fill="${ink}"/><circle cx="${(x + 0.7).toFixed(1)}" cy="${y - 0.8}" r=".8" fill="#fff"/><circle cx="${(x + 10.3).toFixed(1)}" cy="${y - 0.8}" r=".8" fill="#fff"/>`;
  }
  return `<circle cx="${x.toFixed(1)}" cy="${y}" r="1.7" fill="${ink}"/><circle cx="${(x + 9.6).toFixed(1)}" cy="${y}" r="1.7" fill="${ink}"/><circle cx="${(x + 0.6).toFixed(1)}" cy="${y - 0.6}" r=".55" fill="#fff"/><circle cx="${(x + 10.2).toFixed(1)}" cy="${y - 0.6}" r=".55" fill="#fff"/>`;
}

function mouth(style, ink) {
  if (style === 0) return `<path d="M13 19.7c1 .95 5 .95 6 0" fill="none" stroke="${ink}" stroke-width="1.3" stroke-linecap="round"/>`;
  if (style === 1) return `<ellipse cx="16" cy="20" rx="1.45" ry="1.05" fill="${ink}"/>`;
  if (style === 2) return `<path d="M12.6 19.4h6.8" stroke="${ink}" stroke-width="1.3" stroke-linecap="round"/>`;
  return '';
}

/** Hand-drawn category accessories. Colors come from the entity palette. */
function accessory(category, palette) {
  const {light, deep} = palette;
  switch (category) {
    case 'dex':
      return `<circle cx="6.4" cy="12" r="3" fill="${deep}"/><circle cx="6.4" cy="12" r="1.9" fill="${light}"/><circle cx="25.6" cy="12" r="3" fill="${deep}"/><circle cx="25.6" cy="12" r="1.9" fill="${light}"/>`;
    case 'lending':
      return `<ellipse cx="16" cy="5.4" rx="5" ry="1.6" fill="${light}"/><path d="M11 5.4h10v2.1H11z" fill="${deep}" opacity=".35"/><ellipse cx="16" cy="7.5" rx="5" ry="1.6" fill="${light}"/>`;
    case 'perps':
      return `<path d="M16 1.8l2.4 5.4h-4.8z" fill="${light}"/><path d="M9 12.2c.2-4.2 3-7.2 7-7.2s6.8 3 7 7.2" fill="none" stroke="${deep}" stroke-width="1.5" stroke-linecap="round"/>`;
    case 'bridge':
      return `<path d="M7.2 16.2a8.8 8.8 0 0 1 17.6 0" fill="none" stroke="${light}" stroke-width="2.1" stroke-linecap="round"/><circle cx="16" cy="7.6" r="1.45" fill="${light}"/>`;
    case 'vault':
      return `<path d="M12.2 8.2V6.6a3.8 3.8 0 0 1 7.6 0v1.6" fill="none" stroke="${deep}" stroke-width="1.7" stroke-linecap="round"/><rect x="11.2" y="8" width="9.6" height="5.6" rx="1.5" fill="${light}"/>`;
    case 'staking':
      return `<path d="M16 8.4c-2.2-3.4-6-2.2-4.1.5 1.1 1.5 4.1 1.1 4.1 1.1s3-.4 4.1-1.1c1.9-2.7-1.9-3.9-4.1-.5z" fill="${light}"/>`;
    case 'rwa':
      return `<g transform="rotate(14 24 7)"><rect x="19.6" y="4.2" width="8.2" height="5.2" rx="1.1" fill="${light}"/><path d="M21 6.8h5.2" stroke="${deep}" stroke-width="1" stroke-linecap="round"/></g>`;
    case 'whale':
      return `<circle cx="10.2" cy="6" r="1.45" fill="${light}"/><circle cx="16" cy="3.5" r="1.8" fill="${light}"/><circle cx="21.8" cy="6" r="1.45" fill="${light}"/>`;
    case 'bot':
      return `<path d="M16 7.4V3.2" stroke="${deep}" stroke-width="1.6" stroke-linecap="round"/><circle cx="16" cy="2.5" r="1.55" fill="${light}"/>`;
    case 'lp':
      return `<path d="M16 2.8c2.2 2.5 3.2 3.8 3.2 5a3.2 3.2 0 1 1-6.4 0c0-1.2 1-2.5 3.2-5z" fill="${light}"/>`;
    case 'exchange':
      return `<rect x="7.2" y="13.4" width="17.6" height="3" rx="1.4" fill="${deep}"/>`;
    case 'trader':
      return `<path d="M24.2 5.6l.7 1.6 1.7.2-1.3 1.1.4 1.7-1.5-.9-1.5.9.4-1.7-1.3-1.1 1.7-.2z" fill="${light}"/>`;
    case 'retail':
      return `<circle cx="8.4" cy="18.2" r="1.25" fill="#fff" opacity=".5"/><circle cx="23.6" cy="18.2" r="1.25" fill="#fff" opacity=".5"/>`;
    case 'contract':
      return `<path d="M16 3.2l4.4 2.5v5L16 13.2l-4.4-2.5v-5z" fill="${light}"/>`;
    default:
      return `<circle cx="16" cy="5.2" r="1.55" fill="${light}"/>`;
  }
}

export function avatarSvg(seed, {category = 'other', size = 22} = {}) {
  const px = box(size);
  const hash = hashSeed(seed || category);
  const palette = PALETTES[hash % PALETTES.length];
  const eyeStyle = (hash >>> 8) % 4;
  const mouthStyle = (hash >>> 16) % 4;
  const shift = ((hash >>> 21) % 3) - 1;
  const id = `tk${hash.toString(16)}`;
  const kind = String(category || 'other');
  const face = kind === 'whale' ? 13.1 : 12.5;
  return `<svg class="exp-avatar" data-kind="${esc(kind)}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="${px}" height="${px}" aria-hidden="true" focusable="false"><defs><linearGradient id="${id}" x1="8" y1="3" x2="24" y2="30" gradientUnits="userSpaceOnUse"><stop stop-color="${palette.light}"/><stop offset="1" stop-color="${palette.main}"/></linearGradient></defs><circle cx="16" cy="16" r="15.2" fill="${palette.deep}"/><circle cx="16" cy="16.6" r="${face}" fill="url(#${id})"/><ellipse cx="12.2" cy="10.2" rx="4.6" ry="2.5" fill="#fff" opacity=".34"/><ellipse cx="16" cy="22.4" rx="7.2" ry="2.6" fill="${palette.deep}" opacity=".16"/>${eyes(eyeStyle, palette.deep, shift * 0.35)}${mouth(mouthStyle, palette.deep)}${((hash >>> 23) % 3) ? `<ellipse cx="8.6" cy="18.4" rx="1.5" ry=".9" fill="${palette.blush}" opacity=".8"/><ellipse cx="23.4" cy="18.4" rx="1.5" ry=".9" fill="${palette.blush}" opacity=".8"/>` : ''}${accessory(kind, palette)}</svg>`;
}

function logoImg(src, size, seed, category) {
  return `<img class="em-logo" src="${esc(src)}" width="${size}" height="${size}" alt="" loading="lazy" decoding="async" data-tk-avatar="${esc(seed)}" data-tk-kind="${esc(category)}">`;
}

function withFallback(html, seed, category) {
  if (typeof html !== 'string' || !html.includes('<img')) return '';
  return html.replace('<img ', `<img data-tk-avatar="${esc(seed)}" data-tk-kind="${esc(category)}" `);
}

export function protocolMark(row, size = 22, branded = '') {
  const px = box(size);
  const seed = String(row?.id || row?.entity_id || row?.name || 'protocol');
  const category = protocolCategory(row);
  const src = safeLogoPath(row?.icon);
  if (src) return logoImg(src, px, seed, category);
  const kept = withFallback(branded, seed, category);
  if (kept) return kept;
  return avatarSvg(seed, {category, size: px});
}

export function traderMark(row, size = 22) {
  const px = box(size);
  const seed = String(row?.wallet || row?.id || row?.entity_id || 'trader');
  return avatarSvg(seed, {category: traderCategory(row), size: px});
}

export function walletMark(row, size = 20) {
  const px = box(size);
  const seed = String(row?.address || row?.id || row?.entity_id || 'wallet');
  return avatarSvg(seed, {category: walletCategory(row), size: px});
}

export function installAvatarFallback(root) {
  if (!root || root.dataset?.tkAvatarFallback) return;
  if (root.dataset) root.dataset.tkAvatarFallback = '1';
  root.addEventListener('error', event => {
    const img = event.target;
    if (!img || typeof img.getAttribute !== 'function') return;
    const seed = img.getAttribute('data-tk-avatar');
    if (!seed) return;
    const size = Number(img.getAttribute('width')) || 22;
    const html = avatarSvg(seed, {category: img.getAttribute('data-tk-kind') || 'other', size});
    const holder = img.ownerDocument.createElement('span');
    holder.innerHTML = html;
    const node = holder.firstElementChild;
    if (node) img.replaceWith(node);
  }, true);
}
