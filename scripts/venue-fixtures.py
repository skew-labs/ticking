"""Remote-only adversarial fixtures for the native public collector."""
import json,sqlite3,subprocess,sys,time
from pathlib import Path
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
binary=Path(sys.argv[2]);stamp=int(time.time())//60*60000
market=dict(id='HL-BTC',market='BTC',source='hyperliquid_public',marketType='perpetual',base='BTC',quote='USDC',dex='',symbol='BTC',name='Bitcoin perpetual')
catalog=out/'catalog.json';catalog.write_text(json.dumps(dict(source='hyperliquid_public',markets=[market])))
bar=dict(t=stamp,T=stamp+59999,s='BTC',i='1m',o='100',h='102',l='99',c='101',v='2',n=3)
trade=dict(coin='BTC',side='B',time=stamp+5,px='101',sz='1',hash='0x'+'0'*64,tid=4)
book=dict(coin='BTC',time=stamp+5,levels=[[dict(px='100',sz='2',n=1)],[dict(px='102',sz='2',n=1)]])
frames=[]
def frame(kind,data,**extra):frames.append(dict(kind=kind,data=data,received=stamp+40000,**extra))
frame('rest',[bar],coin='BTC',period='1m',depth=600)
frame('trades',[trade,trade]);frame('l2Book',book)
frame('trades',[{**trade,'px':'103'}])
frame('candle',{**bar,'h':'90'});frame('candle',{**bar,'s':'WRONG'})
frame('candle',{**bar,'v':'NaN'});frame('candle',{**bar,'t':stamp+1})
frame('l2Book',{**book,'levels':[[dict(px='104',sz='1',n=1)],book['levels'][1]]})
frame('rest',[bar,{**bar,'l':'0'}],coin='BTC',period='1m',depth=5000)
frame('trades',[{**trade,'tid':5},{**trade,'tid':6,'sz':'-1'}])
source=out/'inputs.jsonl';source.write_text(''.join(json.dumps(x)+'\n' for x in frames));db=out/'fixture.sqlite3'
p=subprocess.run([str(binary),'fixture',str(catalog),str(db),str(source)],capture_output=True,text=True);(out/'native.log').write_text(p.stdout+p.stderr);assert p.returncode==0,p.stderr
result=json.loads(p.stdout);assert result==dict(accepted=3,invalid=8),result
c=sqlite3.connect(db);assert c.execute('select count(*) from trades').fetchone()[0]==1
assert c.execute('select count(*) from candles').fetchone()[0]==1
assert c.execute('select count(*) from jobs').fetchone()[0]==1
assert c.execute('select count(*) from books').fetchone()[0]==1
assert c.execute('select count(*) from originals').fetchone()[0]==11
assert c.execute('select count(*) from events where kind="rejected"').fetchone()[0]==8
c.close()
p=subprocess.run([str(binary),'fixture',str(catalog),str(db),str(source)],capture_output=True,text=True);assert p.returncode==0,p.stderr
c=sqlite3.connect(db);assert c.execute('select count(*) from trades').fetchone()[0]==1;c.close()
(out/'result.json').write_text(json.dumps(dict(native=result,assertions=10,restartDedup=True),indent=2));print((out/'result.json').read_text())
