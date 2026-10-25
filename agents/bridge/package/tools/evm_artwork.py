"""Source bundle for Arbitrum One / Robinhood Chain token artwork (run on the control Mac).

usage: pwvenv/bin/python evm_artwork.py <evm_catalog.json> <out_dir>
Writes <out_dir>/sources.json and rendered 256x256 PNG tiles. qa/refresh-token-icons.py --networks then
normalises every source into the content-addressed <network>-<address>-<sha16>.webp icon store.

  Robinhood Chain stock tokens   Simple Icons (CC0-1.0, pinned) brand mark on its brand colour, for the
                                  reviewed company list below only; otherwise a Ticking monogram tile.
  Arbitrum One tokens            exact-address logoURI from the TrustWallet, Uniswap, Arbitrum and CoinGecko
                                  token lists (chainId 42161), in that order; otherwise a monogram.
  Robinhood Chain anchors        canonical asset artwork for em-charts quote anchors (WETH, USDC, ...).
Artwork never establishes identity: the catalog's network + address does. Brand marks are trademarks of
their owners and identify the underlying company only.
"""
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SIMPLE_ICONS = '16.35.0'
SI_BASE = 'https://cdn.jsdelivr.net/npm/simple-icons@%s/' % SIMPLE_ICONS
# Reviewed symbol -> (Simple Icons slug, note). Exact company matches only; renamed or ambiguous brands
# (Strategy, Circle Internet Group, HP Enterprise, Alibaba Group) deliberately keep a monogram.
BRANDS = {
    'AAPL': ('apple', None), 'AMD': ('amd', None), 'AVGO': ('broadcom', None), 'BA': ('boeing', None),
    'BB': ('blackberry', None), 'COIN': ('coinbase', None), 'CSCO': ('cisco', None), 'DDOG': ('datadog', None),
    'DELL': ('dell', None), 'DOCN': ('digitalocean', None), 'F': ('ford', None), 'FIG': ('figma', None),
    'FTNT': ('fortinet', None), 'GE': ('generalelectric', None), 'GOOGL': ('google', 'Google brand mark for Alphabet Inc.'),
    'INTC': ('intel', None), 'INTU': ('intuit', None), 'MDB': ('mongodb', None), 'META': ('meta', None),
    'NET': ('cloudflare', None), 'NFLX': ('netflix', None), 'NU': ('nubank', 'Nubank brand mark for Nu Holdings'),
    'NVDA': ('nvidia', None), 'PANW': ('paloaltonetworks', None), 'PATH': ('uipath', None), 'PLTR': ('palantir', None),
    'QBTS': ('dwavesystems', 'D-Wave brand mark for D-Wave Quantum Inc.'), 'QCOM': ('qualcomm', None),
    'RBLX': ('roblox', None), 'RDDT': ('reddit', None), 'SHOP': ('shopify', None), 'SNAP': ('snapchat', 'Snapchat brand mark for Snap Inc.'),
    'SNOW': ('snowflake', None), 'SPCX': ('spacex', None), 'TEAM': ('atlassian', None), 'TSLA': ('tesla', None),
    'TTWO': ('taketwointeractivesoftware', None), 'UPS': ('ups', None), 'ZM': ('zoom', None), 'HOOD': ('robinhood', None),
}
LISTS = [('trustwallet', 'https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/arbitrum/tokenlist.json'),
         ('uniswap', 'https://tokens.uniswap.org'),
         ('arbitrum', 'https://tokenlist.arbitrum.io/ArbTokenLists/arbed_uniswap_labs.json'),
         ('coingecko', 'https://tokens.coingecko.com/arbitrum-one/all.json')]
CANONICAL = {'WETH', 'USDC', 'USDT', 'WBTC', 'DAI'}
LOGO_HOSTS = ('assets-cdn.trustwallet.com', 'raw.githubusercontent.com', 'assets.coingecko.com', 'coin-images.coingecko.com')
MONO = [('#ecebfd', '#5658e8'), ('#e7f6f1', '#17856d'), ('#fdecef', '#c76678'), ('#eef4fd', '#3b6fd6'), ('#fdf4e6', '#b7791f')]
UA = {'User-Agent': 'ticking-artwork/1 (+https://ticking.dev)'}


def get(url, limit=16_000_000):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError('too large')
    return data


def ink(hexcolor):
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lum = lambda c: c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    return '#111827' if .2126 * lum(r) + .7152 * lum(g) + .0722 * lum(b) > .45 else '#ffffff'


def brand_tile(svg, hexcolor):
    path = re.search(r'<path d="([^"]+)"', svg.decode()).group(1)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">'
            '<circle cx="128" cy="128" r="128" fill="#%s"/><g transform="translate(58 58) scale(5.8333)"><path fill="%s" d="%s"/></g></svg>'
            % (hexcolor, ink(hexcolor), path))


