"""Read-only cold chart data contract. Does not acquire the collector journal.

Mint resolution exposes candidate pools and picks a stored base-mint chart;
it never silently merges incompatible quotes or changes live-core admission.
"""
import json,sqlite3
from contextlib import contextmanager
from pathlib import Path
from solana_history import SolanaHistory
from server import valid_mint

class ColdHistoryReader(SolanaHistory):
    pool_bar_count_sql='(SELECT count(*) FROM bars b WHERE b.pool=p.pool AND b.interval=60)'
    def __init__(self,path):self.path=Path(path)/'history.sqlite3'
    @contextmanager
    def connect(self):
        c=sqlite3.connect(f'file:{self.path}?mode=ro',uri=True,timeout=5)
        try:
            c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
            with c:yield c
        finally:c.close()
    def close(self):pass
    def catalog(self,after='',limit=100):
        if after and not valid_mint(after) or not isinstance(limit,int) or not 1<=limit<=100:raise ValueError('Invalid bounded Solana catalog request')
        with self.connect() as c:
            rows=c.execute('''SELECT mint FROM (SELECT base mint FROM pools WHERE base>? UNION SELECT quote FROM pools WHERE quote>? UNION SELECT mint FROM mints WHERE mint>?)
              ORDER BY mint LIMIT ?''',(after,after,after,limit+1)).fetchall()
        return {'network':'solana','assets':[{'mint':r[0]} for r in rows[:limit]],'hasMore':len(rows)>limit,
                'nextAfter':rows[limit-1][0] if len(rows)>limit else None,'coverage':'retained_finalized_pool_and_mint_identities','entireSolanaComplete':False,'liveReady':False}
    def resolve(self,token=None,pool=None,quote_mint=None):
        if bool(token)==bool(pool) or not valid_mint(token or pool) or quote_mint is not None and not valid_mint(quote_mint):raise ValueError('Supply one Solana mint or pool')
        with self.connect() as c:
            c.execute('BEGIN')
            clauses='p.pool=?' if pool else '(p.base=? OR p.quote=?)';params=[pool] if pool else [token,token]
            if quote_mint:
                if pool:clauses+=' AND p.quote=?';params.append(quote_mint)
                else:clauses+=' AND CASE WHEN p.base=? THEN p.quote ELSE p.base END=?';params.extend([token,quote_mint])
            rows=c.execute('SELECT p.pool,p.base,p.quote,p.program,'+self.pool_bar_count_sql+
              ' bars FROM pools p WHERE '+clauses+' ORDER BY bars DESC,p.pool LIMIT 101',params).fetchall()
            m=c.execute('SELECT config FROM mints WHERE mint=?',(token,)).fetchone() if token else None
            claim=c.execute('SELECT issuer,name,symbol,source FROM mint_claims WHERE mint=? ORDER BY issuer LIMIT 5',(token,)).fetchall() if token else []
        candidates=[{'pool':r['pool'],'baseMint':token if token else r['base'],'quoteMint':r['base'] if token==r['quote'] else r['quote'],'program':r['program'],'minuteCandles':r['bars'],'inverted':bool(token and token==r['quote'])} for r in rows[:100]]
        preferred=candidates[0] if candidates and candidates[0]['minuteCandles'] else None
        return {'network':'solana','mode':'historical_finalized_rpc','ready':bool(preferred),'liveReady':False,
                'state':'history_available' if preferred else 'awaiting_recorded_executions' if candidates else 'no_verified_base_pool',
                'preferredPool':preferred,'pools':candidates,'hasMorePools':len(rows)>100,
                'mint':token,'mintAccount':json.loads(m[0]) if m else None,'rwaClaims':[dict(r) for r in claim],
                'entireSolanaComplete':False,'tokenAggregationApplied':False}

def handle(reader,path,query,headers):
    """Same project/origin entitlement checks as live SDK; no provider key.
    The host can mount this handler without modifying the live engine or UI.
    """
    import product_api
    claims=product_api.authorize(headers);product_api.admit_read(claims['project'])
    allowed={'/sdk/archive/resolve':{'token','pool','quoteMint'},'/sdk/archive/candles':{'pool','interval','limit','before'},'/sdk/archive/assets':{'after','limit'}}
    if path not in allowed or not set(query)<=allowed[path]:raise ValueError('Unknown historical chart argument')
    def one(name,default=None):
        value=query.get(name,[default])
        if len(value)!=1:raise ValueError('Repeated history argument')
        return value[0]
    if path=='/sdk/archive/assets':return reader.catalog(after=one('after',''),limit=int(one('limit','100')))
    if path=='/sdk/archive/resolve':return reader.resolve(token=one('token'),pool=one('pool'),quote_mint=one('quoteMint'))
    if path=='/sdk/archive/candles':
        limit=int(one('limit','300'));before=int(one('before')) if one('before') is not None else None
        if not 1<=limit<=600 or before is not None and not 0<before<=2**53-1:raise ValueError('Invalid bounded candle page')
        return reader.history(one('pool'),interval=int(one('interval','1'))*60,limit=limit,before=before)
    raise ValueError('Unknown historical chart route')
