"""First-class Arbitrum / Robinhood networks in the private read bridge (solana-mingle engine).

usage: apply_engine_patch.py <engine_dir> [--check]
Adds identity.py and evm_history.py; a `network` parameter on /resolve, /candles, /trades and
/token/*, dispatched through READERS; EVM hot stores in commit_notifications. Solana requests
without `network` take exactly the existing code path.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from patchlib import apply  # noqa: E402

EVM_QUERY = '''
def evm_query(path,evm,one):
    """The same reader contract on an EVM store. Pools use chart_history unchanged."""
    if path=='/token/catalog':return evm.catalog()
    if path.startswith('/token/'):
        token=one('token')
        if not valid_address(evm.network,token):raise ValueError('Invalid token address')
        if path=='/token/resolve':return evm.resolve_token(token)
        limit=int(one('limit','300' if path=='/token/candles' else '50'))
        if path=='/token/trades':return evm.trades(token,limit)
        if one('history','executions') not in {'full','executions'}:raise ValueError('Invalid history mode')
        return evm.history('token:'+token,int(one('interval','60')),limit,int(one('before')) if one('before') else None,one('gapPolicy','recorded'))
    if path=='/resolve':return evm.resolve(token=one('token'),pool=one('pool'),quote_mint=one('quoteMint'))
    if path not in {'/candles','/trades'}:raise ValueError('Unsupported network request')
    pool=one('pool')
    if not valid_pool(evm.network,pool):raise ValueError('Invalid pool')
    limit=int(one('limit','300' if path=='/candles' else '50'))
    if not 1<=limit<=(600 if path=='/candles' else 100):raise ValueError('Invalid page size')
    if path=='/trades':return evm.pool_trades(pool,limit)
    period=int(one('interval','60'))
    if not 1<=period<=604800:raise ValueError('Invalid interval')
    return chart_history(evm,pool,period,limit,int(one('before')) if one('before') else None,one('gapPolicy','recorded'))

def query(path,q):'''

PATCHES = {
    'ticking_bridge.py': [
        ('from candle_quality import attach_quality\n',
         'from candle_quality import attach_quality\nfrom identity import EVM,network_of,valid_address,valid_pool\nfrom evm_history import EvmHistory\n'),
        ('hyperliquid=HyperliquidPublic(ROOT)\n',
         "hyperliquid=HyperliquidPublic(ROOT)\nREADERS={'solana':reader,**{n:EvmHistory(n) for n in sorted(EVM)}}\n"),
        ('\ndef query(path,q):', EVM_QUERY),
        ("    if path not in allowed or not set(q)<=allowed[path] or any(len(v)!=1 for v in q.values()):raise ValueError('Invalid engine request')\n"
         "    one=lambda k,d=None:q.get(k,[d])[0]\n",
         "    for networked in ('/resolve','/candles','/trades','/token/catalog','/token/resolve','/token/candles','/token/trades'):allowed[networked]=allowed[networked]|{'network'}\n"
         "    if path not in allowed or not set(q)<=allowed[path] or any(len(v)!=1 for v in q.values()):raise ValueError('Invalid engine request')\n"
         "    one=lambda k,d=None:q.get(k,[d])[0]\n"
         "    network=network_of(one('network'))\n"
         "    if network in EVM:return evm_query(path,READERS[network],one)\n"),
        # EVM reads get their own lanes and never queue behind (or occupy) the Solana process pool
        ("pool_identity_reads=threading.BoundedSemaphore(1)\n",
         "pool_identity_reads=threading.BoundedSemaphore(1)\nevm_reads=threading.BoundedSemaphore(2)\n"),
        ("    lane=identity_reads if path in",
         "    evm=params.get('network',['solana'])[0] in EVM\n    lane=evm_reads if evm else identity_reads if path in"),
        ("if historical and candle and compute else encode_payload",
         "if historical and candle and compute and not evm else encode_payload"),
    ],
    'commit_notifications.py': [
        ("    'pool_archive': 'reviewed-pool-prices/prices.sqlite3',\n}",
         "    'pool_archive': 'reviewed-pool-prices/prices.sqlite3',\n"
         "    # EVM hot suffixes (em-charts live tail); read-only, outside this data root.\n"
         "    **{'evm_'+n: str(Path(os.environ.get('TICKING_EVM_HOT_ROOT', '/srv/skew/em-charts/hot')) / n / 'history.sqlite3') for n in ('arbitrum', 'robinhood')},\n}"),
        ("def dependencies(path, query):\n",
         "def dependencies(path, query):\n"
         "    network = query.get('network', [None])[0] if isinstance(query.get('network'), list) else query.get('network')\n"
         "    if network in ('arbitrum', 'robinhood'): return frozenset(('evm_' + network, 'config'))\n"),
    ],
}

if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    apply(sys.argv[1], PATCHES, [(HERE / 'engine/identity.py', 'identity.py'), (HERE / 'engine/evm_history.py', 'evm_history.py')],
          check='--check' in sys.argv)
