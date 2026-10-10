import copy
import json
import time
import pytest
from fastapi.testclient import TestClient
from solders.keypair import Keypair
from solders.signature import Signature
from solders.hash import Hash
from solders.transaction import Transaction
from . import app as api
from . import payments as pay
from . import store

ORIGIN={'Origin':'https://ticking.dev'}

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'console.sqlite3')
    monkeypatch.setattr(pay,'RECIPIENT','7aFkGZYvx3UUpK5duxpjEFExHhP9wy4gN62mbvvHfh81')
    monkeypatch.setattr(api,'RECIPIENT',pay.RECIPIENT)
    monkeypatch.setenv('TICKING_CRYPTO_ENABLED','true')
    monkeypatch.setattr(api,'crypto_ready',lambda:True)
    with TestClient(api.app,base_url='https://ticking.dev',headers=ORIGIN) as c:
        yield c

def signup(client,email='dev@example.test'):
    r=client.post('/api/auth/signup',json={'email':email,'password':'correct-horse-battery-123','name':'My project'})
    assert r.status_code==200,r.text
    return r.json()

def key(client,scopes=None):
    r=client.post('/api/keys',json={'name':'Backend','scopes':scopes or ['markets:read','candles:read','mcp:read']})
    assert r.status_code==200,r.text
    return r.json()

def invoice(client,payer=None):
    payer=payer or str(Keypair().pubkey())
    r=client.post('/api/billing/invoices',json={'plan':'Data','payer':payer,'accepted_preview':True,'amount':1})
    assert r.status_code==200,r.text
    return r.json()

def receipt(inv):
    dest=str(pay.associated(pay.RECIPIENT))
    tx={'blockTime':inv['created']+1,'transaction':{'message':{'accountKeys':[{'pubkey':inv['payer'],'signer':True},{'pubkey':dest,'signer':False},{'pubkey':inv['reference'],'signer':False}],'instructions':[{'programId':pay.TOKEN,'parsed':{'type':'transferChecked','info':{'mint':pay.MINT,'authority':inv['payer'],'destination':dest,'tokenAmount':{'amount':str(inv['amount']*10000),'decimals':6}}}}]}},'meta':{'err':None,'preTokenBalances':[],'postTokenBalances':[{'accountIndex':1,'mint':pay.MINT,'owner':pay.RECIPIENT,'uiTokenAmount':{'amount':str(inv['amount']*10000),'decimals':6}}],'innerInstructions':[]}}
    return tx,{'err':None,'confirmationStatus':'finalized'}

def verified_receipt(inv, signature, **kwargs):
    transaction, status = receipt(inv)
    return {'signature':signature,'genesis':pay.GENESIS,'transaction':transaction,'status':status}

def test_auth_cookie_csrf_and_tenant_isolation(client):
    assert client.get('/api/console').status_code==401
    user=signup(client)
    cookie=client.cookies.get('ticking_session');assert cookie
    k=key(client)
    snapshot=client.get('/api/console').json()
    assert k['key'] not in json.dumps(snapshot)
    with store.db() as c:
        stored=c.execute('SELECT * FROM api_keys').fetchone()
        assert stored['hash']!=k['key'] and k['key'] not in str(tuple(stored))
        assert c.execute('SELECT password FROM users').fetchone()['password']!='correct-horse-battery-123'
    assert client.post('/api/keys',headers={'Origin':'https://evil.test'},json={'name':'Bad','scopes':['markets:read']}).status_code==403
    client.post('/api/auth/logout',json={});assert client.get('/api/console').status_code==401
    signup(client,'second@example.test')
    assert client.delete('/api/keys/'+k['id']).status_code==404
    assert client.get('/api/console').json()['keys']==[]
    login=client.post('/api/auth/login',json={'email':'dev@example.test','password':'correct-horse-battery-123'})
    assert login.status_code==200
    assert client.cookies.get('ticking_session')!=cookie

