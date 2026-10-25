"""Independent provisional pool quotes; never mutates exchange bars or trades."""
import hashlib,json,math,sqlite3,time
from pathlib import Path
from evm_composite import sql,ROOT,SECONDS,fx_rows,usd_factor
POOL=Path(__file__).with_name('fastfeed_exact_pools.json')
INPUT=Path('/srv/skew/em-realtime/arb-fastfeed/out/quotes.jsonl')
TOPIC='0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'

def price(info,data,quote_usd):
    h=data.removeprefix('0x')
    if len(h)!=320:return None
    sqrt=int(h[128:192],16)
    if not 0<sqrt<2**160:return None
    ratio=(sqrt/2**96)**2*10**(info['decimals0']-info['decimals1'])
    px=(ratio if info['base_is_token0'] else 1/ratio)*quote_usd
    return px if math.isfinite(px) and px>0 else None

def anchor(info,stamp):
    quote=info['quote'];fx=fx_rows(stamp//60*60-120,stamp//60*60+60)
    usdc=usd_factor(fx,stamp,'USDC',live=True);usdt=usd_factor(fx,stamp,'USDT',live=True)
    if quote in ('0xaf88d065e77c8cc2239327c5edb3a432268e5831','0xff970a61a04b1ca14834a43f5de4533ebddb5cc8'):return usdc[0] if usdc else None
    if quote=='0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9':return usdt[0] if usdt else None
    rows=sql(SECONDS/'bars_1s_onchain.sqlite3',"SELECT close,time FROM bars_1s WHERE market=? AND time<=? AND time>? AND source='tip_hot' ORDER BY time DESC LIMIT 1",(info['quote_market'],stamp*1000+999,(stamp-120)*1000))
    if rows:return rows[0]['close']
    return None

def main():
    pools=json.loads(POOL.read_text());db=SECONDS/'fastfeed_observations.sqlite3';c=sqlite3.connect(db,timeout=1);c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL')
    c.executescript('CREATE TABLE IF NOT EXISTS bars(market TEXT,time INTEGER,open REAL,high REAL,low REAL,close REAL,samples INTEGER,first INTEGER,last INTEGER,receipt TEXT,PRIMARY KEY(market,time)) WITHOUT ROWID; CREATE TABLE IF NOT EXISTS observations(receipt TEXT PRIMARY KEY,market TEXT,time INTEGER,price REAL,pool TEXT,tx TEXT,block INTEGER,raw TEXT) WITHOUT ROWID; CREATE INDEX IF NOT EXISTS obs_time ON observations(time);')
    offset=INPUT.stat().st_size if INPUT.exists() else 0;seen=set();count=0;latest=0;last_status=0;last_prune=0
    while True:
        started=time.time()
        if INPUT.exists():
            if INPUT.stat().st_size<offset:offset=0
            with INPUT.open('rb') as f:f.seek(offset);blob=f.read(2*1024**2)
            end=blob.rfind(b'\n');lines=blob[:end+1].splitlines() if end>=0 else [];offset+=end+1
        else:lines=[]
        for raw in lines:
            try:
                r=json.loads(raw);stamp=int(r['t_ms']);info=pools.get(r.get('pool','').lower())
                if not info or not r.get('topics') or r['topics'][0].lower()!=TOPIC or not -2<time.time()-stamp/1000<5:continue
                receipt=hashlib.sha256((str(r.get('block'))+str(r.get('tx'))+r['pool'].lower()+r['data']).encode()).hexdigest()
                if receipt in seen:continue
                q=anchor(info,stamp//1000)
                if q is None:continue
                px=price(info,r['data'],q)
                if px is None:continue
                mid=info['market'];sec=stamp//1000
                if not c.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?)',(receipt,mid,stamp,px,r['pool'],r.get('tx'),r.get('block'),raw.decode())).rowcount:continue
                c.execute('''INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(market,time) DO UPDATE SET high=max(high,excluded.high),low=min(low,excluded.low),close=CASE WHEN excluded.last>=last THEN excluded.close ELSE close END,open=CASE WHEN excluded.first<first THEN excluded.open ELSE open END,samples=samples+1,first=min(first,excluded.first),last=max(last,excluded.last),receipt=CASE WHEN excluded.last>=last THEN excluded.receipt ELSE receipt END''',(mid,sec,px,px,px,px,1,stamp,stamp,receipt));seen.add(receipt);count+=1;latest=max(latest,stamp)
            except (ValueError,KeyError,TypeError,OverflowError,ZeroDivisionError):continue
        if len(seen)>30000:seen.clear()
        c.commit()
        if started-last_prune>3600:
            c.execute('DELETE FROM bars WHERE time<?',(int(started)-7*86400,));c.execute('DELETE FROM observations WHERE time<?',((int(started)-86400)*1000,));c.commit();last_prune=started
        if started-last_status>5:
            state={'at':started,'accepted':count,'pools':len(pools),'latest':latest,'policy':'exact_pool_decimals_provisional_v1'}
            tmp=SECONDS/'fastfeed-observer-status.tmp';tmp.write_text(json.dumps(state));tmp.replace(SECONDS/'fastfeed-observer-status.json');last_status=started
        time.sleep(max(.01,.1-(time.time()-started)))
if __name__=='__main__':main()
