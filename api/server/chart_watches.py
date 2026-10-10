"""Tenant-owned mint subscriptions, over the existing shared Solana engine."""
import hashlib,json,secrets,time
from fastapi import HTTPException,Request
from pydantic import BaseModel,Field
from solders.pubkey import Pubkey
from . import engine_data,products,realtime
from .store import db

def initialize(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS chart_watches(
      id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
      project_id TEXT NOT NULL REFERENCES chart_projects(id),mint TEXT NOT NULL,
      created INTEGER NOT NULL,revoked INTEGER);
      CREATE UNIQUE INDEX IF NOT EXISTS live_chart_watch ON chart_watches(project_id,mint) WHERE revoked IS NULL;
      CREATE INDEX IF NOT EXISTS user_chart_watch ON chart_watches(user_id,revoked);
      CREATE TABLE IF NOT EXISTS chart_watch_sync(user_id TEXT PRIMARY KEY REFERENCES users(id),expires INTEGER NOT NULL,mints TEXT NOT NULL);''')
    if 'paused' not in {r['name'] for r in c.execute('PRAGMA table_info(chart_watches)')}:
        c.execute('ALTER TABLE chart_watches ADD COLUMN paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1))')

def mint_identity(value):
    try:
        if str(Pubkey.from_string(value))!=value:raise ValueError()
        return value
    except (ValueError,TypeError):raise HTTPException(422,'Enter the exact Solana token mint address, not a wallet or pool.')

def control(path,**body):
    import httpx
    try:
        r=engine_data.client.post(path,json=body,headers={'Authorization':'Bearer '+engine_data.SECRET_FILE.read_text().strip()})
        data=r.json()
        if r.status_code in {422,429}:raise HTTPException(r.status_code,data.get('detail','Token watch unavailable.'),headers={'Retry-After':'30'} if r.status_code==429 else None)
        r.raise_for_status();return data
    except (httpx.HTTPError,OSError,ValueError):raise HTTPException(503,'Token admission is temporarily unavailable. Existing charts are retained.',headers={'Retry-After':'10'})

def owner(user_id):return 'workspace:'+hashlib.sha256(user_id.encode()).hexdigest()
def list_watches(user_id):
    with db() as c:return [dict(r) for r in c.execute('SELECT w.* FROM chart_watches w JOIN chart_projects p ON p.id=w.project_id WHERE w.user_id=? AND w.revoked IS NULL AND p.revoked IS NULL ORDER BY w.created DESC',(user_id,))]

def authorize(user_id,mint,project_id=None):
    if not products.limits(user_id)['managed']:raise HTTPException(403,'Choose Charts or Data to manage a token chart.')
    rows=[r for r in list_watches(user_id) if not r['paused']]
    cap=5 if products.plan_for(user_id)['plan']=='Charts' else 20
    allowed=sorted({r['mint'] for r in rows})[:cap]
    if mint not in allowed:raise HTTPException(403,'This token is outside the current plan watch limit.')
    if not any(r['mint']==mint and (project_id is None or r['project_id']==project_id) for r in rows):
        raise HTTPException(403,'Register this token mint in the chart project first.')

def status(mint):
    watch=engine_data.get('/watch/status',mint=mint)
    resolved=engine_data.resolve(mint=mint,aggregation='token')
    ready=bool(resolved.get('ready'))
    return {'mint':mint,'network':'solana','state':'ready' if ready else watch['state'],
            'ready':ready,'verified':watch['verified'] or ready,'active':watch['active'],
            'message':'Retained chart data is ready.' if ready else watch.get('message') or 'Token validation is queued.',
            'pools':watch.get('pools',[]),'quoteMint':resolved.get('quoteMint'),
            'minuteCandles':resolved.get('minuteCandles',0),'coverage':resolved.get('coverage',{}),
            'historyComplete':False,'bondingCurveCoverage':watch.get('bondingCurveCoverage','adapter_pending'),
            'updatedAt':resolved.get('last'),'provisional':True}

def chart_window(result):
    """A bounded display window, from the same exact-mint retained reader."""
    if not result.get('ready'):return result
    try:
        data=engine_data.candles(mint=result['mint'],aggregation='token',interval='1m',limit=300,gap_policy='carry',history='executions')
        if data.get('mint')==result['mint'] and data.get('identity',{}).get('baseMint')==result['mint'] and data.get('candles'):
            return {**result,'initialChart':{'history':'executions','data':data}}
    except HTTPException as e:
        if e.status_code not in {409,422,503}:raise
    return result  # A window read failure never invalidates admission or erases history.

def preview_result(mint,initial_chart=False):
    result=status(mint)
    if initial_chart and result['ready']:
        try:authorize_preview(mint)
        except HTTPException:
            # A ready retained series does not authorize an unverified lease.
            return result
        return chart_window(result)
    return result

def authorize_preview(mint,aggregation='token',pool=None):
    # A paid customer's watch alone never opens free direct-data access.
    # Only bounded, independently verified public preview leases admit charts.
    if aggregation!='token' or pool or not engine_data.address(mint):
        raise HTTPException(404,'Start an exact-mint chart preview first.')
    watch=engine_data.get('/watch/status',mint=mint)
    if not watch.get('verified') or (watch.get('previewExpires') or 0)<=time.time():
        raise HTTPException(404,'This chart preview has paused. Submit the mint again to resume.')
    return mint

def reconcile():
    # Subscription extension/downgrade and project revocation change leases,
    # never rewrite price history or customer payment state.
    with db() as c:
        users=[r[0] for r in c.execute('SELECT DISTINCT user_id FROM chart_watches UNION SELECT user_id FROM chart_watch_sync LIMIT 1000')]
    for user_id in users:
        plan=products.plan_for(user_id);limit=products.spec(plan['plan'])
        cap=5 if plan['plan']=='Charts' else 20
        mints=sorted({r['mint'] for r in list_watches(user_id) if not r['paused']})[:cap] if limit['managed'] else []
        expires=plan['expires'] if mints else 0
        with db() as c:old=c.execute('SELECT * FROM chart_watch_sync WHERE user_id=?',(user_id,)).fetchone()
        prior=json.loads(old['mints']) if old else []
        if old and old['expires']==expires and prior==mints:continue
        for mint in set(prior)-set(mints):control('/watch/cancel',mint=mint,owner=owner(user_id))
        for mint in mints:control('/watch/register',mint=mint,owner=owner(user_id),kind='paid',expires=expires)
        with db() as c:c.execute('INSERT OR REPLACE INTO chart_watch_sync VALUES(?,?,?)',(user_id,expires,json.dumps(mints)))

async def run():
    import asyncio
    while True:
        try:await realtime.threaded(reconcile)
        except Exception:pass  # Retry local admission; last-good delivery is retained.
        await asyncio.sleep(60)

def install(app,session_user,api_user,rate):
    class Registration(BaseModel):
        mint:str=Field(min_length=32,max_length=44)
        project_id:str|None=Field(default=None,min_length=15,max_length=40)
        create_key:bool=False

    def register(user,body):
        mint=mint_identity(body.mint);plan=products.plan_for(user['id']);limit=products.spec(plan['plan'])
        if not limit['managed']:raise HTTPException(403,'Choose Charts or Data to register a managed token chart.')
        cap=5 if plan['plan']=='Charts' else 20
        rate('watch-register:'+user['id'],10,60)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if body.project_id is None:
                # Console-only collections reserve no customer app domain. A
                # publishable ID with no registered origins cannot grant embeds.
                project=c.execute("SELECT * FROM chart_projects WHERE user_id=? AND name='Token portfolio' AND origins='[]' AND revoked IS NULL",(user['id'],)).fetchone()
                if not project:
                    if c.execute('SELECT count(*) FROM chart_projects WHERE user_id=? AND revoked IS NULL',(user['id'],)).fetchone()[0]>=5:raise HTTPException(409,'Choose an existing chart project. The project limit is reached.')
                    ident='cp_'+secrets.token_hex(12)
                    c.execute('INSERT INTO chart_projects VALUES(?,?,?,?,?,?,NULL)',(ident,user['id'],'Token portfolio','[]','ticking_pk_'+secrets.token_urlsafe(24),int(time.time())))
                    project=c.execute('SELECT * FROM chart_projects WHERE id=?',(ident,)).fetchone()
            else:project=c.execute('SELECT * FROM chart_projects WHERE id=? AND user_id=? AND revoked IS NULL',(body.project_id,user['id'])).fetchone()
            if not project:raise HTTPException(404,'Choose an active chart project from this workspace.')
            project_id=project['id']
            active=[r for r in list_watches(user['id']) if not r['paused']]
            if mint not in {r['mint'] for r in active} and len({r['mint'] for r in active})>=cap:raise HTTPException(409,f'This plan supports {cap} active custom token mints. Remove an unused watch first.')
            found=c.execute('SELECT * FROM chart_watches WHERE user_id=? AND project_id=? AND mint=? AND revoked IS NULL',(user['id'],project_id,mint)).fetchone()
            if found:ident=found['id']
            else:
                ident='cw_'+secrets.token_hex(12)
                if len(list_watches(user['id']))>=128:raise HTTPException(409,'Remove an unused token from your portfolio first.')
                c.execute('INSERT INTO chart_watches(id,user_id,project_id,mint,created,revoked) VALUES(?,?,?,?,?,NULL)',(ident,user['id'],project_id,mint,int(time.time())))
            c.execute('UPDATE chart_watches SET paused=0 WHERE id=?',(ident,))
            secret=None;key_name='Chart API '+project_id
            existing=c.execute('SELECT id FROM api_keys WHERE user_id=? AND name=? AND revoked IS NULL',(user['id'],key_name)).fetchone()
            if body.create_key and not existing:
                count=c.execute("SELECT count(*) FROM api_keys WHERE user_id=? AND revoked IS NULL AND prefix!='managed'",(user['id'],)).fetchone()[0]
                if count>=limit['keys']:raise HTTPException(409,'Your API key limit is reached. Reuse an existing chart key or revoke an unused key.')
                secret='tk_live_'+secrets.token_urlsafe(32);key_id='key_'+secrets.token_hex(12)
                scopes=['charts:embed','markets:read']+(['candles:read','trades:read'] if limit['data'] else [])
                c.execute('INSERT INTO api_keys(id,user_id,name,hash,prefix,scopes,created,scope_version) VALUES(?,?,?,?,?,?,?,?)',(key_id,user['id'],key_name,hashlib.sha256(secret.encode()).hexdigest(),secret[:18],json.dumps(scopes),int(time.time()),'v2'))
            # Network control is local and bounded; failure rolls back both the
            # workspace binding and one-time API key. Provider work is asynchronous.
            control('/watch/register',mint=mint,owner=owner(user['id']),kind='paid',expires=plan['expires'])
            registered=sorted({r[0] for r in c.execute('SELECT w.mint FROM chart_watches w JOIN chart_projects p ON p.id=w.project_id WHERE w.user_id=? AND w.revoked IS NULL AND w.paused=0 AND p.revoked IS NULL',(user['id'],))})
            c.execute('INSERT OR REPLACE INTO chart_watch_sync VALUES(?,?,?)',(user['id'],plan['expires'],json.dumps(registered)))
        return {'id':ident,'mint':mint,'project_id':project_id,'publishable':project['publishable'],'key':secret,
                'state':'queued','status_url':'/api/chart-watches/'+ident,'customMintLimit':cap}

    @app.post('/api/chart-watches')
    def console_register(body:Registration,request:Request):return register(session_user(request),body)
    @app.post('/api/v1/chart-watches')
    def api_register(body:Registration,request:Request):
        key=api_user(request,'charts:embed')
        if body.create_key:raise HTTPException(422,'Create server keys in the console. API registration reuses your current key.')
        return register({'id':key['user_id']},body)
    @app.get('/api/chart-watches/{ident}')
    def watch_status(ident:str,request:Request,initial_chart:bool=False):
        user=session_user(request);row=next((r for r in list_watches(user['id']) if r['id']==ident),None)
        if not row:raise HTTPException(404,'Token watch not found.')
        result={**status(row['mint']),'paused':bool(row['paused'])}
        if initial_chart and not row['paused'] and products.limits(user['id'])['managed']:
            authorize(user['id'],row['mint'],row['project_id'])
            result=chart_window(result)
        return result
    @app.get('/api/v1/chart-watches/{ident}')
    def api_status(ident:str,request:Request):
        key=api_user(request,'charts:embed');row=next((r for r in list_watches(key['user_id']) if r['id']==ident),None)
        if not row:raise HTTPException(404,'Token watch not found.')
        return status(row['mint'])
    @app.delete('/api/chart-watches/{ident}')
    def remove(ident:str,request:Request):
        user=session_user(request)
        with db() as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM chart_watches WHERE id=? AND user_id=? AND revoked IS NULL',(ident,user['id'])).fetchone()
            if not row:raise HTTPException(404,'Token watch not found.')
            c.execute('UPDATE chart_watches SET revoked=? WHERE id=?',(int(time.time()),ident))
            if not c.execute('SELECT 1 FROM chart_watches w JOIN chart_projects p ON p.id=w.project_id WHERE w.user_id=? AND w.mint=? AND w.revoked IS NULL AND w.paused=0 AND p.revoked IS NULL',(user['id'],row['mint'])).fetchone():control('/watch/cancel',mint=row['mint'],owner=owner(user['id']))
        return {'ok':True}
    class Activation(BaseModel):active:bool
    @app.patch('/api/chart-watches/{ident}')
    def activation(ident:str,body:Activation,request:Request):
        user=session_user(request);rate('watch-register:'+user['id'],10,60)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT w.* FROM chart_watches w JOIN chart_projects p ON p.id=w.project_id WHERE w.id=? AND w.user_id=? AND w.revoked IS NULL AND p.revoked IS NULL',(ident,user['id'])).fetchone()
            if not row:raise HTTPException(404,'Token watch not found.')
            if body.active:
                plan=products.plan_for(user['id'])
                if not products.spec(plan['plan'])['managed']:raise HTTPException(403,'Choose Charts or Data to activate token collection.')
                cap=5 if plan['plan']=='Charts' else 20
                active={r['mint'] for r in list_watches(user['id']) if not r['paused']}
                if row['mint'] not in active and len(active)>=cap:raise HTTPException(409,f'This plan supports {cap} active custom token mints.')
                control('/watch/register',mint=row['mint'],owner=owner(user['id']),kind='paid',expires=plan['expires'])
            c.execute('UPDATE chart_watches SET paused=? WHERE id=?',(int(not body.active),ident))
            if not body.active and not c.execute('SELECT 1 FROM chart_watches w JOIN chart_projects p ON p.id=w.project_id WHERE w.user_id=? AND w.mint=? AND w.revoked IS NULL AND w.paused=0 AND p.revoked IS NULL',(user['id'],row['mint'])).fetchone():control('/watch/cancel',mint=row['mint'],owner=owner(user['id']))
            # Force the subscription reconciler to recompute the exact set.
            c.execute('DELETE FROM chart_watch_sync WHERE user_id=?',(user['id'],))
        return {'ok':True,'paused':not body.active}
    class Preview(BaseModel):
        mint:str=Field(min_length=32,max_length=44)
        initial_chart:bool=False
    @app.post('/api/public/chart-preview')
    async def preview(body:Preview,request:Request):
        mint=mint_identity(body.mint);ip=request.headers.get('x-real-ip',request.client.host)
        rate('mint-preview:'+ip,12,300);rate('mint-preview-global',120,300)
        identity='preview:'+hashlib.sha256(ip.encode()).hexdigest();expires=int(time.time())+600
        # Reuse an existing recorded reference immediately; a preview lease
        # only adds collection when the current catalog cannot supply it.
        await realtime.threaded(control,'/watch/register',mint=mint,owner=identity,kind='preview',expires=expires)
        result=await realtime.threaded(preview_result,mint,body.initial_chart)
        return {**result,'previewExpires':expires,'temporary':True}
    @app.get('/api/public/chart-preview')
    async def preview_status(mint:str,request:Request,initial_chart:bool=False):
        mint_identity(mint);rate('preview-status:'+request.headers.get('x-real-ip',request.client.host),60)
        return await realtime.threaded(preview_result,mint,initial_chart)
