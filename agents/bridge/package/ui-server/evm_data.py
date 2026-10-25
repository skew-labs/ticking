"""Arbitrum One and Robinhood Chain markets on the shared public chart contract.

Rows are reviewed exact network/address identities (evm_catalog.json, pinned by network in
reviewed_markets.json). Candles, tapes and coverage come from the engine bridge's per-network
readers; prices are USD on-chain DEX references (em_pool_reference_v2). No ticker matching.
"""
import json
import re
import time
from pathlib import Path

from fastapi import HTTPException

NETWORKS = ('arbitrum', 'robinhood')
NAMES = {'arbitrum': 'Arbitrum One', 'robinhood': 'Robinhood Chain'}
EXPLORERS = {'arbitrum': 'https://arbiscan.io', 'robinhood': 'https://robinhoodchain.blockscout.com'}
POLICY = 'em_pool_reference_v2'
FILE = Path(__file__).with_name('evm_catalog.json')
CATALOG = json.loads(FILE.read_text())['markets'] if FILE.exists() else []
INDEX = {t['id']: t for t in CATALOG}
BY_ADDRESS = {(t['network'], t['mint']): t for t in CATALOG}
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
POOL = re.compile(r'0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})')
SCOPE = ('USD on-chain DEX reference: per minute the most established pool sets the price and pools within 5% are '
         'admitted; non-USD quotes are converted with a same-minute anchor rate. Archive completeness is not attested.')


def normal(value, pattern=ADDRESS):
    return value.lower() if isinstance(value, str) and pattern.fullmatch(value) else None


def chosen(market=None, network=None, mint=None):
    """An exact reviewed row: '<network>:<0x address>', or network + address. Never a ticker."""
    if market and ':' in market:
        net, _, address = market.partition(':')
        if network and network != net:
            return None
        return BY_ADDRESS.get((net, normal(address)))
    if network in NETWORKS and (mint or market):
        return BY_ADDRESS.get((network, normal(mint or market)))
    return None


def selection(market=None, network=None, mint=None):
    from .market_data import market_review
    t = chosen(market, network, mint)
    if not t or not market_review(t):
        raise HTTPException(404, 'Choose a reviewed %s market by its exact token address.' % NAMES.get(network, 'network'))
    return t


def interval_seconds(interval, token):
    match = re.fullmatch(r'([1-9][0-9]{0,4})(s|m|h|d|w)', interval.lower()) if isinstance(interval, str) else None
    seconds = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[match[2]] if match else 0
    if not 1 <= seconds <= 604800 or token and seconds % 60:
        raise HTTPException(422, 'Use 1s–1w for pools and whole minutes 1m–1w for token references.')
    return seconds


def resolve(t, aggregation='token', pool=None):
    from .engine_data import get
    if aggregation == 'token':
        return get('/token/resolve', network=t['network'], token=t['mint'])
    r = get('/resolve', network=t['network'], **({'pool': normal(pool, POOL)} if pool else {'token': t['mint']}))
    if pool and (not r['pools'] or r['pools'][0]['baseMint'] != t['mint']):
        raise HTTPException(422, 'This pool does not trade the selected token.')
    return r


def pinned(t, pool=None):
    r = resolve(t, 'pool', pool)
    p = r['pools'][0] if pool else r['preferredPool']
    if not p:
        raise HTTPException(409, {'code': 'history_pending', 'state': r['state'], 'pools': r['pools'], 'message': 'No recorded executions for this selection yet.'})
    return p


