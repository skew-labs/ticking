"""Bounded asynchronous reads of existing local wallet stores. No ingestion or RPC.

Public, network-qualified evidence only. Jobs deduplicate the exact request and
retain no sessions, customer account data, signing material, or model prompts.
"""
import copy
import hashlib
import json
import math
import queue
import re
import threading
import time
from collections import OrderedDict

ADDR=re.compile(r'^0x[0-9a-f]{40}$')
LOCAL=threading.local()


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def position_rows(node,chain,market_for,projector=None,sheet_held=False):
    """Never turn an ambiguous symbol, a debt, or held-back data into a holding."""
    sheet=node.get('balance_sheet') or {}
    raw=[p for p in sheet.get('positions',[]) if isinstance(p,dict)]
    if callable(projector):
        rows=[]
        for p in sorted(raw,key=lambda p:-abs(p['usd']) if finite(p.get('usd')) else 0)[:128]:
            row=projector(p,chain,sheet.get('as_of'),bool(sheet_held or sheet.get('held')))
            contract=row.get('token') if row.get('identityResolved') and not row.get('identityConflict') else None
            if not isinstance(contract,str) or not ADDR.fullmatch(contract) or contract in {'0x'+'0'*40,'0x'+'e'*40}:
                contract=None
            expected=chain+':'+contract if contract else None
            reviewed=market_for(chain,contract) if contract else None
            row.update(tokenAddress=contract,chartMarket=reviewed if reviewed==expected else None,
                       chartReviewed=bool(expected and reviewed==expected))
            if contract:
                row['identityBasis']='position_contract'
                if not row['chartReviewed']:row['identityReason']='no_reviewed_chart_for_contract'
            else:
                row['identityReason']='conflicting_position_identity' if row.get('identityConflict') else 'position_contract_not_recorded'
            rows.append(row)
        return rows,len(raw)
    symbols={}
    for token in node.get('top_tokens') or []:
        if not isinstance(token,dict):continue
        contract=str(token.get('token') or token.get('address') or '').lower()
        if ADDR.fullmatch(contract) and contract!='0x'+'0'*40:
            symbols.setdefault(str(token.get('symbol') or '').casefold(),set()).add(contract)
    rows=[]
    for p in sorted(raw,key=lambda p:-abs(p['usd']) if finite(p.get('usd')) else 0)[:128]:
        row={k:p[k] for k in ['kind','protocol','asset','held','heldCode','heldReason','amountRaw','decimals'] if k in p}
        row.update(amount=p.get('amount') if finite(p.get('amount')) else None,usd=p.get('usd') if finite(p.get('usd')) else None,
                   flag=p.get('q'),pricePending=isinstance(p.get('price'),str) or None)
        contract=next((str(p[k]).lower() for k in ['tokenAddress','token_address','asset_address','contract','token'] if isinstance(p.get(k),str) and ADDR.fullmatch(str(p[k]).lower())),None)
        if contract:row['identityBasis']='position_contract'
        else:
            candidates=symbols.get(str(p.get('asset') or '').casefold(),set())
            if len(candidates)==1:
                contract=next(iter(candidates));row['identityBasis']='unique_token_in_wallet_records'
            else:row['identityReason']='ambiguous_recorded_symbol' if candidates else 'position_contract_not_recorded'
        row.update(chain=chain,tokenAddress=contract,market=market_for(chain,contract) if contract else None)
        if contract and not row['market']:row['identityReason']='no_reviewed_chart_for_contract'
        rows.append(row)
    return rows,len(raw)


