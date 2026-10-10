"""Explicit exchange-instrument contract; no ticker-to-Solana-mint guesses."""
import json,re
from pathlib import Path
from fastapi import HTTPException
FILE=Path(__file__).with_name('global_markets.json')
CATALOG=json.loads(FILE.read_text())['markets'] if FILE.exists() else []
HL_FILE=Path(__file__).with_name('hyperliquid_markets.json')
HL_CATALOG=json.loads(HL_FILE.read_text())['markets'] if HL_FILE.exists() else []
CATALOG+=HL_CATALOG
INDEX={key:t for t in CATALOG for key in ((t['id'],) if t['source']=='hyperliquid_public' else (t['id'],t['market']))}
ASSETS_FILE=Path(__file__).with_name('global_assets.json')
ASSETS=json.loads(ASSETS_FILE.read_text()).get('assets',{}) if ASSETS_FILE.exists() else {}
_hl_snapshot=[]
def chosen(market):return INDEX.get(market)
def snapshot():
    global _hl_snapshot
    from .engine_data import get
    from .token_icons import exchange_icon_url
    rows=get('/reference/catalog')['markets']
    if HL_CATALOG:
        try:
            _hl_snapshot=get('/hyperliquid/catalog')['markets']
            rows+=_hl_snapshot
        except HTTPException as error:
            if error.status_code not in (502,503,504):raise
            # A venue read failure must not remove unrelated healthy markets.
            rows+=[{**t,'stale':True,'changeStale':True,'sourceUnavailable':True} for t in _hl_snapshot]
    return [{**t,'icon':exchange_icon_url(t['source'],t['market']) or t.get('icon')} for t in rows]
def detail(ident):
    t=next((r for r in snapshot() if r['id']==ident),None)
    if not t:raise HTTPException(404,'Unknown exchange instrument.')
    return {**t,'pools':{'items':[],'stale':False,'source':t['venue'],'updated_at':t['updated_at']},'revenue':{'status':'not_applicable'},'research':{'status':'pending'},'chain':{'status':'not_applicable'},'quality':{'scope':'Hyperliquid perpetual execution candles; not underlying stock or Solana volume.' if t['source']=='hyperliquid_public' else 'Multi-venue asset price reference. Market cap is global; Solana volume covers verified observed pools only.'}}
def candles(market,interval='1m',limit=300,before=None,gap_policy='recorded'):
    from .engine_data import get
    t=chosen(market)
    if not t:raise HTTPException(404,'Unknown exchange instrument.')
    match=re.fullmatch(r'([1-9][0-9]{0,4})(m|h|d|w)',interval)
    seconds=int(match[1])*{'m':60,'h':3600,'d':86400,'w':604800}[match[2]] if match else 0
    if not 60<=seconds<=604800 or type(limit)!=int or not 1<=limit<=600 or gap_policy not in {'recorded','carry'}:raise HTTPException(422,'Use 1m–1w and 1–600 bars.')
    if before is not None and (type(before)!=int or not 0<before<2**53):raise HTTPException(422,'Invalid before cursor.')
    raw=get('/hyperliquid/candles' if t['source']=='hyperliquid_public' else '/reference/candles',market=t['id'],interval=seconds,limit=limit,before=before//1000 if before else None,gapPolicy=gap_policy)
    return {**raw,'interval':interval,'candles':[{**b,'time':b['time']*1000,**({'sourceTime':b['sourceTime']*1000} if 'sourceTime' in b else {})} for b in raw['candles']],'nextBefore':raw['nextBefore']*1000 if raw['nextBefore'] else None}
def trades(ident,limit=100,before=None):
    t=chosen(ident)
    if not t:raise HTTPException(404,'Unknown exchange instrument.')
    if t['source']=='hyperliquid_public':
        from .engine_data import get
        return get('/hyperliquid/trades',market=t['id'],limit=limit,before=before)
    return {'identity':{'network':'exchange','venue':t['source'],'market':t['market']},'trades':[],'source':t['venue'],'live':False,'state':'candles_only','message':'This exchange feed currently provides OHLCV candles; individual executions are not collected.'}

def book(ident):
    from .engine_data import get
    t=chosen(ident)
    if not t or t['source']!='hyperliquid_public':raise HTTPException(422,'Choose a Hyperliquid market for L2 snapshots.')
    return get('/hyperliquid/book',market=t['id'])
