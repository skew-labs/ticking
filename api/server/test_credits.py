"""Isolated accounting / receipt tests. No real wallet, RPC call or settlement."""
import copy
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from solders.keypair import Keypair
from solders.signature import Signature
from solders.hash import Hash
from solders.transaction import Transaction
import base64
import httpx
from . import app as api, credits as ledger, credit_api, payments as pay, store
from .test_console import signup, key, receipt

ORIGIN={'Origin':'https://ticking.dev'}

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'isolated-credits.sqlite')
    monkeypatch.setenv('TICKING_CREDITS_ENABLED','true')
    monkeypatch.setenv('TICKING_MARKETS_ENABLED','false');monkeypatch.setenv('TICKING_NEWS_ENABLED','false')
    monkeypatch.setattr(pay,'RECIPIENT','7aFkGZYvx3UUpK5duxpjEFExHhP9wy4gN62mbvvHfh81')
    monkeypatch.setattr(api,'RECIPIENT',pay.RECIPIENT)
    monkeypatch.setattr(pay,'crypto_ready',lambda:True);monkeypatch.setattr(api,'crypto_ready',lambda:True)
    def forbidden(*args,**kwargs):raise AssertionError('A test attempted live RPC')
    monkeypatch.setattr(pay,'rpc',forbidden)
    monkeypatch.setattr(credit_api,'payment_rpc',forbidden)
    with TestClient(api.app,base_url='https://ticking.dev',headers=ORIGIN) as c:yield c

def topup(c,cents=100):
    r=c.post('/api/credits/topups',json={'amount_cents':cents,'payer':str(Keypair().pubkey()),'accepted_terms':True})
    assert r.status_code==200,r.text
    return r.json()

def fund(c,monkeypatch,cents=100):
    inv=topup(c,cents);tx,status=receipt(inv);sig=str(Signature.new_unique())
    proof={'genesis':pay.GENESIS,'signature':sig,'transaction':tx,'status':status}
    monkeypatch.setattr(pay,'verify_invoice',lambda *args,**kwargs:proof)
    r=c.post('/api/credits/topups/'+inv['id']+'/verify',json={'signature':sig})
    assert r.status_code==200 and r.json()['status']=='paid',r.text
    return inv,sig,proof

def setup(c,monkeypatch):
    user=signup(c);k=key(c,['candles:read']);fund(c,monkeypatch)
    r=c.put('/api/credits/settings',json={'enabled':True,'daily_limit_micro':1000000,'accepted_terms':True})
    assert r.status_code==200,r.text
    monkeypatch.setattr(api.engine_data.global_data,'chosen',lambda *args:None)
    def candles(**kwargs):
        return {'candles':[{'time':int(time.time())*1000,'open':100,'high':102,'low':99,'close':101}],
                'market':'test-pool','pool':'test-pool','mint':'test-mint','quote':'USDC','quoteMint':pay.MINT,
                'interval':kwargs.get('interval','1m'),'stale':False,'coverage':{'complete':False,'commitment':'processed'},'live':False,'complete':False}
    monkeypatch.setattr(api.engine_data,'candles',candles)
    monkeypatch.setattr(api.engine_data,'trades',lambda **kwargs:{'trades':[{'signature':'test-only','price':101}],'complete':False})
    return user,k

def read(c,k,ident='request-0001',kind='price',**params):
    return c.get('/api/v1/prepaid/'+kind,params={'market':'SOL',**params},headers={'Authorization':'Bearer '+k['key'],'Idempotency-Key':ident})

def test_pending_and_confirmed_receipts_never_credit(client,monkeypatch):
    signup(client);inv=topup(client)
    assert client.get('/api/credits').json()['balance_micro']==0
    monkeypatch.setattr(pay,'verify_invoice',lambda *a,**kw:False)
    r=client.post('/api/credits/topups/'+inv['id']+'/verify',json={'signature':str(Signature.new_unique())})
    assert r.json()['status']=='confirming'
    assert client.get('/api/credits').json()['balance_micro']==0
    assert ledger.reconcile()['passed']

def test_finalized_topup_exactly_once_balanced_receipt_and_no_entitlement(client,monkeypatch):
    signup(client);inv,sig,proof=fund(client,monkeypatch,123)
    for _ in range(3):
        r=client.post('/api/credits/topups/'+inv['id']+'/verify',json={'signature':sig})
        assert r.json()['status']=='paid'
    snapshot=client.get('/api/console').json()
    assert snapshot['credits']['balance_micro']==1230000 and not snapshot['credits']['enabled']
    assert snapshot['invoices']==[] and snapshot['plan']['plan']=='Explore'
    assert client.get('/api/billing/invoices/'+inv['id']).status_code==404
    with store.db() as c:
        assert c.execute('SELECT COUNT(*) n FROM credit_entries').fetchone()['n']==2
        assert c.execute('SELECT SUM(delta_micro) n FROM credit_entries').fetchone()['n']==0
        assert c.execute('SELECT COUNT(*) n FROM entitlements').fetchone()['n']==0
        row=c.execute('SELECT * FROM credit_receipts').fetchone()
        assert json.loads(row['proof'])==proof
    assert ledger.reconcile()['passed']