class ReadJobs:
    def __init__(self,execute,clock=time.monotonic,workers=2):
        self.execute=execute;self.clock=clock;self.lock=threading.Lock();self.jobs=OrderedDict();self.tasks=queue.Queue(maxsize=8);self.workers=workers;self.started=False

    def _start(self):
        if self.started:return
        self.started=True
        for i in range(self.workers):threading.Thread(target=self._worker,name='wallet-read-'+str(i),daemon=True).start()

    @staticmethod
    def identity(kind,chain,address,target=None):return hashlib.sha256(json.dumps([kind,chain,address,target],separators=(',',':')).encode()).hexdigest()[:32]

    def submit(self,kind,chain,address,target=None):
        ident=self.identity(kind,chain,address,target)
        with self.lock:
            for key,job in list(self.jobs.items()):
                expiry=10 if job['state']=='failed' else 300
                if job['state'] not in {'queued','running'} and self.clock()-job['updated']>expiry:del self.jobs[key]
            if ident not in self.jobs:
                if len(self.jobs)>=64 or self.tasks.full():return 429,{'error':'Wallet read capacity is busy. Retry the same read shortly.','retryAfter':5}
                job={'id':ident,'kind':kind,'chain':chain,'address':address,'target':target,'state':'queued','created':self.clock(),'updated':self.clock(),'phase':'Queued for an existing source read'}
                self.jobs[ident]=job;self.tasks.put_nowait(ident);self._start()
            job=copy.deepcopy(self.jobs[ident])
        if job['state']=='complete':return 200,{'schema':'ticking.wallet-read.v1','state':'complete','job':ident,'result':job['result']}
        if job['state']=='failed':return 200,{'schema':'ticking.wallet-read.v1','state':'failed','job':ident,'status':job['code'],'error':job['error'],'chain':chain,'address':address}
        return 202,{'schema':'ticking.wallet-read.v1','state':job['state'],'job':ident,'kind':kind,'chain':chain,'address':address,'phase':job['phase'],'retryAfter':1}

    def _worker(self):
        while True:
            ident=self.tasks.get()
            try:
                with self.lock:
                    job=self.jobs[ident]
                    if self.clock()-job['created']>=65:
                        job.update(state='failed',code=503,error='Source queue budget reached. Retry this exact read; previous evidence is retained.',updated=self.clock());continue
                    job.update(state='running',phase='Reading retained '+job['kind']+' evidence',updated=self.clock())
                LOCAL.deadline=job['created']+65;LOCAL.calls=0
                value=self.execute(job['kind'],job['chain'],job['address'],job['target'])
                if len(json.dumps(value,allow_nan=False).encode())>524288:raise ValueError('Wallet response exceeds its bound')
                with self.lock:job.update(state='complete',result=value,updated=self.clock())
            except Exception as e:
                code=getattr(e,'code',503)
                with self.lock:self.jobs[ident].update(state='failed',code=code if code in (400,404,429,501,502,503,504) else 503,error=str(e)[:240] if code in (400,404,501) else 'This retained source read could not finish. Previous evidence remains available.',updated=self.clock())
            finally:
                LOCAL.deadline=None;self.tasks.task_done()


