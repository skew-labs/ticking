import asyncio
from copy import deepcopy
import json
import time

import pytest

from . import realtime as r
from .changes import ChangeFeed, TOKEN, GLOBAL

def test_pool_archive_corrections_invalidate_full_history_without_waking_raw_execution_topics():
    from .changes import dependencies
    assert 'pool_archive' in dependencies('/token/candles',{'history':'full'})
    assert 'pool_archive' not in dependencies('/token/candles',{'history':'executions'})


def frame(value=1):
    return {'identity':{'market':'test'},'candles':[{'time':60000,'close':value}]}


def test_commit_wakes_shared_topic_without_periodic_reads(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'INTEREST',tmp_path/'interest.json')
    feed=ChangeFeed();feed.connected=True;monkeypatch.setattr(r,'feed',feed)
    async def run():
        hub=r.StreamHub();value=1
        async def load():return frame(value)
        a,q=hub.attach('asset',load,'one',dependencies=TOKEN,period=.1)
        b,q2=hub.attach('asset',load,'two',dependencies=TOKEN,period=.1)
        await asyncio.wait_for(q.get(),1);await q2.get()
        await asyncio.sleep(.3)
        assert a is b and a.loads==1
        value=2
        feed.accept({'boot':'a','versions':{'token':1},'observedAt':0},hub)
        msg=json.loads(await asyncio.wait_for(q.get(),.3))
        assert msg['type']=='delta' and msg['candles'][0]['close']==2
        assert json.loads(await q2.get())==msg
        assert a.loads==2
        await hub.close()
    asyncio.run(run())


def test_notification_during_read_is_not_lost_and_unrelated_sources_stay_idle(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'INTEREST',tmp_path/'interest.json')
    feed=ChangeFeed();feed.connected=True;monkeypatch.setattr(r,'feed',feed)
    async def run():
        hub=r.StreamHub();gate=asyncio.Event();value=1;reads=0
        async def load():
            nonlocal reads
            reads+=1;snapshot=frame(value)
            if reads==1:await gate.wait()
            return snapshot
        t,q=hub.attach('asset',load,'one',dependencies=TOKEN,period=.1)
        await asyncio.sleep(.02)
        value=2;feed.accept({'boot':'a','versions':{'token':1}},hub);gate.set()
        await asyncio.wait_for(q.get(),1)
        await asyncio.sleep(.2)
        assert t.latest==frame(2) and reads==2
        feed.accept({'boot':'a','versions':{'token':1,'exchange':1}},hub)
        await asyncio.sleep(.15);assert reads==2
        # Feed restart forces resync even when the numerical versions match.
        feed.accept({'boot':'b','versions':{'token':1,'exchange':1}},hub)
        await asyncio.sleep(.15);assert reads==3
        await hub.close()
    asyncio.run(run())


def test_feed_failure_restores_polling_and_older_corrections_reach_client(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'INTEREST',tmp_path/'interest.json')
    feed=ChangeFeed();monkeypatch.setattr(r,'feed',feed)
    async def run():
        hub=r.StreamHub();value=1
        async def load():return frame(value)
        t,q=hub.attach('asset',load,'one',dependencies=TOKEN,period=.05)
        await asyncio.wait_for(q.get(),1);value=4
        msg=json.loads(await asyncio.wait_for(q.get(),1))
        assert msg['candles'][0]['time']==60000 and msg['candles'][0]['close']==4
        await hub.close()
    asyncio.run(run())


def test_change_vectors_reject_regression_and_bound_untrusted_input():
    feed=ChangeFeed();hub=r.StreamHub()
    feed.accept({'boot':'a','versions':{'token':2}},hub)
    with pytest.raises(ValueError):feed.accept({'boot':'a','versions':{'token':1}},hub)
    with pytest.raises(ValueError):feed.accept({'boot':'a','versions':{'token':True}},hub)
    assert feed.fingerprint(TOKEN)[1]==(('config',0),('managed',0),('token',2))


def test_healthy_stream_reconciles_a_missed_notification(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'INTEREST',tmp_path/'interest.json')
    feed=ChangeFeed();feed.connected=True;monkeypatch.setattr(r,'feed',feed)
    async def run():
        hub=r.StreamHub(reconcile_seconds=.15);value=1
        async def load():return frame(value)
        t,q=hub.attach('asset',load,'one',dependencies=TOKEN)
        await asyncio.wait_for(q.get(),1);value=8
        msg=json.loads(await asyncio.wait_for(q.get(),1))
        assert msg['candles'][0]['close']==8 and t.reconciles>=1
        await hub.close()
    asyncio.run(run())


def test_bridge_cache_invalidates_older_history_on_commit(tmp_path,monkeypatch):
    from . import engine_data as e
    import httpx
    feed=ChangeFeed();monkeypatch.setattr(e,'feed',feed)
    monkeypatch.setattr(e,'cache',e.OrderedDict());monkeypatch.setattr(e,'cache_bytes',0)
    key=tmp_path/'key';key.write_text('isolated-test-only');monkeypatch.setattr(e,'SECRET_FILE',key)
    calls=[]
    def request(url,**kwargs):
        calls.append(url)
        return httpx.Response(200,json={'value':len(calls)},request=httpx.Request('GET','http://private.test'))
    monkeypatch.setattr(e.client,'get',request)
    assert e.get('/token/candles',token='test',history='full',before=100)=={'value':1}
    assert e.get('/token/candles',token='test',history='full',before=100)=={'value':1}
    feed.accept({'boot':'a','versions':{'gap':1}},r.StreamHub())
    assert e.get('/token/candles',token='test',history='full',before=100)=={'value':2}
