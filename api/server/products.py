"""Versioned chart/data entitlements. Legacy purchases keep their original term."""
import json
import time
from fastapi import HTTPException
from .store import db

VERSION = 'products-20261010-v2'
PLANS = {
    'Explore': dict(usd=0, keys=2, requests=10000, markets=3, domains=1,
                    chart_streams=2, data_streams=0, rpm=120, data=True, managed=False),
    'Charts': dict(usd=29, keys=3, requests=None, markets=2000, domains=2,
                   chart_streams=16, data_streams=0, rpm=120, data=False, managed=True),
    'Data': dict(usd=59, keys=5, requests=None, markets=2000, domains=5,
                 chart_streams=16, data_streams=4, rpm=120, data=True, managed=True),
}
LEGACY = {
    'Pro': dict(usd=29,keys=3,requests=100000,markets=10),
    'Build': dict(usd=149,keys=10,requests=1000000,markets=10),
    'Scale': dict(usd=499,keys=25,requests=10000000,markets=100),
}

def spec(name):
    if name in PLANS: return {**PLANS[name], 'version':VERSION}
    if name in LEGACY:
        return {**PLANS['Data'], **LEGACY[name], 'version':'legacy-v1', 'legacy':True}
    raise HTTPException(403, 'This entitlement needs review. Contact support.')

def plan_for(user_id):
    with db() as c:
        rows=c.execute('SELECT e.plan,e.expires,i.billing_period,i.term_days FROM entitlements e LEFT JOIN invoices i ON i.id=e.source WHERE e.user_id=? AND e.expires>?',
                       (user_id,int(time.time()))).fetchall()
    return max((dict(r) for r in rows),key=lambda r:(spec(r['plan'])['usd'],r['expires']),
               default={'plan':'Explore','expires':None})

def limits(user_id): return spec(plan_for(user_id)['plan'])

def require_data(user_id):
    if not limits(user_id)['data']:
        raise HTTPException(403, 'Charts includes managed chart display. Choose Data or use explicit prepaid reads for direct data access.')

