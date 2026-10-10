"""Address-selected, executed-only customer data product.

The recent projection is rebuilt from the retained normalized WAL. It is not a
full historical export and never treats a provider quote as an execution.
"""
from execution_projection import ExecutionTape
from server import valid_mint

INTERVALS = (1, 5, 15, 60, 240, 1440)
DEFAULT_QUOTES = ('EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
                  'So11111111111111111111111111111111111111112')

def select(identities, pool=None, token=None, quote=None):
    if bool(pool) == bool(token):
        raise ValueError('Select exactly one pool or token address')
    if not valid_mint(pool or token) or (quote and not valid_mint(quote)):
        raise ValueError('Invalid Solana address')
    pools = {a: i for a, i in identities.items() if valid_mint(a) and i.get('network') == 'solana'
             and i.get('series') == 'trade' and valid_mint(i.get('baseMint', '')) and valid_mint(i.get('quoteMint', ''))}
    if pool:
        if pool not in pools: raise ValueError('This pool has no verified execution adapter')
        identity = pools[pool]
        if quote and quote != identity['quoteMint']: raise ValueError('Pool quote identity mismatch')
        chosen = {pool: identity}
    else:
        available = {a: ({**i,'baseMint':token,'quoteMint':i['baseMint'],'quoteSymbol':i['baseMint'],'_inverse':True} if i['quoteMint']==token else i) for a,i in pools.items() if token in (i['baseMint'],i['quoteMint'])}
        quotes = sorted({i['quoteMint'] for i in available.values()})
        if not quotes: raise ValueError('No supported base-token pools are registered')
        # Mint-only requests choose one quote deterministically. No FX
        # conversion or combination of unlike quote volumes is implied.
        quote = quote or next((q for q in DEFAULT_QUOTES if q in quotes), quotes[0])
        chosen = {a: i for a, i in available.items() if i['quoteMint'] == quote}
        if not chosen: raise ValueError('No supported pools for this token and quote')
        identity = next(iter(chosen.values()))
    addresses = sorted(chosen)
    return {'mode': 'pool' if pool else 'token', 'pools': addresses,
            'invertedPools':[a for a in addresses if chosen[a].get('_inverse')],
            'identity': {'network': 'solana', 'venue': identity.get('venue', identity.get('program', 'native')) if pool else 'aggregate',
                         'pool': pool or '', 'market': pool or 'token:' + token + ':' + identity['quoteMint'],
                         'baseMint': identity['baseMint'], 'quoteMint': identity['quoteMint'],
                         'quoteSymbol': identity.get('quoteSymbol', identity['quoteMint']), 'series': 'trade'},
            'aggregation': 'single_pool' if pool else 'canonical_same_quote_pool_fills',
            'coverage': {'entireSolanaComplete': False, 'history': 'bounded_retained_execution_window',
                         'poolSet': 'registered_supported_token_pools', 'liquidityCombined': False,
                         'volume': 'venue_execution_volume_not_router_order_volume'}}