def test_key_scopes_revocation_and_usage(client,monkeypatch):
    monkeypatch.setattr(api, 'markets_data', lambda *a: {'mode':'executed_history','markets':[]})
    signup(client);k=key(client,['markets:read']);auth={'Authorization':'Bearer '+k['key']}
    assert client.get('/api/v1/markets',headers=auth).json()['mode']=='executed_history'
    assert client.get('/api/v1/candles',headers=auth).status_code==403
    assert client.get('/api/console').json()['requests']==1
    assert client.delete('/api/keys/'+k['id']).status_code==200
    assert client.get('/api/v1/markets',headers=auth).status_code==401
    assert client.get('/api/console').json()['requests']==1

def test_key_limits_and_invalid_scopes(client):
    signup(client)
    assert client.post('/api/keys',json={'name':'x','scopes':['billing:write']}).status_code==422
    key(client);key(client)
    assert client.post('/api/keys',json={'name':'third','scopes':['markets:read']}).status_code==409

def test_mcp_missing_store_is_error_not_generated_candles(client):
    signup(client);k=key(client);auth={'Authorization':'Bearer '+k['key']}
    init=client.post('/api/mcp',headers=auth,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}})
    assert init.json()['result']['serverInfo']['name']=='ticking'
    tools=client.post('/api/mcp',headers=auth,json={'jsonrpc':'2.0','id':2,'method':'tools/list'}).json()
    assert len(tools['result']['tools'])==2
    call=client.post('/api/mcp',headers=auth,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_candles','arguments':{'market':'SOL-USDC','interval':'3s','limit':5}}}).json()
    assert call['result']['isError'] is True
    assert 'unavailable' in call['result']['content'][0]['text']
    assert client.post('/api/mcp',headers=auth,json={'jsonrpc':'2.0','id':4,'method':'tools/call','params':[]}).status_code==200
    assert client.get('/api/v1/candles?interval=0s',headers=auth).status_code==422
    assert client.get('/api/v1/candles?limit=10000',headers=auth).status_code==422

def test_feed_saved_and_owned(client,monkeypatch):
    from fastapi import HTTPException
    def resolve(**kwargs):
        if kwargs.get('market') != 'SOL-USDC': raise HTTPException(422,'Unknown fixture market')
        return {'ready':True}
    monkeypatch.setattr(api.engine_data, 'resolve', resolve)
    signup(client)
    body={'name':'SOL seconds','markets':['SOL-USDC'],'interval':'3s'}
    feed=client.post('/api/feeds',json=body).json()
    assert feed['live'] is False
    assert client.get('/api/console').json()['feeds'][0]['interval']=='3s'
    assert client.post('/api/feeds',json={**body,'markets':['FAKE-USDC']}).status_code==422
    client.post('/api/auth/logout',json={});signup(client,'other@example.test')
    assert client.delete('/api/feeds/'+feed['id']).status_code==404

def test_invoice_server_price_idempotency_and_ownership(client):
    signup(client);inv=invoice(client)
    assert inv['amount']==5900
    repeated=client.post('/api/billing/invoices',json={'plan':'Data','payer':inv['payer'],'accepted_preview':True}).json()
    assert repeated['id']==inv['id']
    assert client.post('/api/billing/invoices',json={'plan':'Data','payer':inv['payer']}).status_code==422
    client.post('/api/auth/logout',json={});signup(client,'other@example.test')
    assert client.get('/api/billing/invoices/'+inv['id']).status_code==404

def test_receipt_validation(client):
    signup(client);inv=invoice(client);tx,status=receipt(inv)
    assert pay.validate_receipt(inv,tx,status)
    assert not pay.validate_receipt(inv,tx,{'err':None,'confirmationStatus':'confirmed'})
    cases=[]
    wrong=copy.deepcopy(tx);wrong['meta']['err']={'InstructionError':[0,'failed']};cases.append(wrong)
    wrong=copy.deepcopy(tx);wrong['transaction']['message']['accountKeys'][2]['pubkey']=str(Keypair().pubkey());cases.append(wrong)
    wrong=copy.deepcopy(tx);wrong['transaction']['message']['instructions'][0]['parsed']['info']['mint']=str(Keypair().pubkey());cases.append(wrong)
    wrong=copy.deepcopy(tx);wrong['transaction']['message']['instructions'][0]['parsed']['info']['tokenAmount']['amount']='1';cases.append(wrong)
    wrong=copy.deepcopy(tx);wrong['transaction']['message']['instructions'][0]['parsed']['info']['authority']=str(Keypair().pubkey());cases.append(wrong)
    wrong=copy.deepcopy(tx);wrong['meta']['postTokenBalances'][0]['owner']=str(Keypair().pubkey());cases.append(wrong)
    wrong=copy.deepcopy(tx);wrong['meta']['postTokenBalances'][0]['uiTokenAmount']['amount']='1';cases.append(wrong)
    wrong=copy.deepcopy(tx);wrong['blockTime']=inv['expires']+1;cases.append(wrong)
    for wrong in cases:
        with pytest.raises(pay.PaymentError):pay.validate_receipt(inv,wrong,status)
    with pytest.raises(pay.PaymentError):pay.validate_receipt(inv,tx,{'err':'failed','confirmationStatus':'finalized'})

def test_finalized_credit_once_and_no_signature_reuse(client,monkeypatch):
    signup(client);inv=invoice(client);signature=str(Keypair().sign_message(b'fixture'))
    monkeypatch.setattr(api,'verify_invoice',lambda *a,**k:False)
    assert client.post('/api/billing/invoices/'+inv['id']+'/verify',json={'signature':signature}).json()['status']=='confirming'
    assert client.get('/api/console').json()['plan']['plan']=='Explore'
    monkeypatch.setattr(api,'verify_invoice',verified_receipt)
    for _ in range(2):assert client.post('/api/billing/invoices/'+inv['id']+'/verify',json={'signature':signature}).json()['status']=='paid'
    with store.db() as c: assert c.execute('SELECT COUNT(*) n FROM entitlements').fetchone()['n']==1
    assert client.get('/api/console').json()['plan']['plan']=='Data'
    another=invoice(client)
    assert client.post('/api/billing/invoices/'+another['id']+'/verify',json={'signature':signature}).status_code==409

def test_unsigned_transaction_structure(client,monkeypatch):
    signup(client);inv=invoice(client)
    def rpc(method,params=None):
        if method=='getGenesisHash':return pay.GENESIS
        if method=='getLatestBlockhash':return {'value':{'blockhash':str(Hash.default()),'lastValidBlockHeight':123}}
        raise AssertionError(method)
    monkeypatch.setattr(pay,'rpc',rpc)
    prepared=pay.unsigned_transaction(inv)
    import base64,struct
    tx=Transaction.from_bytes(base64.b64decode(prepared['transaction']))
    assert len(tx.message.instructions)==3
    instruction=tx.message.instructions[1]
    assert struct.unpack('<BQB',instruction.data)==(12,59000000,6)
    assert str(tx.message.account_keys[instruction.accounts[-1]])==inv['reference']
    assert all(s==Signature.default() for s in tx.signatures)
    assert tx.message.header.num_required_signatures==1
    assert str(tx.message.account_keys[0])==inv['payer']
    create=tx.message.instructions[0]
    assert str(tx.message.account_keys[create.accounts[2]])==inv['recipient']
    assert str(tx.message.account_keys[create.accounts[1]])==str(pay.associated(inv['recipient']))
    monkeypatch.setattr(pay,'rpc',lambda *_:'wrong-network')
    with pytest.raises(pay.PaymentError):pay.unsigned_transaction(inv)

def test_quota_enforced(client,monkeypatch):
    monkeypatch.setattr(api, 'markets_data', lambda *a: {'mode':'executed_history','markets':[]})
    signup(client);k=key(client);auth={'Authorization':'Bearer '+k['key']}
    monkeypatch.setitem(api.PLANS['Explore'],'requests',1)
    assert client.get('/api/v1/markets',headers=auth).status_code==200
    assert client.get('/api/v1/markets',headers=auth).status_code==429


def test_invalid_mcp_args_and_version(client):
    signup(client);k=key(client);headers={'Authorization':'Bearer '+k['key']}
    response=client.post('/api/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}})
    assert response.json()['result']['protocolVersion']=='2025-03-26'
    response=client.post('/api/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_candles','arguments':['bad']}})
    assert response.status_code==200 and response.json()['result']['isError']


