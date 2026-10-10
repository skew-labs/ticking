"""Isolated product, payment, promotional ledger and delivery boundary checks."""
import asyncio
import hashlib
import json
import time
import pytest
from solders.keypair import Keypair
from solders.signature import Signature
from . import app as api, credits, products, product_delivery as delivery, payments, store
from .test_credits import client, setup, read
from .test_console import signup,key,receipt

@pytest.mark.parametrize('check',['test_receipt_validation','test_rpc_reference_account_parser_shape'])
def test_existing_receipt_checks_with_current_plan(client,monkeypatch,check):
    from . import test_console as legacy
    def current_invoice(c,payer=None):
        r=c.post('/api/billing/invoices',json={'plan':'Data','payer':payer or str(Keypair().pubkey()),'accepted_preview':True,'amount':1})
        assert r.status_code==200,r.text
        return r.json()
    monkeypatch.setattr(legacy,'invoice',current_invoice)
    getattr(legacy,check)(client)

def entitlement(user,plan,expires=None):
    with store.db() as c:c.execute('INSERT INTO entitlements VALUES(?,?,?,?)',(user['id'],'fixture:'+plan,plan,expires or int(time.time())+10000))

def identity(user):
    with store.db() as c:c.execute('INSERT INTO auth_identities VALUES(?,?,?,?)',('did:privy:'+user['id'],user['id'],json.dumps([{'type':'google','label':'verified-test@example.test'}]),int(time.time())))

def test_prices_and_retired_checkout(client):
    signup(client);config=client.get('/api/config').json();assert set(config['plans'])=={'Explore','Charts','Data'}
    for name,amount,annual in [('Charts',2900,27840),('Data',5900,56640)]:
        assert config['plans'][name]['billing']['monthly']['amount']==amount
        assert config['plans'][name]['billing']['yearly']['amount']==annual
    for name in ['Build','Scale','Pro']:
        r=client.post('/api/billing/invoices',json={'plan':name,'payer':str(Keypair().pubkey()),'accepted_preview':True});assert r.status_code==422

@pytest.mark.parametrize('plan',['Charts','Data'])
def test_exact_finalized_payment_activates_once(client,monkeypatch,plan):
    user=signup(client);r=client.post('/api/billing/invoices',json={'plan':plan,'billing_period':'yearly','payer':str(Keypair().pubkey()),'accepted_preview':True,'amount':1});assert r.status_code==200
    invoice=r.json();tx,status=receipt(invoice);sig=str(Signature.new_unique())
    proof={'signature':sig,'genesis':payments.GENESIS,'transaction':tx,'status':status}
    monkeypatch.setattr(api,'verify_invoice',lambda *a,**kw:proof)
    for _ in range(3):assert client.post('/api/billing/invoices/'+invoice['id']+'/verify',json={'signature':sig}).json()['status']=='paid'
    with store.db() as c:
        assert c.execute('SELECT COUNT(*) FROM entitlements WHERE user_id=?',(user['id'],)).fetchone()[0]==1
        row=c.execute('SELECT * FROM plan_receipts').fetchone();assert row['sha256']==hashlib.sha256(row['receipt'].encode()).hexdigest()
        assert c.execute('SELECT policy_version FROM invoices WHERE id=?',(invoice['id'],)).fetchone()[0]==products.VERSION
    snapshot=client.get('/api/console').json();assert snapshot['plan']['plan']==plan and snapshot['limits']['requests'] is None
    assert snapshot['credits']['balance_micro']==0

def test_confirmed_payment_does_not_activate(client,monkeypatch):
    user=signup(client);inv=client.post('/api/billing/invoices',json={'plan':'Data','payer':str(Keypair().pubkey()),'accepted_preview':True}).json()
    monkeypatch.setattr(api,'verify_invoice',lambda *a,**k:False)
    assert client.post('/api/billing/invoices/'+inv['id']+'/verify',json={'signature':str(Signature.new_unique())}).json()['status']=='confirming'
    assert products.plan_for(user['id'])['plan']=='Explore'

