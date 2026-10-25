"""Build server/evm_catalog.json: the reviewed Arbitrum One / Robinhood Chain market selection.

Exact identities only (network + lowercase 0x address from the em-charts market table). Selection:
every Robinhood Chain stock token, the 25 most-traded other Robinhood Chain tokens and the 60
most-traded Arbitrum One tokens by recorded USD turnover; never the native placeholder, a
quality-flagged market or one without a recorded trade in the last 14 days.
usage: build_evm_catalog.py OUT.json [--deep http://127.0.0.1:8895]
"""
import json
import sys
import time
import urllib.request

NATIVE = '0x0000000000000000000000000000000000000000'
LIMITS = {'robinhood': 25, 'arbitrum': 60}
NAMES = {'arbitrum': 'Arbitrum One', 'robinhood': 'Robinhood Chain'}


def display_name(t, stock):
    """'NVIDIA • Robinhood Token' -> 'NVIDIA · Robinhood stock token'; the network badge carries the chain elsewhere."""
    name = (t.get('name') or t['symbol']).strip()
    if stock:
        name = name.replace(' • Robinhood Token', '').replace(' Common Stock', '').replace(' common stock', '').strip(' ,') + ' · Robinhood stock token'
    return name[:96]


def rows(base, network):
    with urllib.request.urlopen(base + '/api/public/markets?network=' + network, timeout=20) as r:
        return [t for t in json.load(r)['tokens'] if t.get('network') == network]


def main(out, base):
    now = int(time.time())
    selected = []
    for network in ('robinhood', 'arbitrum'):
        live = [t for t in rows(base, network) if t['address'] != NATIVE and (t.get('quality') or 'ok') == 'ok'
                and t.get('updated_at') and now - t['updated_at'] < 14 * 86400 and t.get('symbol')]
        stocks = [t for t in live if network == 'robinhood' and t.get('kind') == 'stock']
        others = sorted((t for t in live if t not in stocks), key=lambda t: -(t.get('usdVolumeAllTime') or 0))[:LIMITS[network]]
        for t in sorted(stocks, key=lambda t: t['symbol']) + others:
            stock = t in stocks
            selected.append({'id': '%s:%s' % (network, t['address']), 'network': network, 'mint': t['address'], 'address': t['address'],
                             'symbol': t['symbol'][:20], 'name': display_name(t, stock), 'kind': t.get('kind') or 'token',
                             'category': 'RWA' if stock else NAMES[network], 'coingecko': None, 'icon': None,
                             'decimals': t.get('decimals'), 'uiMultiplier': t.get('uiMultiplier'),
                             'selection_basis': 'em-charts %s market table: %s' % (NAMES[network], 'stock token' if stock else 'top recorded USD turnover'),
                             'usdVolumeAllTime': t.get('usdVolumeAllTime'), 'selected_at': time.strftime('%Y-%m-%d', time.gmtime(now)),
                             'reviewed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))})
    doc = {'version': 1, 'policy': 'em_pool_reference_v2', 'generatedAt': now,
           'selection': __doc__.split('\n\n')[1].split('usage:')[0].strip(), 'markets': selected}
    with open(out, 'w') as f:
        json.dump(doc, f, indent=1)
    print(json.dumps({k: sum(t['network'] == k for t in selected) for k in NAMES}), len(selected))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[sys.argv.index('--deep') + 1] if '--deep' in sys.argv else 'http://127.0.0.1:8895')