def test_failed_invoice_allows_fresh_invoice(client,monkeypatch):
    signup(client);inv=invoice(client)
    def failed(*args,**kwargs): raise pay.FailedTransaction('Failed transaction')
    monkeypatch.setattr(api,'verify_invoice',failed)
    assert client.post('/api/billing/invoices/'+inv['id']+'/verify',json={'signature':str(Signature.new_unique())}).status_code==422
    assert client.get('/api/billing/invoices/'+inv['id']).json()['status']=='failed'
    assert invoice(client,inv['payer'])['id']!=inv['id']
    monkeypatch.setattr(api,'RECIPIENT',str(Keypair().pubkey()))
    assert client.get('/api/billing/invoices/'+inv['id']).json()['recipient']==inv['recipient']


def test_console_request_obeys_quota(client):
    user=signup(client)
    with store.db() as c:
        c.execute('INSERT INTO usage_events(user_id,endpoint,created,units) VALUES(?,?,?,?)',(user['id'],'/fixture',api.now(),10000))
    assert client.post('/api/console/test-request',json={}).status_code==429


def test_rpc_reference_account_parser_shape(client):
    signup(client);inv=invoice(client);tx,status=receipt(inv)
    info=tx['transaction']['message']['instructions'][0]['parsed']['info']
    info['multisigAuthority']=info.pop('authority');info['signers']=[inv['reference']]
    assert pay.validate_receipt(inv,tx,status)
    tx['transaction']['message']['accountKeys'][0]['signer']=False
    with pytest.raises(pay.PaymentError):pay.validate_receipt(inv,tx,status)


