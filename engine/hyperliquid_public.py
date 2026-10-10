"""Bounded read-only public-venue data. No synthetic trades or quote conversion."""
import json, sqlite3, time
from contextlib import contextmanager
from pathlib import Path

PERIODS={60:'1m',180:'3m',300:'5m',900:'15m',1800:'30m',2700:'45m',3600:'1h',7200:'2h',10800:'3h',14400:'4h',21600:'6h',28800:'8h',43200:'12h',86400:'1d',259200:'3d',604800:'1w'}

class HyperliquidPublic:
    def __init__(self,root): self.path=Path(root)/'data/hyperliquid-public/history.sqlite3'
    @contextmanager
    def connection(self):
        c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
        c.row_factory=sqlite3.Row; deadline=time.monotonic()+2
        c.set_progress_handler(lambda:time.monotonic()>deadline,10000)
        try:
            c.execute('PRAGMA query_only=ON');c.execute('BEGIN');yield c
        finally:c.close()
    def selected(self,c,ident):
        coin=ident[3:] if isinstance(ident,str) and ident.startswith('HL-') else ident
        row=c.execute('SELECT identity FROM markets WHERE coin=?',(coin,)).fetchone()
        if not row:raise ValueError('Unknown Hyperliquid instrument')
        return json.loads(row[0])
    def identity(self,t):
        return dict(network='exchange',venue='hyperliquid_public',market=t['market'],baseMint='',quoteMint=t['quote'],base=t['base'],quote=t['quote'],series='trade',aggregation='exchange_instrument_v1',historyPolicy='hyperliquid_retained_v1',instrumentType='perpetual')
    def snapshot(self):
        if not self.path.exists():return {'markets':[]}
        out=[];now=time.time()
        with self.connection() as c:
            for row in c.execute('SELECT identity FROM markets'):
                t=json.loads(row[0]);coin=t['market']
                last=c.execute("SELECT c,time,received FROM candles WHERE coin=? AND period='1m' ORDER BY time DESC LIMIT 1",(coin,)).fetchone()
                if not last:continue
                prev=c.execute("SELECT c FROM candles WHERE coin=? AND period='1m' AND time<=? ORDER BY time DESC LIMIT 1",(coin,(now-86400)*1000)).fetchone()
                volume=c.execute("SELECT SUM(v),MIN(time) FROM candles WHERE coin=? AND period='1h' AND time>=?",(coin,(now-86400)*1000)).fetchone()
                spark=c.execute("SELECT c FROM candles WHERE coin=? AND period='1h' ORDER BY time DESC LIMIT 48",(coin,)).fetchall()
                stale=now-last['received']/1000>90 or now-last['time']/1000>180
                out.append({**{k:v for k,v in t.items() if k!='context'},'price':last['c'],'change':(last['c']/prev[0]-1)*100 if prev else None,'changeSource':'Hyperliquid last traded close, 24h','changeStale':stale or not prev,'updated_at':last['received']/1000,'stale':stale,'volume':None,'volumeScope':'Not a Solana execution market','venueBaseVolume24h':volume[0],'cap':None,'capSource':None,'ranking_eligible':False,'ready':True,'state':'available','aggregation':'exchange','pool':None,'quoteMint':t['quote'],'sparkline':[r[0] for r in reversed(spark)],'sources':['Hyperliquid'],'priceBasis':'Venue execution candles; separate from oracle and mark prices','coverage':{'complete':False,'historyLimitPerInterval':5000}})
        return {'markets':out,'at':now}
    def history(self,ident,period=60,limit=600,before=None,gap='recorded'):
        if type(period)!=int or period not in PERIODS or type(limit)!=int or not 1<=limit<=600 or gap not in ('recorded','carry'):raise ValueError('Unsupported Hyperliquid interval or page')
        if before is not None and (type(before)!=int or not 0<before<2**53):raise ValueError('Invalid cursor')
        now=int(time.time());bound=(before if before is not None else now+period)*1000
        origin=0  # Hyperliquid native candles use epoch-anchored weeks.
        bucket=lambda t:(t-origin)//period*period+origin
        with self.connection() as c:
            t=self.selected(c,ident);coin=t['market'];p=PERIODS[period]
            native=c.execute('SELECT * FROM candles WHERE coin=? AND period=? AND time<? ORDER BY time DESC LIMIT ?',(coin,p,bound,limit+1)).fetchall()
            def bar(r,stamp=None):return dict(time=r['time']//1000 if stamp is None else stamp,open=r['o'],high=r['h'],low=r['l'],close=r['c'],volume=r['v'],baseVolume=r['v'],count=r['n'],quoteCurrency=t['quote'],sources=['hyperliquid_public'])
            values={r['time']//1000:bar(r) for r in native}
            # Replace a bucket only from a complete lower-resolution prefix.
            # Never add a native volume to its constituent bars a second time.
            lower=86400 if period>=86400 else 3600 if period>=3600 else 60
            if lower!=period:
                rows=c.execute('SELECT * FROM candles WHERE coin=? AND period=? AND time<? ORDER BY time DESC LIMIT ?',(coin,PERIODS[lower],bound,min(20000,(limit+2)*(period//lower)))).fetchall()
                groups={}
                for r in reversed(rows):groups.setdefault(bucket(r['time']//1000),[]).append(r)
                for edge,group in groups.items():
                    expected=(min(edge+period,now-now%lower+lower)-edge)//lower
                    if before is not None and edge+period>before:continue
                    if expected<=0 or len(group)!=expected or group[0]['time']//1000!=edge or any(b['time']-a['time']!=lower*1000 for a,b in zip(group,group[1:])):continue
                    values[edge]=dict(time=edge,open=group[0]['o'],high=max(r['h'] for r in group),low=min(r['l'] for r in group),close=group[-1]['c'],volume=sum(r['v'] for r in group),baseVolume=sum(r['v'] for r in group),count=sum(r['n'] for r in group),quoteCurrency=t['quote'],sources=['hyperliquid_public'])
            first=c.execute('SELECT MIN(time) FROM candles WHERE coin=? AND period=?',(coin,p)).fetchone()[0]
            if first is None:first=c.execute('SELECT MIN(time) FROM candles WHERE coin=? AND period=?',(coin,PERIODS[lower])).fetchone()[0]
            latest=c.execute("SELECT time,received FROM candles WHERE coin=? AND period='1m' ORDER BY time DESC LIMIT 1",(coin,)).fetchone()
            revision=c.execute('SELECT revision FROM state WHERE id=1').fetchone()[0]
            jobs=[dict(r) for r in c.execute('SELECT period,depth,rows,first,last,done FROM jobs WHERE coin=? AND depth>2',(coin,))]
        bars=[b for _,b in sorted(values.items()) if before is None or b['time']+period<=before][-limit:]
        observed=latest['received']/1000 if latest else 0
        return dict(mode='exchange_history',aggregation='exchange',identity=self.identity(t),market=t['id'],pool=None,mint=None,symbol=t['symbol'],name=t['name'],quote=t['quote'],quoteMint=t['quote'],currency=t['quote'],candles=bars,fillPolicy='recorded',carriedBars=0,revision=revision,cursor=revision,hasMore=bool(bars and first and first//1000<bars[0]['time']),nextBefore=bars[0]['time'] if bars else None,coverage={'start':first//1000 if first else None,'complete':False,'scope':'Hyperliquid provider candles. Public API history is limited to the most recent 5000 bars per interval. No cross-venue synthesis.','jobs':jobs},history={'start':first//1000 if first else None,'market':coin,'sourceLabel':'Hyperliquid','archiveQuote':t['quote'],'currentQuote':t['quote']},source='Hyperliquid',sources=['Hyperliquid'],venue='Hyperliquid',updated_at=observed,stale=not latest or now-observed>90 or now-latest['time']/1000>180,refresh_seconds=1,live=bool(latest and now-observed<90),observationAvailable=bool(bars),complete=False)
    def trades(self,ident,limit=100,before=None):
        if type(limit)!=int or not 1<=limit<=100:raise ValueError('Trade page bound')
        with self.connection() as c:
            t=self.selected(c,ident)
            bound=(2**53-1,2**53-1)
            if before is not None:
                if not isinstance(before,str) or len(before)>40:raise ValueError('Invalid trade cursor')
                parts=before.split(':')
                if len(parts)!=2 or not all(p.isdigit() and 0<=int(p)<2**53 for p in parts):raise ValueError('Invalid trade cursor')
                bound=tuple(map(int,parts))
            rows=c.execute('SELECT * FROM trades WHERE coin=? AND (time,tid)<(?,?) ORDER BY time DESC,tid DESC LIMIT ?',(t['market'],*bound,limit+1)).fetchall()
            more=len(rows)>limit;rows=rows[:limit]
            values=[dict(identity=f"hyperliquid:{r['coin']}:{r['time']}:{r['tid']}",time=r['time']/1000,tradeId=r['tid'],price=r['price'],base_volume=r['size'],quote_volume=r['size']*r['price'],side=r['side'],hash=r['hash'],receivedAt=r['received'],venue='Hyperliquid') for r in rows]
        return dict(identity=self.identity(t),trades=values,hasMore=more,nextBefore=f"{rows[-1]['time']}:{rows[-1]['tid']}" if rows and more else None,source='Hyperliquid public trades',live=bool(rows and time.time()-rows[0]['received']/1000<60),state='retained_trades',coverage={'complete':False,'reconnectTradeRecovery':'unverified'},volumeUnit=t['base'],quote=t['quote'])
    def book(self,ident):
        with self.connection() as c:
            t=self.selected(c,ident);r=c.execute('SELECT * FROM books WHERE coin=?',(t['market'],)).fetchone()
        return dict(identity=self.identity(t),book=json.loads(r['payload']) if r else None,receivedAt=r['received'] if r else None,stale=not r or time.time()-r['received']/1000>10,depth=20,source='Hyperliquid public L2 snapshots',complete=False)
