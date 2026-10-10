"""Bounded display windows over retained executions, with explicit carried closes.

Missing records are unknown coverage, not proof that no swap happened. Carried
bars never enter the execution ledger and have zero recorded volume/count.
Recovered historical fills replace them on the next snapshot.
"""
from chart_intervals import bucket_start,bucket_sql,WEEK
NATIVE=(60,180,300,900,1800,2700,3600,7200,10800,14400,21600,28800,43200,86400,WEEK)
STORED=(60,300,3600)

def aggregate(rows,period):
    groups={}
    for r in rows:
        bucket=bucket_start(r['time'],period)
        b=groups.get(bucket)
        if b is None:groups[bucket]=dict(time=bucket,open=r['price'],high=r['price'],low=r['price'],close=r['price'],baseVolume=r['base_volume'],volume=r['quote_volume'],count=1,**({'commitment':r['commitment']} if 'commitment' in r.keys() else {}))
        else:b.update(high=max(b['high'],r['price']),low=min(b['low'],r['price']),close=r['price'],baseVolume=b['baseVolume']+r['base_volume'],volume=b['volume']+r['quote_volume'],count=b['count']+1)
        if 'commitment' in r.keys() and r['commitment']!='finalized':groups[bucket]['commitment']=r['commitment']
    return [groups[k] for k in sorted(groups)]

def history(reader,pool,period,limit,before=None,policy='recorded'):
    if type(period)!=int or not 1<=period<=WEEK:raise ValueError('Unsupported candle period')
    if policy not in ('recorded','carry'):raise ValueError('Unknown gap policy')
    minute_rollup=period in NATIVE or hasattr(reader,'stored_intervals') and period>=60 and period%60==0
    if policy=='recorded' and minute_rollup:return reader.history(pool,period,limit,before)
    base=reader.history(pool,60,1);bound=before if before is not None else 2**62
    with reader.connect() as c:
        c.execute('BEGIN')
        if policy=='recorded':
            buckets=c.execute(f'SELECT DISTINCT {bucket_sql(period)} bucket FROM fills WHERE pool=? AND time<? ORDER BY bucket DESC LIMIT ?',(pool,bound,limit+1)).fetchall()
            lower=buckets[-1][0] if buckets else bound
            rows=c.execute('SELECT * FROM fills WHERE pool=? AND time>=? AND time<? ORDER BY slot,tx_order,event_order,identity LIMIT 20001',(pool,lower,bound)).fetchall()
            if len(rows)>20000:raise ValueError('Reduce custom-interval page size')
            bars=[b for b in aggregate(rows,period) if before is None or b['time']+period<=before];result=bars[-limit:]
            return {**base,'candles':result,'hasMore':len(bars)>limit,'nextBefore':result[0]['time'] if result else None,'fillPolicy':policy}
        if minute_rollup:
            base_period=max(i for i in getattr(reader,'stored_intervals',STORED) if period%i==0)
            head=c.execute(f'SELECT {bucket_sql(period)} FROM bars WHERE pool=? AND interval=? ORDER BY time DESC LIMIT 1',(pool,base_period)).fetchone()
            first=c.execute(f'SELECT {bucket_sql(period)} FROM bars WHERE pool=? AND interval=? ORDER BY time LIMIT 1',(pool,base_period)).fetchone()
        else:
            head=c.execute(f'SELECT {bucket_sql(period)} FROM fills WHERE pool=? ORDER BY time DESC LIMIT 1',(pool,)).fetchone()
            first=c.execute(f'SELECT {bucket_sql(period)} FROM fills WHERE pool=? ORDER BY time LIMIT 1',(pool,)).fetchone()
        if not head:return {**base,'candles':[],'hasMore':False,'nextBefore':None,'fillPolicy':policy,'carriedBars':0}
        end=head[0] if before is None else min(head[0],bucket_start(before,period)-period)
        start=max(first[0],end-(limit-1)*period)
        if start>end:return {**base,'candles':[],'hasMore':False,'nextBefore':None,'fillPolicy':policy,'carriedBars':0}
        if minute_rollup:
            actual=reader.minute_rows(c,pool,start,end+period) if hasattr(reader,'minute_rows') else c.execute('SELECT * FROM bars WHERE pool=? AND interval=? AND time>=? AND time<? ORDER BY time',(pool,base_period,start,end+period)).fetchall()
            groups={}
            for r in actual:
                t=bucket_start(r['time'],period);b=groups.get(t)
                if b is None:groups[t]=dict(time=t,open=r['open'],high=r['high'],low=r['low'],close=r['close'],baseVolume=r['base_volume'],volume=r['quote_volume'],count=r['count'],**({'commitment':r['commitment']} if 'commitment' in r.keys() else {}))
                else:b.update(high=max(b['high'],r['high']),low=min(b['low'],r['low']),close=r['close'],baseVolume=b['baseVolume']+r['base_volume'],volume=b['volume']+r['quote_volume'],count=b['count']+r['count'])
                if 'commitment' in r.keys() and r['commitment']!='finalized':groups[t]['commitment']=r['commitment']
            bars=[groups[t] for t in sorted(groups)]
            seed=c.execute(f'SELECT {bucket_sql(period)} time,close,bars.time minute FROM bars WHERE pool=? AND interval=? AND time<? ORDER BY bars.time DESC LIMIT 1',(pool,base_period,start)).fetchone()
            if seed and hasattr(reader,'minute_rows'):
                precise=reader.minute_rows(c,pool,seed['minute'],seed['minute']+60)
                if precise:seed=dict(time=seed['time'],close=precise[-1]['close'])
        else:
            actual=c.execute('SELECT * FROM fills WHERE pool=? AND time>=? AND time<? ORDER BY slot,tx_order,event_order,identity LIMIT 20001',(pool,start,end+period)).fetchall()
            if len(actual)>20000:raise ValueError('Reduce custom-interval page size')
            bars=aggregate(actual,period)
            prior=c.execute('SELECT time FROM fills WHERE pool=? AND time<? ORDER BY time DESC LIMIT 1',(pool,start)).fetchone()
            seed=None
            if prior:
                bucket=bucket_start(prior[0],period)
                seed=c.execute('SELECT ? time,price close FROM fills WHERE pool=? AND time>=? AND time<? ORDER BY slot DESC,tx_order DESC,event_order DESC,identity DESC LIMIT 1',(bucket,pool,bucket,bucket+period)).fetchone()
    indexed={b['time']:b for b in bars};result=[];previous=dict(seed) if seed else None
    for t in range(start,end+1,period):
        b=indexed.get(t)
        if b is not None:result.append({**b,'synthetic':False,'sourceTime':t});previous=b
        elif previous is not None:
            price=previous['close'];source=previous.get('sourceTime',previous['time'])
            result.append(dict(time=t,open=price,high=price,low=price,close=price,volume=0,baseVolume=0,count=0,synthetic=True,gapKind='unverified_no_record',sourceTime=source))
    return {**base,'candles':result,'hasMore':start>first[0],'nextBefore':start if result else None,'fillPolicy':policy,'carriedBars':sum(b['synthetic'] for b in result),'recordedThrough':head[0],
            'gapDisclosure':'Carried prior recorded close. Missing execution coverage is unverified; recorded volume and count remain zero.'}
