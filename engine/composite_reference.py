"""Reproducible asset reference OHLC; never an execution or volume ledger.

The median of venue OHLC coordinates is an estimate of a reference candle,
not the exact extrema of a synchronized tick index. Inputs and exclusions are
returned so a client can inspect the original venue candles independently.
"""
import math
from statistics import median
PRICES=('open','high','low','close')
POLICY='median_venue_ohlc_v1'

def composite_candle(candidates,stamp):
    rows=[dict(r) for r in candidates if all(isinstance(r.get(k),(int,float)) and math.isfinite(r[k]) and r[k]>0 for k in PRICES)
          and r['low']<=min(r['open'],r['close'])<=max(r['open'],r['close'])<=r['high']]
    if not rows:return None
    # Distinct feeds from the same venue are not independent confirmations.
    rows=list({r['provider']:r for r in rows}.values())
    spot=[r for r in rows if r.get('instrumentType','spot')!='perpetual']
    rows=spot or rows
    anchor=median(r['close'] for r in rows)
    if len(rows)==2 and max(r['close'] for r in rows)/min(r['close'] for r in rows)>1.03:
        anchor=next((r['close'] for r in rows if r['provider']=='binance_spot'),rows[0]['close'])
    included=[];excluded=[]
    for r in rows:
        if abs(r['close']/anchor-1)>.015:
            excluded.append({**r,'reason':'close_disagreement'});continue
        others=[x for x in rows if x is not r and abs(x['close']/anchor-1)<=.015]
        # Quarantine an isolated extreme only with a same-minute independent
        # narrow-range witness. Never clip prices in the original venue store.
        if others:
            other_range=median((x['high']-x['low'])/x['close'] for x in others)
            own_range=(r['high']-r['low'])/r['close']
            extreme=(r['high']>max(x['high'] for x in others)*1.0015 or r['low']<min(x['low'] for x in others)*.9985)
            if extreme and own_range>max(.003,5*other_range):
                excluded.append({**r,'reason':'uncorroborated_range'});continue
        included.append(r)
    if not included:return None
    price={k:median(r[k] for r in included) for k in PRICES}
    return dict(time=stamp,**price,volume=0,baseVolume=0,quoteVolume=0,count=0,
        sourceTime=stamp,synthetic=False,referenceOnly=True,estimated=True,
        estimateMethod=POLICY,quoteCurrency='USDT',quoteCurrencies=['USDT'],
        sources=[r['provider'] for r in included],components=included,excludedComponents=excluded,
        referenceSamples=len(included),referenceBasis='spot' if spot else 'perpetual_fallback',
        sourceReceivedAt=max(r.get('received',0) for r in included)*1000)