def monogram_tile(symbol, stock):
    sym = re.sub(r'[^A-Za-z0-9]', '', symbol or '?')[:4].upper() or '?'
    bg, fg = ('#1c2233', '#ffffff') if stock else MONO[sum(map(ord, sym)) % len(MONO)]
    size = 120 if len(sym) <= 2 else 96 if len(sym) == 3 else 76
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><circle cx="128" cy="128" r="128" fill="%s"/>'
            '<text x="128" y="132" text-anchor="middle" dominant-baseline="central" font-family="Space Grotesk" font-weight="600" '
            'font-size="%d" fill="%s" letter-spacing="-2">%s</text></svg>' % (bg, size, fg, sym))


def main(catalog_path, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    markets = json.loads(Path(catalog_path).read_text())['markets']
    data = json.loads(get(SI_BASE + 'data/simple-icons.json'))
    icons = {i['slug']: i for i in (data if isinstance(data, list) else data['icons'])}
    lists, canonical = {}, {}
    for name, url in LISTS:
        try:
            for t in json.loads(get(url)).get('tokens', []):
                a, logo = str(t.get('address', '')).lower(), str(t.get('logoURI', ''))
                host = urllib.parse.urlsplit(logo).hostname if logo.startswith('https://') else None
                if t.get('chainId') == 42161 and re.fullmatch(r'0x[0-9a-f]{40}', a) and host in LOGO_HOSTS:
                    lists.setdefault(a, []).append({'list': name, 'listUrl': url, 'url': logo, 'symbol': t.get('symbol')})
                    if name in ('trustwallet', 'uniswap') and str(t.get('symbol', '')).upper() in CANONICAL:
                        canonical.setdefault(str(t['symbol']).upper(), {'list': name, 'listUrl': url, 'url': logo, 'address': a})
        except Exception as error:  # a list outage only removes candidates; tiles still render
            print('list %s unavailable: %r' % (name, error), file=sys.stderr)
    sources, tiles = {}, []
    for t in markets:
        key, sym, stock = t['id'], t['symbol'], t.get('kind') == 'stock'
        base = {'network': t['network'], 'address': t['mint'], 'symbol': sym}
        brand = BRANDS.get(sym) if stock else None
        if brand and brand[0] in icons:
            svg = get(SI_BASE + 'icons/%s.svg' % brand[0], 100_000)
            meta = icons[brand[0]]
            tiles.append((key, brand_tile(svg, meta['hex'])))
            sources[key] = {**base, 'sourceType': 'Simple Icons brand tile', 'license': 'CC0-1.0', 'simpleIcons': SIMPLE_ICONS,
                            'slug': brand[0], 'brand': meta['title'], 'hex': meta['hex'], 'svgUrl': SI_BASE + 'icons/%s.svg' % brand[0],
                            'svgSha256': hashlib.sha256(svg).hexdigest(), 'note': brand[1] or 'Brand mark identifies the underlying company only.'}
            continue
        remote = lists.get(t['mint'], []) if t['network'] == 'arbitrum' else \
            [dict(canonical[sym.upper()], canonicalFor=sym.upper())] if t.get('kind') == 'anchor' and sym.upper() in canonical else []
        if remote:
            sources[key] = {**base, 'sourceType': 'Token list logo' if t['network'] == 'arbitrum' else 'Canonical asset artwork (em-charts quote anchor)',
                            'candidates': remote}
            continue
        tiles.append((key, monogram_tile(sym, stock)))
        sources[key] = {**base, 'sourceType': 'Ticking monogram', 'style': 'stock' if stock else 'token'}
    render(tiles, out, sources)
    doc = {'version': 1, 'builtAt': int(time.time()), 'simpleIcons': SIMPLE_ICONS, 'lists': [{'name': n, 'url': u} for n, u in LISTS],
           'policy': __doc__.split('\n\n')[1].strip(), 'sources': sources}
    (out / 'sources.json').write_text(json.dumps(doc, indent=1))
    kinds = {}
    for s in sources.values():
        kinds[s['sourceType']] = kinds.get(s['sourceType'], 0) + 1
    print(json.dumps(kinds))


def render(tiles, out, sources):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={'width': 256, 'height': 256}, device_scale_factor=1)
        page.set_content('<html><head><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600&display=block">'
                         '<style>html,body{margin:0;background:transparent}#t{width:256px;height:256px}</style></head><body><div id="t"></div>'
                         '<span style="font:600 20px \'Space Grotesk\'">AZ09</span></body></html>')
        page.evaluate('document.fonts.ready')
        page.wait_for_timeout(500)
        for key, svg in tiles:
            page.evaluate('s=>{document.getElementById("t").innerHTML=s}', svg)
            name = key.replace(':', '-') + '.png'
            page.locator('#t').screenshot(path=str(out / name), omit_background=True)
            sources[key]['file'] = name
            sources[key]['renderedSha256'] = hashlib.sha256((out / name).read_bytes()).hexdigest()
        browser.close()


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