def test_wrong_network_receipt_never_activates(client,monkeypatch):
    user=signup(client);inv=client.post('/api/billing/invoices',json={'plan':'Data','payer':str(Keypair().pubkey()),'accepted_preview':True}).json()
    tx,status=receipt(inv);sig=str(Signature.new_unique())
    monkeypatch.setattr(api,'verify_invoice',lambda *a,**k:{'genesis':'wrong-network','signature':sig,'transaction':tx,'status':status})
    assert client.post('/api/billing/invoices/'+inv['id']+'/verify',json={'signature':sig}).status_code==503
    assert products.plan_for(user['id'])['plan']=='Explore'

def test_unverified_identity_does_not_grant_credit(client):
    user=signup(client)
    with store.db() as c:c.execute('INSERT INTO auth_identities VALUES(?,?,?,?)',('did:privy:empty',user['id'],'[]',int(time.time())))
    assert client.get('/api/credits').json()['trial']['eligible'] is False
    assert client.post('/api/credits/trial',json={}).status_code==403

def test_prepaid_stream_disconnect_settles_once_and_releases(client,monkeypatch):
    from types import SimpleNamespace
    from starlette.datastructures import Headers
    user=signup(client);identity(user);products.claim_trial(user['id']);k=key(client,['trades:read']);credits.configure(user['id'],True,1000000,True,True)
    monkeypatch.setattr(api.engine_data,'trades',lambda **kwargs:{'trades':[{'identity':'fixture-1','slot':1,'price':2}],'identity':{'market':'fixture'}})
    endpoint=next(r.endpoint for r in api.app.routes if getattr(r,'path','')=='/api/v1/stream/{kind}')
    request=SimpleNamespace(headers=Headers({'Authorization':'Bearer '+k['key'],'Idempotency-Key':'disconnect-test-001','X-Ticking-Accept-Charges':'stream-v1'}),client=SimpleNamespace(host='stream-test'),url=SimpleNamespace(path='/api/v1/stream/trades'))
    async def disconnected():return False
    request.is_disconnected=disconnected
    async def run():
        response=await endpoint('trades',request,mint='So11111111111111111111111111111111111111112',market=None,pool=None,aggregation='token',interval='1m',after=None,prepaid=True)
        assert (await anext(response.body_iterator)).startswith(': connected')
        frame=json.loads((await anext(response.body_iterator))[6:]);assert frame['data']['trades']
        await response.body_iterator.aclose()
    asyncio.run(run())
    snap=credits.snapshot(user['id']);assert snap['trial']['reserved_micro']==0 and snap['reserved_micro']==0
    assert snap['trial']['balance_micro']==4999850 and snap['balance_micro']==0
    with store.db() as c:assert c.execute("SELECT COUNT(*) FROM credit_requests WHERE status='completed'").fetchone()[0]==1
    assert credits.reconcile()['passed']

def test_hosted_tape_renewal_keeps_exact_native_identity(client,monkeypatch):
    user=signup(client);entitlement(user,'Data');p=client.post('/api/projects',json={'name':'App','origins':['https://app.test']}).json()
    mint='So11111111111111111111111111111111111111112'
    monkeypatch.setattr(delivery.global_data,'chosen',lambda m:{'id':'SOL','market':'SOLUSDT','source':'composite','quote':'USDT'} if m=='SOL' else None)
    monkeypatch.setattr(delivery.market_data.markets,'token',lambda m:{'id':'SOL','mint':mint})
    monkeypatch.setattr(delivery.market_data.markets,'catalog',lambda:[{'id':'SOL','mint':mint}])
    monkeypatch.setattr(delivery.market_data,'market_review',lambda t:True)
    monkeypatch.setattr(delivery.engine_data,'resolve',lambda **kw:{'mint':kw['mint'],'ready':True,'quoteMint':payments.MINT})
    r=client.get('/embed/'+p['publishable']+'?market=SOL&trades=true',headers={'Referer':'https://app.test/page'})
    assert r.status_code==200 and 'frame-ancestors https://app.test' in r.headers['Content-Security-Policy']
    settings=json.loads(r.text.split('id="ticking-settings">')[1].split('</script>')[0]);assert settings['grant']['aggregation']=='token'
    assert settings['grant']['origin']=='https://api.ticking.dev'
    renewed=client.post('/api/embeds/'+p['publishable']+'/renew',headers={'Origin':'https://api.ticking.dev','Authorization':'Bearer '+settings['grant']['token']},json={})
    assert renewed.status_code==200,renewed.text
    assert renewed.json()['aggregation']=='token' and renewed.json()['mint']==mint