@pytest.mark.parametrize('fault',['confirmed','wrong_token','wrong_recipient','wrong_payer','wrong_reference','amount','balance_delta','failed','wrong_network'])
def test_receipt_mutations_rejected(client,monkeypatch,fault):
    signup(client);inv=topup(client);tx,status=receipt(inv);sig=str(Signature.new_unique())
    proof={'genesis':pay.GENESIS,'signature':sig,'transaction':tx,'status':status}
    info=tx['transaction']['message']['instructions'][0]['parsed']['info']
    if fault=='confirmed':status['confirmationStatus']='confirmed'
    elif fault=='wrong_token':info['mint']=str(Keypair().pubkey())
    elif fault=='wrong_recipient':info['destination']=str(Keypair().pubkey())
    elif fault=='wrong_payer':info['authority']=str(Keypair().pubkey())
    elif fault=='wrong_reference':tx['transaction']['message']['accountKeys'].pop()
    elif fault=='amount':info['tokenAmount']['amount']='1'
    elif fault=='balance_delta':tx['meta']['postTokenBalances'][0]['uiTokenAmount']['amount']='1'
    elif fault=='failed':status['err']={'InstructionError':[1,'test-only']}
    elif fault=='wrong_network':proof['genesis']='devnet'
    monkeypatch.setattr(pay,'verify_invoice',lambda *a,**kw:proof)
    with pytest.raises((pay.PaymentError,ledger.CreditError)):
        with store.db() as c:c.execute('UPDATE invoices SET signature=? WHERE id=?',(sig,inv['id']))
        ledger.finalize_topup(inv['id'],proof)
    assert client.get('/api/credits').json()['balance_micro']==0
    with store.db() as c:assert c.execute('SELECT COUNT(*) n FROM credit_receipts').fetchone()['n']==0
    assert ledger.reconcile()['passed']

def test_invoice_signature_global_uniqueness_and_tenant_isolation(client,monkeypatch):
    signup(client);inv,sig,proof=fund(client,monkeypatch)
    other=topup(client,200)
    assert client.post('/api/credits/topups/'+other['id']+'/verify',json={'signature':sig}).status_code==409
    assert client.post('/api/credits/topups',headers={'Origin':'https://evil.test'},json={}).status_code==403
    client.post('/api/auth/logout',json={});signup(client,'other@example.test')
    assert client.get('/api/credits/topups/'+inv['id']).status_code==404
    assert client.get('/api/credits').json()['balance_micro']==0

def test_disabled_terms_strict_amount_and_unsigned_only(client,monkeypatch):
    signup(client);payer=str(Keypair().pubkey())
    for amount in (True,1.5,'100',99,50001):
        assert client.post('/api/credits/topups',json={'amount_cents':amount,'payer':payer,'accepted_terms':True}).status_code==422
    assert client.put('/api/credits/settings',json={'enabled':True,'daily_limit_micro':1000000}).status_code==422
    inv=topup(client)
    def rpc(method,params=None):
        if method=='getGenesisHash':return pay.GENESIS
        if method=='getLatestBlockhash':return {'value':{'blockhash':str(Hash.default()),'lastValidBlockHeight':123}}
        raise AssertionError(method)
    monkeypatch.setattr(credit_api,'payment_rpc',rpc)
    r=client.post('/api/credits/topups/'+inv['id']+'/transaction',json={})
    assert r.status_code==200,r.text
    tx=Transaction.from_bytes(base64.b64decode(r.json()['transaction']))
    assert all(s==Signature.default() for s in tx.signatures)
    assert client.get('/api/credits').json()['balance_micro']==0
    monkeypatch.setenv('TICKING_CREDITS_ENABLED','false')
    assert client.post('/api/credits/topups',json={'amount_cents':100,'payer':payer,'accepted_terms':True}).status_code==503