def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS chart_projects(
      id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),name TEXT NOT NULL,
      origins TEXT NOT NULL,publishable TEXT UNIQUE NOT NULL,created INTEGER NOT NULL,revoked INTEGER);
    CREATE INDEX IF NOT EXISTS chart_project_user ON chart_projects(user_id,revoked);
    CREATE TABLE IF NOT EXISTS usage_rollups(
      user_id TEXT NOT NULL,key_id TEXT NOT NULL,endpoint TEXT NOT NULL,hour INTEGER NOT NULL,
      units INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,key_id,endpoint,hour));
    CREATE TABLE IF NOT EXISTS trial_accounts(
      user_id TEXT PRIMARY KEY REFERENCES users(id),subject TEXT UNIQUE NOT NULL,
      granted_micro INTEGER NOT NULL CHECK(granted_micro=5000000),
      balance_micro INTEGER NOT NULL CHECK(balance_micro>=0),
      reserved_micro INTEGER NOT NULL DEFAULT 0 CHECK(reserved_micro>=0 AND reserved_micro<=balance_micro),
      created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS trial_entries(
      id INTEGER PRIMARY KEY,user_id TEXT NOT NULL REFERENCES trial_accounts(user_id),
      source TEXT UNIQUE NOT NULL,delta_micro INTEGER NOT NULL CHECK(delta_micro!=0),created INTEGER NOT NULL);
    CREATE TRIGGER IF NOT EXISTS trial_post AFTER INSERT ON trial_entries
      BEGIN UPDATE trial_accounts SET balance_micro=balance_micro+NEW.delta_micro WHERE user_id=NEW.user_id; END;
    CREATE TRIGGER IF NOT EXISTS trial_entry_no_update BEFORE UPDATE ON trial_entries
      BEGIN SELECT RAISE(ABORT,'immutable trial entry'); END;
    CREATE TRIGGER IF NOT EXISTS trial_entry_no_delete BEFORE DELETE ON trial_entries
      BEGIN SELECT RAISE(ABORT,'immutable trial entry'); END;
    CREATE TABLE IF NOT EXISTS plan_receipts(
      invoice_id TEXT PRIMARY KEY REFERENCES invoices(id),signature TEXT UNIQUE NOT NULL,
      receipt TEXT NOT NULL,sha256 TEXT NOT NULL,created INTEGER NOT NULL);
    CREATE TRIGGER IF NOT EXISTS plan_receipt_no_update BEFORE UPDATE ON plan_receipts
      BEGIN SELECT RAISE(ABORT,'immutable plan receipt'); END;
    CREATE TRIGGER IF NOT EXISTS plan_receipt_no_delete BEFORE DELETE ON plan_receipts
      BEGIN SELECT RAISE(ABORT,'immutable plan receipt'); END;
    ''')
    def column(table,name,definition):
        if name not in {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}:
            c.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
    column('invoices','policy_version',"TEXT NOT NULL DEFAULT 'legacy-v1'")
    column('api_keys','scope_version',"TEXT NOT NULL DEFAULT 'legacy-v1'")
    column('display_tokens','project_id','TEXT REFERENCES chart_projects(id)')
    column('exchange_display_tokens','project_id','TEXT REFERENCES chart_projects(id)')
    column('credit_requests','trial_reserved_micro','INTEGER NOT NULL DEFAULT 0')
    column('credit_requests','trial_charged_micro','INTEGER NOT NULL DEFAULT 0')
    column('credit_settings','stream_enabled','INTEGER NOT NULL DEFAULT 0 CHECK(stream_enabled IN (0,1))')

def trial_snapshot(c,user_id):
    row=c.execute('SELECT * FROM trial_accounts WHERE user_id=?',(user_id,)).fetchone()
    eligible=bool(trial_identity(c,user_id))
    return {'eligible':eligible,'claimed':bool(row),'grant_micro':5000000,
            'balance_micro':row['balance_micro'] if row else 0,
            'reserved_micro':row['reserved_micro'] if row else 0,
            'available_micro':row['balance_micro']-row['reserved_micro'] if row else 0,
            'kind':'one_time_service_credit','cash':False,'withdrawable':False}

def claim_trial(user_id):
    stamp=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM trial_accounts WHERE user_id=?',(user_id,)).fetchone():
            return trial_snapshot(c,user_id)
        identity=trial_identity(c,user_id)
        if not identity: raise HTTPException(403,'Connect a verified Google or wallet login before claiming the one-time credit.')
        if c.execute('SELECT 1 FROM trial_accounts WHERE subject=?',(identity['subject'],)).fetchone():
            raise HTTPException(409,'This login has already claimed its one-time service credit.')
        c.execute('INSERT INTO trial_accounts VALUES(?,?,5000000,0,0,?)',(user_id,identity['subject'],stamp))
        c.execute('INSERT INTO trial_entries(user_id,source,delta_micro,created) VALUES(?,?,5000000,?)',
                  (user_id,'welcome:'+identity['subject'],stamp))
        c.execute('INSERT INTO audit(user_id,action,created) VALUES(?,?,?)',(user_id,'One-time $5 service credit claimed',stamp))
        return trial_snapshot(c,user_id)

def trial_identity(c,user_id):
    for row in c.execute('SELECT subject,profile FROM auth_identities WHERE user_id=? ORDER BY created',(user_id,)):
        if any(a.get('type') in {'google','wallet'} and a.get('label') for a in json.loads(row['profile'])):return row
    return None

def projects(user_id):
    with db() as c:
        rows=[dict(r) for r in c.execute('SELECT id,name,origins,publishable,created,revoked FROM chart_projects WHERE user_id=? ORDER BY created DESC',(user_id,))]
    for r in rows: r['origins']=json.loads(r['origins'])
    return rows

def used(c,user_id,start):
    old=c.execute('SELECT COALESCE(SUM(units),0) FROM usage_events WHERE user_id=? AND created>=?',(user_id,start)).fetchone()[0]
    new=c.execute('SELECT COALESCE(SUM(units),0) FROM usage_rollups WHERE user_id=? AND hour>=?',(user_id,start)).fetchone()[0]
    return old+new

def meter(c,user_id,key_id,endpoint):
    hour=int(time.time())//3600*3600
    c.execute('INSERT INTO usage_rollups VALUES(?,?,?,?,1) ON CONFLICT(user_id,key_id,endpoint,hour) DO UPDATE SET units=units+1',(user_id,key_id or 'console',endpoint,hour))