def test_charts_denies_direct_tape_and_candles(client,monkeypatch):
    user=signup(client);entitlement(user,'Charts');k=key(client,['candles:read','trades:read','charts:embed']);headers={'Authorization':'Bearer '+k['key']}
    assert client.get('/api/v1/candles?market=SOL',headers=headers).status_code==403
    assert client.get('/api/v1/trades?market=SOL',headers=headers).status_code==403
    assert client.get('/api/v1/trades/history?market=SOL',headers=headers).status_code==403
    assert client.get('/api/v1/stream/trades?market=SOL',headers=headers).status_code==403

def test_data_has_no_monthly_cap_but_workspace_rate(client,monkeypatch):
    user=signup(client);entitlement(user,'Data');k1=key(client,['trades:read']);k2=key(client,['trades:read'])
    with store.db() as c:c.execute('INSERT INTO usage_events(user_id,endpoint,created,units) VALUES(?,?,?,?)',(user['id'],'fixture',int(time.time()),999999999))
    monkeypatch.setattr(api.engine_data,'trades',lambda *a,**kw:{'trades':[]})
    for i in range(120):assert client.get('/api/v1/trades?market=SOL',headers={'Authorization':'Bearer '+(k1 if i%2 else k2)['key']}).status_code==200
    assert client.get('/api/v1/trades?market=SOL',headers={'Authorization':'Bearer '+k1['key']}).status_code==429
    assert client.get('/api/console').json()['limits']['requests'] is None
    with store.db() as c:assert c.execute('SELECT COUNT(*) FROM usage_rollups').fetchone()[0]<=2

def test_scope_separation(client,monkeypatch):
    user=signup(client);entitlement(user,'Data');k=key(client,['candles:read'])
    assert client.get('/api/v1/trades?market=SOL',headers={'Authorization':'Bearer '+k['key']}).status_code==403

def test_legacy_entitlement_unchanged_until_expiry(client):
    user=signup(client);entitlement(user,'Build');assert products.limits(user['id'])['requests']==1000000
    assert products.limits(user['id'])['usd']==149
    with store.db() as c:c.execute('UPDATE entitlements SET expires=1 WHERE user_id=?',(user['id'],))
    assert products.plan_for(user['id'])['plan']=='Explore'

def test_trial_verified_once_separate_and_spendable(client,monkeypatch):
    user=signup(client);assert client.post('/api/credits/trial',json={}).status_code==403
    identity(user);assert client.post('/api/credits/trial',json={}).json()['balance_micro']==5000000
    assert client.post('/api/credits/trial',json={}).json()['balance_micro']==5000000
    k=key(client,['candles:read']);client.put('/api/credits/settings',json={'enabled':True,'daily_limit_micro':1000000,'accepted_terms':True})
    monkeypatch.setattr(api.engine_data,'candles',lambda **k:{'candles':[{'time':int(time.time())*1000,'close':100}],'stale':False})
    r=read(client,k,'trial-read-1');assert r.status_code==200,r.text
    r=read(client,k,'trial-read-1');assert r.headers['X-Ticking-Charged-Micro-USDC']=='0'
    snap=client.get('/api/credits').json();assert snap['balance_micro']==0 and snap['trial']['balance_micro']==4999995
    assert credits.reconcile()['passed']