def test_squads_vault_recipient_readiness_and_checkout(client,monkeypatch):
    monkeypatch.setattr(api,'crypto_ready',pay.crypto_ready)
    monkeypatch.setenv('TICKING_RECIPIENT_KIND','squads-v4')
    monkeypatch.setenv('TICKING_SQUADS_MULTISIG','5nxmzC5ZMPEhAhL51eT1Nfnr6A5oVse4knw8TG8jaVb5')
    monkeypatch.setenv('TICKING_SQUADS_VAULT_INDEX','0')
    assert pay.crypto_ready()
    assert client.get('/api/config').json()['payments']['crypto'] is True
    signup(client)
    inv=invoice(client)
    assert inv['recipient']==pay.RECIPIENT
    assert str(pay.associated(inv['recipient']))=='5XaL5K36xjpRMcMmT5RFwmUQANeCvrJhXGTsSJ6ZWVY9'
    tx,status=receipt(inv)
    assert pay.validate_receipt(inv,tx,status)
    monkeypatch.setenv('TICKING_SQUADS_VAULT_INDEX','1')
    assert not pay.crypto_ready()
    assert client.post('/api/billing/invoices',json={'plan':'Charts','payer':inv['payer'],'accepted_preview':True}).status_code==503


@pytest.mark.parametrize('changes',[
    {'TICKING_RECIPIENT_KIND':'wallet'},
    {'TICKING_RECIPIENT_KIND':'unknown'},
    {'TICKING_SQUADS_MULTISIG':'invalid'},
    {'TICKING_SQUADS_VAULT_INDEX':'-1'},
    {'TICKING_SQUADS_VAULT_INDEX':'256'},
    {'TICKING_SQUADS_VAULT_INDEX':''},
    {'TICKING_CRYPTO_ENABLED':'false'},
])
def test_unconfigured_or_mismatched_squads_vault_stays_disabled(client,monkeypatch,changes):
    for key,value in {'TICKING_RECIPIENT_KIND':'squads-v4','TICKING_SQUADS_MULTISIG':'5nxmzC5ZMPEhAhL51eT1Nfnr6A5oVse4knw8TG8jaVb5','TICKING_SQUADS_VAULT_INDEX':'0',**changes}.items():
        monkeypatch.setenv(key,value)
    assert not pay.crypto_ready()