def test_request_id_retry_exact_bytes_and_independent_plan_meter(client,monkeypatch):
    user,k=setup(client,monkeypatch)
    initial=client.get('/api/credits').json()['balance_micro']
    first=read(client,k);assert first.status_code==200,first.text
    assert first.headers['x-ticking-charged-micro-usdc']=='5'
    replay=read(client,k);assert replay.content==first.content
    assert replay.headers['x-ticking-charged-micro-usdc']=='0' and replay.headers['x-ticking-replayed']=='true'
    assert read(client,k,market='BONK').status_code==409
    state=client.get('/api/console').json()
    assert state['requests']==0 and state['credits']['balance_micro']==initial-5 and state['credits']['reserved_micro']==0
    assert ledger.reconcile()['passed']

@pytest.mark.parametrize('fault',['empty','stale','upstream','oversized','nonfinite'])
def test_failed_results_release_holds_without_charge(client,monkeypatch,fault):
    user,k=setup(client,monkeypatch);initial=ledger.snapshot(user['id'])['balance_micro']
    def bad(**kwargs):
        if fault=='upstream':raise HTTPException(503,'test-only outage')
        return {'candles':[] if fault=='empty' else [{'time':1,'close':float('nan') if fault=='nonfinite' else 1}],
                'stale':fault=='stale','extra':'x'*70000 if fault=='oversized' else ''}
    monkeypatch.setattr(api.engine_data,'candles',bad)
    kind='candles' if fault=='oversized' else 'price'
    r=read(client,k,kind=kind)
    assert r.status_code>=400,r.text
    state=ledger.snapshot(user['id']);assert state['balance_micro']==initial and state['reserved_micro']==0
    assert ledger.reconcile()['passed']

def test_daily_cap_revocation_disable_and_cache_expiry(client,monkeypatch):
    user,k=setup(client,monkeypatch)
    ledger.configure(user['id'],True,5,True)
    assert read(client,k).status_code==200
    assert read(client,k,'request-0002').status_code==429
    assert client.put('/api/credits/settings',json={'enabled':True,'daily_limit_micro':1,'accepted_terms':True}).status_code==422
    with store.db() as c:c.execute('UPDATE credit_requests SET created=created-90000')
    assert read(client,k).status_code==410
    assert ledger.snapshot(user['id'])['today_spent_micro']==5
    assert client.delete('/api/keys/'+k['id']).status_code==200
    assert read(client,k).status_code==401
    assert ledger.reconcile()['passed']

def test_concurrent_reservations_cannot_overspend_and_restarts_release(client,monkeypatch):
    user=signup(client);k=key(client,['candles:read'])
    ledger.configure(user['id'],True,1000,True)
    # Tiny synthetic balance in an isolated test DB; never a production deposit.
    with store.db() as c:ledger._post(c,user['id'],'topup','fixture:tiny-balance',100,{})
    def attempt(n):
        try:return ledger.reserve(user['id'],k['id'],'parallel-'+str(n),'candles',{'n':n})
        except ledger.CreditError as e:return e.code
    with ThreadPoolExecutor(max_workers=8) as pool:rows=list(pool.map(attempt,range(24)))
    wins=[r for r in rows if isinstance(r,dict)];assert len(wins)==1
    assert ledger.snapshot(user['id'])['available_micro']==0
    store.initialize() # Restart migration must preserve the durable reservation.
    assert ledger.snapshot(user['id'])['reserved_micro']==100
    with store.db() as c:c.execute('UPDATE credit_requests SET expires=0')
    state=ledger.snapshot(user['id']);assert state['available_micro']==100 and state['reserved_micro']==0
    assert ledger.reconcile()['passed']

def test_concurrent_identical_id_one_commit_and_atomic_rollback(client,monkeypatch):
    user,k=setup(client,monkeypatch)
    def attempt(_):
        try:return ledger.reserve(user['id'],k['id'],'same-request-id','price',{'market':'SOL'})
        except ledger.CreditError as e:return e.code
    with ThreadPoolExecutor(max_workers=8) as pool:rows=list(pool.map(attempt,range(16)))
    winners=[r for r in rows if isinstance(r,dict)];assert len(winners)==1
    held=winners[0];balance=ledger.snapshot(user['id'])['balance_micro']
    original=ledger._post
    def interrupt(*args,**kwargs):original(*args,**kwargs);raise RuntimeError('Injected failure before commit')
    with monkeypatch.context() as patch:
        patch.setattr(ledger,'_post',interrupt)
        with pytest.raises(RuntimeError):ledger.complete(held['id'],{'price':1})
    assert ledger.snapshot(user['id'])['balance_micro']==balance
    assert ledger.reconcile()['passed']
    completed=ledger.complete(held['id'],{'price':1});assert completed['charged_micro']==5
    with pytest.raises(ledger.CreditError):ledger.complete(held['id'],{'price':1})
    assert ledger.snapshot(user['id'])['balance_micro']==balance-5
    assert ledger.reconcile()['passed']