def test_trial_reservation_crash_release(client):
    user=signup(client);identity(user);products.claim_trial(user['id']);k=key(client,['candles:read']);credits.configure(user['id'],True,1000000,True)
    row=credits.reserve(user['id'],k['id'],'trial-hold-001','candles',{})
    assert credits.snapshot(user['id'])['trial']['reserved_micro']==100
    credits.release(row['id']);assert credits.snapshot(user['id'])['trial']['reserved_micro']==0
    assert credits.reconcile()['passed']

def test_prepaid_stream_opt_in_and_time_meter(client):
    user=signup(client);identity(user);products.claim_trial(user['id']);k=key(client,['trades:read']);credits.configure(user['id'],True,1000000,True)
    with pytest.raises(credits.CreditError):credits.reserve(user['id'],k['id'],'stream-session:1','stream',{})
    credits.configure(user['id'],True,1000000,True,True)
    row=credits.reserve(user['id'],k['id'],'stream-session:2','stream',{})
    result=credits.complete(row['id'],{'slice':1},{'seconds':16,'bytes':32769});assert result['charged_micro']==300
    assert result['trial_charged_micro']==300 and credits.snapshot(user['id'])['balance_micro']==0
    assert credits.reconcile()['passed']

def test_project_domain_caps_and_tenant_isolation(client):
    user=signup(client);entitlement(user,'Charts')
    p=client.post('/api/projects',json={'name':'Launchpad','origins':['https://launch.test','https://app.test']});assert p.status_code==200
    assert client.post('/api/projects',json={'name':'Other','origins':['https://third.test']}).status_code==409
    client.post('/api/auth/logout',json={});signup(client,'other@example.test')
    assert client.delete('/api/projects/'+p.json()['id']).status_code==404

def test_trade_deltas_resumption_shared_receipt():
    hub=api.realtime.StreamHub();topic=delivery.DeliveryTopic(hub,('test',),None,False,.5)
    def row(i,p=1):return {'identity':str(i),'slot':i,'price':p}
    first={'identity':{'market':'test'},'trades':[row(1),row(2)]};topic.publish(first)
    cursor=topic.epoch+':1';topic.publish({'identity':first['identity'],'trades':[row(2,2),row(3)]})
    event=json.loads(topic.resume(cursor)[0]);assert event['type']=='trade_delta' and event['ids']==['2','3'] and len(event['trades'])==2
    assert json.loads(topic.resume('old:1')[0])['type']=='snapshot'
    assert topic.resume(topic.epoch+':2')==[]
    digest=delivery.integrity(first);assert digest==delivery.integrity(first) and not delivery.receipts[digest]['consensusProof']
    for i in range(3,30):topic.publish({'identity':first['identity'],'trades':[row(i)]})
    assert len(topic.frames)<=16 and topic.frame_bytes<=1048576

def test_project_grants_reject_wrong_origins_and_charts_tape(client,monkeypatch):
    user=signup(client);entitlement(user,'Charts');p=client.post('/api/projects',json={'name':'App','origins':['https://app.test']}).json()
    monkeypatch.setattr(delivery.global_data,'chosen',lambda m:None)
    monkeypatch.setattr(delivery.market_data.markets,'token',lambda m:{'id':'SOL','mint':'So11111111111111111111111111111111111111112'})
    monkeypatch.setattr(delivery.market_data,'market_review',lambda t:True)
    monkeypatch.setattr(delivery.engine_data,'resolve',lambda **kw:{'mint':kw['mint'],'ready':True,'quoteMint':payments.MINT})
    url='/api/embeds/'+p['publishable']+'/token'
    assert client.post(url,headers={'Origin':'https://wrong.test'},json={'market':'SOL'}).status_code==403
    assert client.post(url,headers={'Origin':'https://app.test'},json={'market':'SOL','trades':True}).status_code==403
    r=client.post(url,headers={'Origin':'https://app.test'},json={'market':'SOL'});assert r.status_code==200,r.text
    token=r.json()['token']
    with store.db() as c:assert c.execute('SELECT project_id FROM display_tokens WHERE hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()[0]==p['id']
    client.delete('/api/projects/'+p['id']);assert client.get('/api/v1/display/candles',headers={'Origin':'https://app.test','Authorization':'Bearer '+token}).status_code==403
