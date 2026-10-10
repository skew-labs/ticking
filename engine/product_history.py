"""Read-only, identity-bound keyset pages. No provider queries or wallet labels."""
import base64
import json
import sqlite3
import re
import time
from contextlib import closing

def cursor(scope, row):
    return base64.urlsafe_b64encode(json.dumps([scope,row['slot'],row['tx_order'],row['event_order'],row['identity']],separators=(',',':')).encode()).decode().rstrip('=')

def page(token_reader, token, limit=100, before=None, quote_mint=None):
    if not isinstance(token,str) or not re.fullmatch('[1-9A-HJ-NP-Za-km-z]{32,44}',token) or not 1<=limit<=100: raise ValueError('Invalid trade history selection')
    if quote_mint is not None and not re.fullmatch('[1-9A-HJ-NP-Za-km-z]{32,44}',quote_mint):raise ValueError('Invalid quote mint')
    scope=token+(':'+quote_mint if quote_mint else '')
    args=[token];where='mint=?'
    if quote_mint:where+=' AND quote_mint=?';args.append(quote_mint)
    if before:
        if len(before)>512: raise ValueError('Trade cursor too large')
        try:
            value=json.loads(base64.urlsafe_b64decode(before+'='*((-len(before))%4)))
            if not isinstance(value,list) or len(value)!=5 or value[0]!=scope or any(type(v)!=int or v<0 for v in value[1:4]) or not isinstance(value[4],str) or len(value[4])>200:raise ValueError()
        except Exception:raise ValueError('Invalid or cross-market trade cursor')
        where+=' AND (slot,tx_order,event_order,identity)<(?,?,?,?)';args+=value[1:]
    with closing(sqlite3.connect('file:'+str(token_reader.path)+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        rows=[dict(r) for r in c.execute('SELECT * FROM tape WHERE '+where+' ORDER BY slot DESC,tx_order DESC,event_order DESC,identity DESC LIMIT ?',(*args,limit+1))]
    more=len(rows)>limit;rows=rows[:limit]
    updated=max((r.get('time') or 0 for r in rows),default=0) or None
    return {'trades':list(reversed(rows)), 'identity':{'network':'solana','market':token,'baseMint':token,'quoteMint':quote_mint,'series':'pool_executions'},
            'hasMore':more,'nextBefore':cursor(scope,rows[-1]) if rows and more else None,
            'commitment':'finalized_prefix_processed_observation_suffix','canonicalFinalityVerified':False,
            'updated_at':updated,'stale':not updated or time.time()-updated>180,
            'complete':False,'live':False,'latencySla':None,'scope':'Retained physical pool execution legs; not routed user swaps, an order book or whole-chain history.'}