def install(env):
    original_find=env['find_wallet'];original_wallet=env['tool_wallet'];original_fetch=env['fetch'];original_galaxy=env['galaxy'];original_api=env['Handler'].api
    raw_nodes={};raw_lock=threading.Lock()

    def bounded_fetch(hp,path,timeout=20,headers=None):
        deadline=getattr(LOCAL,'deadline',None)
        if deadline:
            LOCAL.calls+=1;remaining=deadline-time.monotonic()
            if remaining<=0 or LOCAL.calls>16:raise env['HTTPError'](504,'Retained wallet read budget reached.')
            timeout=min(timeout,remaining)
        return original_fetch(hp,path,timeout,headers)

    def bounded_galaxy(path,ttl=60,timeout=20):
        deadline=getattr(LOCAL,'deadline',None)
        if not deadline:return original_galaxy(path,ttl,timeout)
        remaining=deadline-time.monotonic()
        if remaining<=0:raise env['HTTPError'](504,'Retained source read budget reached.')
        return env['CACHE'].get('g:'+path,ttl,lambda:env['fetch_json'](env['CFG']['galaxy'],'/api/'+path,min(timeout,remaining)),wait=min(30,remaining))

    def direct_node(address,chain):
        key=(chain,address)
        with raw_lock:old=raw_nodes.get(key)
        if old and time.monotonic()-old[0]<120:return old[1]
        value=env['galaxy']('wallet/'+chain+'/'+address,ttl=120,timeout=45)
        if value.get('chain',chain)!=chain or str(value.get('address',address)).lower()!=address:raise env['HTTPError'](502,'Wallet source identity mismatch.')
        if value.get('node_id') is not None and value.get('in_map') and not value.get('balance_sheet'):
            value=env['galaxy']('node/'+chain+'/'+str(int(value['node_id'])),ttl=120,timeout=15)
        if value.get('chain',chain)!=chain or str(value.get('address',address)).lower()!=address:raise env['HTTPError'](502,'Wallet node identity mismatch.')
        with raw_lock:
            if len(raw_nodes)>=128:raw_nodes.pop(next(iter(raw_nodes)))
            raw_nodes[key]=(time.monotonic(),value)
        return value

    def find_wallet(address,chain=None):
        if not chain:return original_find(address,chain)
        try:node=direct_node(address,chain)
        except env['HTTPError'] as e:
            if e.code==404:return None
            raise
        return {'chain':chain,'address':address,'node_id':node.get('node_id'),'in_map':bool(node.get('in_map'))}

    def wallet(qs):
        card=original_wallet(qs);chain=card.get('chain');address=card.get('address')
        if chain in env['EVM_CHAINS'] and address and card.get('status') not in {'coming','unavailable'}:
            node=direct_node(address,chain);projector=env.get('wallet_position')
            qualities=[card.get('_sourceQuality'),node.get('_sourceQuality')]
            global_held=bool(card.get('held') or (card.get('sheet') or {}).get('held') or
                             any(q.get('held') or q.get('state')=='held' or q.get('status')=='held' for q in qualities if isinstance(q,dict)))
            rows,total=position_rows(node,chain,env['CATALOG'].market_for,projector=projector,sheet_held=global_held)
            if 'positions_limit' in qs:rows=rows[:int(qs['positions_limit'])]  # original_wallet validates explicit limits first
            known_total=card.get('positionsTotal')
            total_positions=max(total,known_total) if type(known_total) is int and known_total>=0 else total
            card.update(positionsReturned=len(rows),positionsTotal=total_positions,
                        positions=rows,positionRows=total,positionsTruncated=total_positions>len(rows),
                        identityNote=('Position identity uses recorded exact chain and contract metadata. Display symbols never establish contracts. Charts require an exact reviewed catalog match; transfers do not establish ownership.' if callable(projector) else
                                      'Chart identities use position contracts, or a unique token with the same symbol in this wallet’s recorded activity. Ambiguous symbols are excluded. No unrelated token is substituted.'))
        return card

    def trace(qs):
        chain,address=qs['chain'],qs['address'];node=direct_node(address,chain)
        if node.get('in_map') and node.get('node_id') is not None:
            nb=env['galaxy']('neighbors/'+chain+'/'+str(int(node['node_id']))+'?limit=10',ttl=120,timeout=15)
            peers=[{'address':n.get('address'),'nodeId':n.get('node_id'),'category':n.get('category'),'categoryName':env['CATEGORY'].get(n.get('category') or 'unknown','Wallet'),'entity':env['human'](n.get('entity_name')),'direction':n.get('dir'),'usd':n.get('usd'),'n':n.get('n'),'assets':(n.get('assets') or [])[:3]} for n in (nb.get('neighbors') or [])[:10]]
        else:
            nb=env['glserve_neighbors'](chain,address,10,'both') or {}
            peers=[{'address':n.get('ref'),'nodeId':None,'category':n.get('category'),'entity':n.get('entity'),'direction':n.get('dir'),'n':n.get('n'),'assets':[],'countOnly':True} for n in (nb.get('neighbors') or [])[:10]]
        return {'kind':'trace','chain':chain,'address':address,'counterparties':peers,'note':'Recorded aggregate connections; shared services do not establish common ownership.'}

    def execute(kind,chain,address,target):
        if kind=='wallet':return wallet({'chain':chain,'address':address})
        if kind=='trace':return trace({'chain':chain,'address':address})
        return env['flow_trace']({'chain':chain,'from':address,**({'to':target} if target else {}),'hops':'2'})

    jobs=ReadJobs(execute)
    def api(handler,path,query,qs):
        if handler.command=='GET' and path=='/api/public/em/wallet-coverage':
            env['LIMIT'].take(env['client_ip'](handler),'data')
            if qs:raise env['HTTPError'](400,'wallet-coverage takes no parameters.')
            def read_coverage():
                registry=env['chains_status']()['chains'];env['CATALOG'].refresh()
                counts={c['id']:0 for c in registry}
                for market in env['CATALOG'].rows:counts[market['network']]=counts.get(market['network'],0)+1
                return {'schema':'ticking.wallet-coverage.v1','checkedAt':int(time.time()),'chains':[
                    {'id':c['id'],'name':c['name'],'status':c.get('status'),'hasData':bool(c.get('hasData')),
                     'reviewedCharts':counts.get(c['id'],0),'configured':{k:bool(c.get('capabilities',{}).get(k)) for k in ['map','econ','graph','realtime']},
                     'readScope':'Existing indexed records only; an absent wallet is not a zero balance.'}
                    for c in registry],
                    'limits':['Registry membership does not prove a specific wallet is indexed.','Charts use exact reviewed chain and contract identities.','Solana pool charts and native BTC reference charts are separate from these EVM wallet stores.','Transfers, borrowing and bridge movements are not inferred purchases or common ownership.']}
            return handler.send_json(200,env['CACHE'].get('agent-wallet-coverage',30,read_coverage),cache='public, max-age=30')
        if handler.command=='GET' and path=='/api/public/em/wallet-read':
            if set(qs)-{'kind','chain','address','to','poll'}:raise env['HTTPError'](400,'Unknown wallet-read parameter.')
            chain=env['opt_chain'](qs);address=env['need_addr'](qs);kind=qs.get('kind')
            if not chain or kind not in {'wallet','trace','path'}:raise env['HTTPError'](400,'Choose a registered chain and wallet, trace or path read.')
            target=env['need_addr'](qs,'to') if qs.get('to') else None
            if target and kind!='path':raise env['HTTPError'](400,'A destination is only used for a path read.')
            if qs.get('poll'):
                expected=jobs.identity(kind,chain,address,target)
                with jobs.lock:exists=expected in jobs.jobs
                if qs['poll']!=expected or not exists:raise env['HTTPError'](404,'This source read has expired. Start the exact read again.')
                # Status reads do not launch work and must not consume a second
                # heavy-tool allowance every second of a cold source read.
                env['LIMIT'].take(env['client_ip'](handler),'data')
            else:env['LIMIT'].take(env['client_ip'](handler),'tools')
            code,value=jobs.submit(kind,chain,address,target)
            return handler.send_json(code,value,cache='no-store')
        return original_api(handler,path,query,qs)

    env['fetch']=bounded_fetch;env['galaxy']=bounded_galaxy;env['find_wallet']=find_wallet;env['tool_wallet']=wallet;env['tool_trace']=trace;env['Handler'].api=api
    env['AGENT_WALLET_JOBS']=jobs
