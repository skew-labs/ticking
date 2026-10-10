"""Explicit prepaid reads and finalized USDC top-ups; existing plans stay separate."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import time
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from solders.pubkey import Pubkey
from solders.signature import Signature
from . import credits, payments, engine_data
from .store import db


def enabled():
    return os.environ.get('TICKING_CREDITS_ENABLED')=='true'


def payment_rpc(method,params=None):
    # Coordinate occasional payment checks with the existing shared read budget.
    # Admission fails closed on a missing/unwritable budget, without sending RPC.
    path=Path(os.environ.get('TICKING_PAYMENT_RPC_BUDGET','./engine/data/solana-history/rpc-budget.json'))
    try:
        with path.open('r+') as file:
            fcntl.flock(file,fcntl.LOCK_EX)
            state=json.load(file);stamp=time.time()
            if not isinstance(state,dict):raise ValueError('invalid budget')
            previous=state.get('next',0);cooldown=state.get('cooldown',0)
            if any(not isinstance(value,(int,float)) or not math.isfinite(value) for value in (previous,cooldown)):raise ValueError('invalid budget')
            previous=max(previous,cooldown)
            start=max(stamp,previous)
            if start-stamp>20:raise ValueError('busy budget')
            state['next']=start+2
            file.seek(0);json.dump(state,file);file.truncate();file.flush()
        time.sleep(max(0,start-time.time()))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise payments.PaymentError('Payment verification is busy. Retry this payment shortly; do not send again.') from exc
    try:
        return payments.rpc(method,params)
    except payments.PaymentError as exc:
        response=getattr(exc.__cause__,'response',None)
        if getattr(response,'status_code',None)==429:
            try:
                retry=float(response.headers.get('Retry-After','30'))
                if not math.isfinite(retry) or retry<0:retry=30
            except (ValueError,TypeError):retry=30
            try:
                with path.open('r+') as file:
                    fcntl.flock(file,fcntl.LOCK_EX);state=json.load(file)
                    state['cooldown']=max(state.get('cooldown',0),time.time()+max(30,retry))
                    file.seek(0);json.dump(state,file);file.truncate();file.flush()
            except (OSError,ValueError,KeyError,TypeError):
                pass # Preserve the payment error; no receipt or credit is committed.
        raise


class TopupCreate(BaseModel):
    amount_cents:int=Field(strict=True,ge=100,le=50000)
    payer:str=Field(min_length=32,max_length=44)
    accepted_terms:bool=False


class Settings(BaseModel):
    enabled:bool
    daily_limit_micro:int=Field(strict=True,ge=5,le=1_000_000_000)
    accepted_terms:bool=False
    stream_enabled:bool=False


class Verify(BaseModel):
    signature:str=Field(min_length=64,max_length=88)


def install(app,session_user,api_key,rate):
    def ready():
        if not enabled():raise HTTPException(503,'Prepaid data credits are temporarily unavailable.')

    def owned(ident,request):
        user=session_user(request)
        with db() as c:
            row=c.execute("SELECT * FROM invoices WHERE id=? AND user_id=? AND purpose='credits'",(ident,user['id'])).fetchone()
        if not row:raise HTTPException(404,'Credit payment not found.')
        return dict(row)

    def public(row):
        return {k:row.get(k) for k in ('id','amount','status','created','expires','payer','reference','recipient','mint','signature','paid_at')}|{'purpose':'credits','plan':'Service credits','network':'solana:mainnet','credit_micro':row['amount']*10000,'price_version':credits.VERSION}

    @app.exception_handler(credits.CreditError)
    async def credit_error(request,error):
        headers={'Cache-Control':'no-store'}
        if error.code=='request_in_progress':headers['Retry-After']='2'
        return JSONResponse({'detail':error.message,'code':error.code},status_code=error.status,headers=headers)

    @app.get('/api/credits/rates')
    def rates():
        return {'available':enabled(),'currency':'USDC','decimals':6,'price_version':credits.VERSION,'products':credits.RATES,'min_topup_cents':100,'max_topup_cents':50000,'scope':'Retained Solana pool data in native quote units; partial coverage and provisional observations. Streaming follows retained-store updates; no upstream latency SLA.','x402_enabled':False,'zoneless_connected':False}

    @app.get('/api/credits')
    def balance(request:Request):
        return {'available':enabled(),**credits.snapshot(session_user(request)['id'])}

    @app.put('/api/credits/settings')
    def settings(body:Settings,request:Request):
        ready();user=session_user(request);rate('credit-settings:'+user['id'],20,3600)
        return credits.configure(user['id'],body.enabled,body.daily_limit_micro,body.accepted_terms,body.stream_enabled)

    @app.post('/api/credits/topups')
    def topup(body:TopupCreate,request:Request):
        ready();user=session_user(request);rate('credit-topup:'+user['id'],10,3600)
        if not payments.crypto_ready():raise HTTPException(503,'Wallet checkout is temporarily unavailable.')
        if not body.accepted_terms:raise HTTPException(422,'Confirm the service credit scope before paying.')
        try:
            if not Pubkey.from_string(body.payer).is_on_curve():raise ValueError()
        except ValueError:raise HTTPException(422,'Connect a valid Solana payer wallet.')
        if body.payer==payments.RECIPIENT:raise HTTPException(422,'The receiving wallet cannot pay itself.')
        stamp=int(time.time())
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM invoices WHERE user_id=? AND purpose='credits' AND amount=? AND payer=? AND status='pending' AND expires>?",(user['id'],body.amount_cents,body.payer,stamp)).fetchone()
            if row:return public(dict(row))
            row={'id':'inv_'+secrets.token_hex(12),'user_id':user['id'],'plan':'Service credits','amount':body.amount_cents,'method':'USDC · Solana','status':'pending','created':stamp,'expires':stamp+1800,'payer':body.payer,'reference':str(Pubkey.from_bytes(secrets.token_bytes(32))),'recipient':payments.RECIPIENT,'mint':payments.MINT}
            c.execute("INSERT INTO invoices(id,user_id,plan,amount,method,status,created,expires,payer,reference,recipient,mint,purpose) VALUES(:id,:user_id,:plan,:amount,:method,:status,:created,:expires,:payer,:reference,:recipient,:mint,'credits')",row)
        return public(row)

    @app.get('/api/credits/topups/{ident}')
    def get_topup(ident:str,request:Request):return public(owned(ident,request))

    @app.post('/api/credits/topups/{ident}/transaction')
    def transaction(ident:str,request:Request):
        ready();row=owned(ident,request);rate('credit-prepare:'+row['user_id'],10);rate('credit-rpc-global',6)
        if not payments.crypto_ready():raise HTTPException(503,'Wallet checkout is temporarily unavailable.')
        if row['status']!='pending' or row['expires']<time.time():raise HTTPException(409,'This credit payment is paid or its payment window expired.')
        if row['signature']:raise HTTPException(409,'Verify the submitted transaction; do not pay again.')
        try:return payments.unsigned_transaction(row,rpc_fn=payment_rpc)
        except payments.PaymentError as e:raise HTTPException(503,str(e))

    @app.post('/api/credits/topups/{ident}/verify')
    def verify(ident:str,body:Verify,request:Request):
        ready();row=owned(ident,request)
        if row['status']=='paid':return public(row)
        rate('credit-verify:'+row['user_id'],6);rate('credit-rpc-global',6)
        if row['status']=='failed':raise HTTPException(409,'This payment failed on Solana. Create a new top-up to retry.')
        try:Signature.from_string(body.signature)
        except ValueError:raise HTTPException(422,'Invalid Solana transaction signature.')
        if row['signature'] and row['signature']!=body.signature:raise HTTPException(409,'Verify the existing transaction before retrying payment.')
        try:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                old=c.execute('SELECT signature FROM invoices WHERE id=?',(ident,)).fetchone()['signature']
                if old and old!=body.signature:raise HTTPException(409,'Another transaction is associated with this payment.')
                c.execute('UPDATE invoices SET signature=? WHERE id=? AND signature IS NULL',(body.signature,ident))
            proof=payments.verify_invoice(row,body.signature,rpc_fn=payment_rpc,evidence=True)
        except sqlite3.IntegrityError:raise HTTPException(409,'This transaction is already associated with another payment.')
        except payments.FailedTransaction as e:
            with db() as c:c.execute("UPDATE invoices SET status='failed' WHERE id=? AND status='pending'",(ident,))
            raise HTTPException(422,str(e))
        except payments.PaymentError as e:raise HTTPException(422,str(e))
        if not proof:return {'id':ident,'purpose':'credits','status':'confirming','signature':body.signature,'message':'Waiting for finalized Solana confirmation. Do not pay again.'}
        credits.finalize_topup(ident,proof)
        return public(owned(ident,request))

    def paid_read(request,product,query,operation):
        ready();key=api_key(request,'trades:read' if product=='trades' else 'candles:read');rate('prepaid-user:'+key['user_id'],600)
        row=credits.reserve(key['user_id'],key['id'],request.headers.get('Idempotency-Key'),product,query)
        replay=row['status']=='completed'
        if not replay:
            try:
                data=operation()
                if product!='price' and not data.get(product):raise HTTPException(409,'No retained records are available for this selection. No charge was made.')
                row=credits.complete(row['id'],data)
            except Exception:
                credits.release(row['id']);raise
        if row['response'] is None or row['created']+credits.REPLAY_SECONDS<int(time.time()):raise HTTPException(410,'This previously charged result is outside the 24-hour replay window. Use a new request ID for a new read.')
        if hashlib.sha256(row['response'].encode()).hexdigest()!=row['response_sha256']:raise HTTPException(503,'This read needs reconciliation. No new charge was made.')
        return Response(row['response'],media_type='application/json',headers={'X-Ticking-Request-ID':row['id'],'X-Ticking-Cost-Micro-USDC':str(row['charged_micro']),'X-Ticking-Charged-Micro-USDC':'0' if replay else str(row['charged_micro']),'X-Ticking-Price-Version':row['price_version'],'X-Ticking-Replayed':str(replay).lower(),'Cache-Control':'no-store'})

    def selection(market,mint,pool):
        if engine_data.global_data.chosen(market):
            if mint or pool:raise HTTPException(422,'Use one exchange instrument without a Solana selector.')
            return {'market':market,'aggregation':'exchange'}
        if not (market or mint or pool):raise HTTPException(422,'Choose a Solana market, mint or pool.')
        return {'market':market,'mint':mint,'pool':pool,'aggregation':'pool'}

    @app.get('/api/v1/prepaid/price')
    def price(request:Request,market:str|None=None,mint:str|None=None,pool:str|None=None):
        query=selection(market,mint,pool)
        def read():
            data=engine_data.candles(**query,interval='1m',limit=1,gap_policy='recorded',history='executions')
            if not data.get('candles') or data.get('stale'):raise HTTPException(409,'A fresh retained pool price is unavailable. No charge was made.')
            bar=data['candles'][-1];value=bar.get('close')
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:raise HTTPException(503,'The pool price is unavailable. No charge was made.')
            if not isinstance(bar.get('time'),(int,float)) or time.time()-bar['time']/1000>240:raise HTTPException(409,'The last retained price candle is too old. No charge was made.')
            return {k:data.get(k) for k in ('market','pool','mint','quote','quoteMint','identity','updated_at','interval','source')}|{'price':value,'bar_time':bar['time'],'mode':'retained_pool_reference','live':False,'complete':False,'coverage':data.get('coverage'),'definition':'Last recorded 1-minute candle close in its native quote units; not an executable quote.'}
        return paid_read(request,'price',query,read)

    @app.get('/api/v1/prepaid/candles')
    def candles(request:Request,market:str|None=None,mint:str|None=None,pool:str|None=None,interval:str='1m',limit:int=100,before:int|None=None):
        if not 1<=limit<=100:raise HTTPException(422,'Choose 1–100 candles per page.')
        query=selection(market,mint,pool)|{'interval':interval,'limit':limit,'before':before}
        return paid_read(request,'candles',query,lambda:engine_data.candles(**query,gap_policy='recorded',history='executions'))

    @app.get('/api/v1/prepaid/trades')
    def trades(request:Request,market:str|None=None,mint:str|None=None,pool:str|None=None,limit:int=100):
        if not 1<=limit<=100:raise HTTPException(422,'Choose 1–100 trades per page.')
        query=selection(market,mint,pool)|{'limit':limit}
        return paid_read(request,'trades',query,lambda:engine_data.trades(**query))