@pytest.mark.parametrize('cancel',['key','settings'])
def test_revocation_during_work_is_not_charged(client,monkeypatch,cancel):
    user,k=setup(client,monkeypatch);row=ledger.reserve(user['id'],k['id'],'cancel-read-0001','price',{})
    if cancel=='key':client.delete('/api/keys/'+k['id'])
    else:ledger.configure(user['id'],False,1000000,False)
    with pytest.raises(ledger.CreditError):ledger.complete(row['id'],{'price':1})
    ledger.release(row['id']);assert ledger.snapshot(user['id'])['today_spent_micro']==0
    assert ledger.reconcile()['passed']

def test_append_only_journal_and_byte_price_boundaries(client,monkeypatch):
    user,k=setup(client,monkeypatch)
    for sql in ('DELETE FROM credit_entries','UPDATE credit_entries SET delta_micro=1','DELETE FROM credit_journals','UPDATE credit_journals SET metadata=\'{}\'','DELETE FROM credit_receipts'):
        with pytest.raises(sqlite3.IntegrityError):
            with store.db() as c:c.execute(sql)
    row=ledger.reserve(user['id'],k['id'],'bytes-page-0001','candles',{})
    done=ledger.complete(row['id'],{'candles':['x'*32760]})
    assert done['charged_micro']==100
    assert ledger.snapshot(user['id'])['reserved_micro']==0
    assert ledger.reconcile()['passed']

def test_insufficient_credit_missing_id_and_scope(client,monkeypatch):
    user=signup(client);k=key(client,['candles:read'])
    monkeypatch.setattr(api.engine_data.global_data,'chosen',lambda *a:None)
    assert read(client,k).status_code==403
    ledger.configure(user['id'],True,1000000,True)
    assert read(client,k).status_code==402
    assert read(client,k,'short').status_code==422
    assert client.get('/api/v1/prepaid/price?market=SOL').status_code==401
    wrong=key(client,['markets:read']);assert read(client,wrong).status_code==403
    assert ledger.reconcile()['passed']

def test_abandoned_request_retry_releases_durable_hold(client,monkeypatch):
    user,k=setup(client,monkeypatch)
    row=ledger.reserve(user['id'],k['id'],'abandoned-read','price',{'market':'SOL'})
    with store.db() as c:c.execute('UPDATE credit_requests SET expires=0 WHERE id=?',(row['id'],))
    with pytest.raises(ledger.CreditError) as error:ledger.reserve(user['id'],k['id'],'abandoned-read','price',{'market':'SOL'})
    assert error.value.code=='request_released'
    with store.db() as c:assert c.execute('SELECT reserved_micro FROM credit_accounts WHERE user_id=?',(user['id'],)).fetchone()['reserved_micro']==0
    assert ledger.reconcile()['passed']

def test_payment_budget_fails_closed_and_reserves_slot(tmp_path,monkeypatch):
    path=tmp_path/'budget.json';monkeypatch.setenv('TICKING_PAYMENT_RPC_BUDGET',str(path));calls=[]
    monkeypatch.setattr(pay,'rpc',lambda method,params=None:calls.append(method) or pay.GENESIS)
    with pytest.raises(pay.PaymentError):credit_api.payment_rpc('getGenesisHash')
    assert calls==[]
    path.write_text(json.dumps({'next':time.time(),'cooldown':0,'preserve':'existing provider state'}))
    assert credit_api.payment_rpc('getGenesisHash')==pay.GENESIS
    assert json.loads(path.read_text())['next']>time.time()+1
    assert json.loads(path.read_text())['preserve']=='existing provider state'
    path.write_text(json.dumps({'next':time.time(),'cooldown':time.time()+100}))
    with pytest.raises(pay.PaymentError):credit_api.payment_rpc('getGenesisHash')
    assert calls==['getGenesisHash']
    assert json.loads(path.read_text())['cooldown']>time.time()+99
    path.write_text(json.dumps({'next':0,'cooldown':0,'preserve':'shared'}))
    def limited(*args,**kwargs):
        response=httpx.Response(429,headers={'Retry-After':'60'},request=httpx.Request('POST','https://rpc.example.test'))
        try:response.raise_for_status()
        except httpx.HTTPStatusError as cause:raise pay.PaymentError('fixture-only rate limit') from cause
    monkeypatch.setattr(pay,'rpc',limited)
    with pytest.raises(pay.PaymentError):credit_api.payment_rpc('getSignatureStatuses')
    state=json.loads(path.read_text());assert state['cooldown']>time.time()+59 and state['preserve']=='shared'
