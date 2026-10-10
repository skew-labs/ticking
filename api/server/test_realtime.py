import asyncio
from copy import deepcopy
import json
import pytest
from fastapi import HTTPException
from . import realtime as r

def frame(close=2):
    return {'identity':{'market':'SOL'},'candles':[{'time':60000,'close':1},{'time':120000,'close':close}]}

def test_delta_includes_older_corrections_and_removed_window():
    old=frame();new=deepcopy(old);new['candles'][0]['close']=3
    d=r.delta(old,new,2,1)
    assert d['candles']==[{'time':60000,'close':3}] and d['base']==1
    new['candles']=new['candles'][1:]
    assert r.delta(old,new,3,2)['times']==[120000]
    new['identity']={'market':'BTC'}
    assert r.delta(old,new,3,2)['type']=='snapshot'

def test_shared_topic_backpressure_resets_and_cleans_interest(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'INTEREST',tmp_path/'interest.json')
    async def run():
        hub=r.StreamHub();gate=asyncio.Event()
        async def load(): await gate.wait();return frame()
        a,q=hub.attach('sol',load,'ip',mint='s'*32)
        b,q2=hub.attach('sol',load,'ip',mint='s'*32)
        assert a is b and len(hub.topics)==1
        a.publish(frame());assert json.loads(q.get_nowait())['type']=='snapshot'
        a.publish(frame(4));assert json.loads(q.get_nowait())['type']=='delta'
        a.publish(frame(6));a.publish(frame(7))
        latest=json.loads(q.get_nowait());assert latest['type']=='snapshot' and latest['data']==frame(7)
        assert json.loads(q2.get_nowait())['data']==frame(7)
        assert json.loads((tmp_path/'interest.json').read_text())['mints']=={'s'*32:2}
        hub.detach(a,q,'ip','s'*32);hub.detach(b,q2,'ip','s'*32)
        assert not hub.topics and not hub.ips and not hub.mints
        await hub.close()
    asyncio.run(run())

def test_admission_bounds_do_not_allocate_extra_topics(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'INTEREST',tmp_path/'interest.json')
    async def run():
        hub=r.StreamHub(max_topics=1,max_clients=2)
        async def load(): await asyncio.sleep(100)
        a,q=hub.attach('a',load,'one')
        with pytest.raises(HTTPException):hub.attach('b',load,'two')
        b,q2=hub.attach('a',load,'two')
        with pytest.raises(HTTPException):hub.attach('a',load,'three')
        hub.detach(a,q,'one',None);hub.detach(b,q2,'two',None);await hub.close()
    asyncio.run(run())

def test_analytics_uses_current_universe_24h_and_keeps_unknowns():
    rows=[{'id':'SOL','price':100,'change':5,'ranking_eligible':True,'stale':False},
          {'id':'BTC','network':'exchange','price':60000,'change':-2,'ranking_eligible':True,'stale':False},
          {'id':'OLD','price':1,'change':100,'ranking_eligible':False,'stale':True},
          {'id':'MISSING','price':None,'change':None,'stale':True}]
    d=r.analytics_snapshot({'tokens':rows,'generated_at':10})
    assert d['listed']==4 and d['priced']==3 and d['fresh']==2
    assert d['advancing']==1 and d['declining']==1 and d['global']==1
    assert d['tokens'][3]['price'] is None and 'volume' not in d

def test_failed_loader_preserves_latest(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'INTEREST',tmp_path/'interest.json')
    async def run():
        hub=r.StreamHub()
        async def fail():raise ValueError('secret-provider-url-not-published')
        t,q=hub.attach('a',fail,'one');t.publish(frame());q.get_nowait()
        msg=await asyncio.wait_for(q.get(),1)
        assert json.loads(msg)=={'type':'unavailable','retryMs':5000}
        assert t.latest==frame()
        hub.detach(t,q,'one',None);await hub.close()
    asyncio.run(run())

def test_empty_stream_keeps_granted_market_and_rechecks_revocation(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from . import app as api,store,global_data
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'console.sqlite3')
    monkeypatch.setattr(r,'INTEREST',tmp_path/'interest.json')
    for name in ('MARKETS','NEWS','CRYPTO'):monkeypatch.setenv('TICKING_'+name+'_ENABLED','false')
    requested=[]
    def empty(market,*a,**kw):
        requested.append(market)
        return {'market':market,'identity':{'network':'exchange'},'candles':[]}
    monkeypatch.setattr(global_data,'candles',empty)
    with TestClient(api.app,base_url='https://ticking.dev',headers={'Origin':'https://ticking.dev'}) as c:
        c.post('/api/auth/signup',json={'email':'stream@example.test','password':'isolated-stream-pass-12345','name':'Stream test'})
        key=c.post('/api/keys',json={'name':'Stream','scopes':['candles:read']}).json()
        grant=c.post('/api/v1/display-token',headers={'Authorization':'Bearer '+key['key']},json={'origin':'https://embed.example','market':'BTC','aggregation':'exchange'}).json()
        auth={'Authorization':'Bearer '+grant['token'],'Origin':'https://embed.example'}
        result=c.get('/api/v1/display/stream?market=ETH&mint=ignored',headers=auth)
        assert result.status_code==409 and requested==['BTC'], result.text
        assert 'No retained records' in result.json()['detail']
        assert c.get('/api/v1/display/stream',headers={**auth,'Origin':'https://wrong.example'}).status_code==403
        assert c.options('/api/v1/display/stream',headers={'Origin':'https://embed.example'}).status_code==204
        c.delete('/api/keys/'+key['id'])
        assert c.get('/api/v1/display/stream',headers=auth).status_code==401

def test_global_24h_breadth_uses_own_source_freshness(monkeypatch,tmp_path):
    from . import market_data as md,global_data
    m=md.MarketData(tmp_path/'cache.json');monkeypatch.setattr(m,'solana_volume',lambda mints:{'volume':None})
    monkeypatch.setattr(global_data,'ASSETS',{'BTC':{'coingecko':'bitcoin'}})
    source={'bitcoin':{'price_change_percentage_24h':2,'last_updated':md.datetime.now(md.timezone.utc).isoformat()}}
    row={'id':'BTC','price':50000,'stale':False}
    assert m.enrich_global(dict(row),source)['ranking_eligible'] is True
    source['bitcoin']['last_updated']='2020-01-01T00:00:00Z'
    assert m.enrich_global(dict(row),source)['ranking_eligible'] is False
    assert m.enrich_global(dict(row),{})['ranking_eligible'] is False