def candles(t, interval='1m', limit=300, before=None, gap_policy='recorded', aggregation='token', pool_pin=None, history='executions'):
    from .engine_data import get
    token = aggregation == 'token'
    if aggregation not in ('token', 'pool') or token and pool_pin:
        raise HTTPException(422, 'aggregation must be token or pool.')
    seconds = interval_seconds(interval, token)
    if type(limit) != int or not 1 <= limit <= 600:
        raise HTTPException(422, 'Use a page size of 1–600.')
    if before is not None and (type(before) != int or not 0 < before < 2 ** 53):
        raise HTTPException(422, 'before is an exclusive Unix-millisecond cursor.')
    if gap_policy not in {'recorded', 'carry'} or history not in {'full', 'executions'}:
        raise HTTPException(422, 'Use recorded or carry, and full or executions.')
    net, page = t['network'], dict(interval=seconds, limit=limit, before=before // 1000 if before else None, gapPolicy=gap_policy)
    if token:
        raw = get('/token/candles', network=net, token=t['mint'], **page)
        p = None
    else:
        p = pinned(t, pool_pin)
        raw = get('/candles', network=net, pool=p['pool'], **page)
    rows = [{**b, 'time': b['time'] * 1000, **({'sourceTime': b['sourceTime'] * 1000} if 'sourceTime' in b else {})} for b in raw['candles']]
    identity = {**raw['identity'], **({} if token else {'baseMint': t['mint'], 'quoteMint': 'USD', 'inverted': False, 'pool': p['pool']})}
    end = raw.get('observedThrough') or raw.get('recordedThrough') or (rows[-1]['time'] // 1000 if rows else None)
    url = EXPLORERS[net] + ('/token/' + t['mint'] if token else '/address/' + p['pool'][:42])
    return {'mode': 'executed_history', 'live': False, 'aggregation': aggregation, 'network': net,
            'observationAvailable': raw.get('observationAvailable', False), 'refresh_seconds': 2 if token else 1,
            'market': t['mint'] if token else p['pool'], 'pool': None if token else p['pool'], 'mint': t['mint'],
            'symbol': t['symbol'], 'name': t['name'], 'quoteMint': 'USD', 'quote': 'USD', 'identity': identity, 'interval': interval,
            'candles': rows, 'fillPolicy': raw.get('fillPolicy', gap_policy), 'carriedBars': raw.get('carriedBars', 0),
            'gapDisclosure': raw.get('gapDisclosure'), 'revision': raw['sequence'], 'cursor': raw['sequence'], 'hasMore': raw['hasMore'],
            'nextBefore': raw['nextBefore'] * 1000 if raw['nextBefore'] else None, 'coverage': {**raw['coverage'], 'scope': SCOPE},
            'history': None,  # Solana asset-history copy (archive/source markers) does not describe these markets
            'source': 'ticking execution store · ' + NAMES[net], 'updated_at': end,
            'stale': bool(raw.get('retainedSnapshot')) or not end or time.time() - end > 900,
            **({'retainedSnapshot': raw['retainedSnapshot']} if raw.get('retainedSnapshot') else {}), 'currency': 'USD',
            'venue': 'Reference across observed %s DEX pools' % NAMES[net] if token else 'DEX pool ' + p['pool'][:10] + '…',
            'source_url': url, 'complete': False}


def trades(t, pool=None, limit=50, aggregation='token'):
    from .engine_data import get
    if type(limit) != int or not 1 <= limit <= 100:
        raise HTTPException(422, 'Use a tape size of 1–100.')
    if aggregation == 'pool' or pool:
        p = pinned(t, pool)
        data = get('/trades', network=t['network'], pool=p['pool'], limit=limit)
    else:
        data = get('/token/trades', network=t['network'], token=t['mint'], limit=limit)
    return {**data, 'source': 'ticking execution store · ' + NAMES[t['network']], 'latencySla': None, 'explorer': EXPLORERS[t['network']]}


def pin_stream(query, kind):
    t = chosen(network=query['network'], mint=query['mint'])
    if query.get('aggregation') != 'pool':
        return dict(query)
    p = pinned(t, query.get('pool_pin') or query.get('pool'))
    result = {k: v for k, v in query.items() if k not in ('pool', 'pool_pin')}
    result['pool_pin' if kind == 'candles' else 'pool'] = p['pool']
    return result


def reference_rows():
    """Reviewed rows with engine-observed price, 24h change, 24h volume and a 7d hourly line."""
    from .engine_data import get
    from .market_data import market_review
    from . import token_icons
    now = int(time.time())
    rows = []
    for net in NETWORKS:
        try:
            observed = {r['address']: r for r in get('/token/catalog', network=net)['tokens']}
        except HTTPException:
            observed = {}
        for t in CATALOG:
            if t['network'] != net:
                continue
            verification = market_review(t)
            if not verification:
                continue
            r = observed.get(t['mint'], {})
            updated = r.get('updated_at')
            points = [x for x in (r.get('sparkline') or []) if isinstance(x, (int, float)) and x > 0][-168:]
            rows.append({**t, 'icon': token_icons.icon_url(t['mint'], net) or t.get('icon'), 'price': r.get('price'), 'change': r.get('change'),
                         'volume': r.get('volume'), 'cap': None, 'fdv': None, 'sparkline': points, 'updated_at': updated,
                         'stale': bool(r.get('stale', True)) or not updated,
                         'verification': verification, 'ranking_eligible': False, 'source': 'Ticking on-chain reference',
                         'source_url': EXPLORERS[net] + '/token/' + t['mint'], 'priceBasis': POLICY, 'priceUnit': 'USD',
                         'volumeScope': 'Observed %s DEX pools, USD turnover (24h)' % NAMES[net], 'volumeUpdatedAt': updated, 'volumeStale': bool(r.get('stale', True)),
                         'volumePoolCount': r.get('pools'), 'capSource': None, 'capUpdatedAt': None, 'capStale': True,
                         'changeSource': 'Ticking on-chain reference 24h', 'changeUpdatedAt': updated, 'changeStale': bool(r.get('stale', True)),
                         'sources': ['onchain_dex_swaps'], 'quality': r.get('quality') or 'ok', 'qualityNote': r.get('qualityNote')})
    return rows


def identity(ident):
    """Reviewed identity only: no engine, pool or price reads may delay the first candle request."""
    t = chosen(ident)
    from .market_data import market_review
    from . import token_icons
    verification = market_review(t) if t else None
    if not verification:
        raise HTTPException(404, 'This market has not been reviewed for public Markets.')
    row = {k: t.get(k) for k in ('id', 'symbol', 'name', 'mint', 'icon', 'category', 'network', 'market', 'marketType', 'source', 'venue', 'quote', 'rankOrder')}
    row.update(icon=token_icons.icon_url(t['mint'], t['network']) or t.get('icon'), verification=verification, price=None, change=None,
               volume=None, cap=None, updated_at=None, stale=True, ranking_eligible=False, priceBasis=POLICY, priceUnit='USD')
    return row


def detail(ident):
    t = chosen(ident)
    from .market_data import market_review
    if not t or not market_review(t):
        raise HTTPException(404, 'This market has not been reviewed for public Markets.')
    row = next((r for r in reference_rows() if r['id'] == t['id']), None)
    if row is None:
        raise HTTPException(404, 'This market has not been reviewed for public Markets.')
    try:
        resolved = resolve(t, 'pool')
    except HTTPException as error:
        if error.status_code < 500:
            raise
        resolved = {'ready': False, 'state': 'pool_details_delayed', 'preferredPool': None, 'pools': []}
    net = t['network']
    items = [{'address': p['pool'], 'venue': NAMES[net] + ' DEX pool', 'base': t['symbol'], 'quote': p.get('quote') or 'USD',
              'url': EXPLORERS[net] + '/address/' + p['pool'][:42], 'history_candles': None, 'executions': p.get('executions'),
              'usdVolume': p.get('usdVolume'), 'observed_through': p.get('observedThrough'), 'baseMint': t['mint'], 'quoteMint': 'USD',
              'quoteToken': p.get('quoteToken')} for p in resolved['pools'][:20]]
    row['pools'] = {'items': items, 'identities': [{'address': i['address'], 'venue': i['venue'], 'baseMint': t['mint'], 'quoteMint': 'USD', 'quote': i['quote']} for i in items],
                    'stale': False, 'source': 'ticking ' + NAMES[net] + ' pool identities · USD-converted prints', 'updated_at': int(time.time())}
    row['chart'] = {'ready': resolved['ready'], 'state': resolved['state'], 'preferredPool': resolved['preferredPool'], 'source': 'ticking execution store'}
    row['revenue'] = {'status': 'not_applicable'}
    row['research'] = {'status': 'pending'}
    row['chain'] = {'status': 'observed', 'network': net, 'chain': NAMES[net], 'source': NAMES[net] + ' explorer',
                    'source_url': EXPLORERS[net] + '/token/' + t['mint'], 'address': t['mint'], 'decimals': t.get('decimals')}
    row['quality'] = {'price': 'unavailable' if row['price'] is None else 'delayed' if row['stale'] else 'current',
                      'pools': 'current' if items else 'unavailable', 'research': 'pending', 'scope': SCOPE}
    return row
