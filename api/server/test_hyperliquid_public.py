"""Instrument namespace and existing customer billing boundaries; no live accounts."""
import pytest
from . import app as api,global_data,engine_data,product_delivery,products,store
from .test_credits import client,setup,read
from .test_console import signup,key
from .test_products import entitlement

def test_distinct_venue_identity(monkeypatch):
    assert global_data.chosen('BTC')['id']=='BTC'
    assert global_data.chosen('BTC')['source']!='hyperliquid_public'
    assert global_data.chosen('HL-BTC')['market']=='BTC'
    assert global_data.chosen('HL-xyz:NVDA')['market']=='xyz:NVDA'
    assert global_data.chosen('xyz:NVDA') is None
    seen=[]
    def get(path,**q):seen.append((path,q));return dict(candles=[],nextBefore=None)
    monkeypatch.setattr(engine_data,'get',get)
    global_data.candles('HL-BTC','1m',100)
    assert seen[-1]==('/hyperliquid/candles',dict(market='HL-BTC',interval=60,limit=100,before=None,gapPolicy='recorded'))
    with pytest.raises(Exception):engine_data.candles(market='HL-BTC',mint='So11111111111111111111111111111111111111112')

def test_data_entitlement_and_book_scope(client,monkeypatch):
    user=signup(client);k=key(client,['trades:read']);headers={'Authorization':'Bearer '+k['key']}
    monkeypatch.setattr(global_data,'book',lambda m:dict(market=m,book={'levels':[[],[]]}))
    assert client.get('/api/v1/book?market=HL-BTC').status_code==401
    entitlement(user,'Charts')
    assert client.get('/api/v1/book?market=HL-BTC',headers=headers).status_code==403
    entitlement(user,'Data')
    assert client.get('/api/v1/book?market=HL-BTC',headers=headers).json()['market']=='HL-BTC'
    revoked=client.delete('/api/keys/'+k['id']);assert revoked.status_code==200
    assert client.get('/api/v1/book?market=HL-BTC',headers=headers).status_code==401

def test_prepaid_exchange_read_is_idempotent(client,monkeypatch):
    user,k=setup(client,monkeypatch)
    monkeypatch.setattr(global_data,'chosen',lambda m:{'id':'HL-BTC','source':'hyperliquid_public'} if m=='HL-BTC' else None)
    first=read(client,k,'hl-candle-request',kind='candles',market='HL-BTC');assert first.status_code==200,first.text
    again=read(client,k,'hl-candle-request',kind='candles',market='HL-BTC');assert again.status_code==200
    assert again.headers['x-ticking-replayed']=='true'
    assert again.headers['x-ticking-charged-micro-usdc']=='0'
    bad=read(client,k,'hl-invalid-mixed',kind='candles',market='HL-BTC',mint='So11111111111111111111111111111111111111112')
    assert bad.status_code==422

def test_stream_selection_keeps_venue():
    assert product_delivery.selection('HL-BTC',None,None,'exchange')=={'market':'HL-BTC','aggregation':'exchange'}
    with pytest.raises(Exception):product_delivery.selection('HL-BTC','So11111111111111111111111111111111111111112',None,'exchange')

def test_venue_outage_keeps_other_markets_and_marks_retained_venue_stale(monkeypatch):
    from fastapi import HTTPException
    from server import engine_data,token_icons
    legacy={'id':'BTC','source':'binance_spot','market':'BTCUSDT'}
    retained={'id':'HL-BTC','source':'hyperliquid_public','market':'BTC','stale':False}
    monkeypatch.setattr(global_data,'_hl_snapshot',[retained])
    monkeypatch.setattr(token_icons,'exchange_icon_url',lambda *a:None)
    def read(path):
        if path=='/reference/catalog':return {'markets':[legacy]}
        raise HTTPException(503,'Venue store unavailable')
    monkeypatch.setattr(engine_data,'get',read)
    rows=global_data.snapshot()
    assert rows[0]['id']=='BTC'
    assert rows[1]['id']=='HL-BTC' and rows[1]['stale'] and rows[1]['sourceUnavailable']
    assert retained['stale'] is False
