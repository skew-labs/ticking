"""Price support across exact-mint pools, separate from the execution ledger.

Applies the routing boundaries used by StockMesh: identity, age, coherent inputs,
deduplication, and bounded candidates. This is provider-observed reference pricing,
not StockMesh native quoting, an executable swap route, or a trade.
"""
import math,statistics

def price_support(pairs,fetched_at,now,failed=False):
    base={'kind':'pool_price_reference','currency':'USD','execution':False,
          'fetched_at':fetched_at,'chainObservationTimeKnown':False,
          'policy':'pool_reference_v1','price':None,'pools':[]}
    if failed or not fetched_at or not 0<=now-fetched_at<=600:
        return {**base,'state':'stale'}
    unique={}
    for row in pairs[:20]:
        p,l=row.get('price'),row.get('liquidity')
        if (isinstance(p,(float,int)) and not isinstance(p,bool) and math.isfinite(p) and p>0
            and isinstance(l,(float,int)) and math.isfinite(l) and l>=1000
            and row.get('address')):unique.setdefault(row['address'],row)
    rows=list(unique.values())
    if not rows:return {**base,'state':'unavailable'}
    median=statistics.median(r['price'] for r in rows)
    eligible=[r for r in rows if abs(r['price']/median-1)<=.20]
    if not eligible:return {**base,'state':'disagreement'}
    # Select an actual attributed observation near the median, not a made-up fill.
    # Bound liquidity's influence so one reported deep pool cannot dominate.
    cap=statistics.median(r['liquidity'] for r in eligible)
    weighted=sorted((r['price'],r['address'],min(cap,r['liquidity']),r) for r in eligible)
    half=sum(x[2] for x in weighted)/2;weight=0
    for p,_,w,chosen in weighted:
        weight+=w
        if weight>=half:break
    return {**base,'state':'available','price':p,'selectedPool':chosen['address'],
            'pools':[r['address'] for r in eligible],'rejectedPools':len(rows)-len(eligible),
            'singlePool':len(eligible)==1,'source_url':chosen.get('url')}
