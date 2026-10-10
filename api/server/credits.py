"""Prepaid service credits. Integer USDC accounting; no custody or signing key.

Only finalized, invoice-bound receipts fund the ledger. Reservations precede
work; balanced, sealed postings and the retryable result commit together.
"""
import hashlib
import json
import math
import re
import secrets
import time
from .store import db

VERSION = 'credits-20261010-v1'
MICRO = 1_000_000
MAX_BODY = 65536
RESERVATION_SECONDS = 90
REPLAY_SECONDS = 86400
RATES = {
    'price': {'base_micro': 5, 'max_micro': 5, 'bytes_per_unit': 4096},
    'candles': {'base_micro': 50, 'max_micro': 100, 'bytes_per_unit': 32768},
    'trades': {'base_micro': 50, 'max_micro': 100, 'bytes_per_unit': 32768},
    'stream': {'base_micro':100,'max_micro':800,'bytes_per_unit':32768,'seconds_per_unit':15,'bandwidth_micro':50},
}


class CreditError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status


def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS credit_accounts(
      id TEXT PRIMARY KEY, user_id TEXT UNIQUE REFERENCES users(id),
      kind TEXT NOT NULL CHECK(kind IN ('customer','settlement','service')),
      balance_micro INTEGER NOT NULL DEFAULT 0,
      reserved_micro INTEGER NOT NULL DEFAULT 0 CHECK(reserved_micro>=0),
      CHECK(kind!='customer' OR (balance_micro>=0 AND reserved_micro<=balance_micro)));
    CREATE TABLE IF NOT EXISTS credit_journals(
      id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('topup','usage')),
      source TEXT NOT NULL UNIQUE, created INTEGER NOT NULL,
      metadata TEXT NOT NULL, sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)));
    CREATE TABLE IF NOT EXISTS credit_entries(
      id INTEGER PRIMARY KEY, journal_id TEXT NOT NULL REFERENCES credit_journals(id),
      account_id TEXT NOT NULL REFERENCES credit_accounts(id),
      delta_micro INTEGER NOT NULL CHECK(delta_micro!=0),
      UNIQUE(journal_id,account_id));
    CREATE INDEX IF NOT EXISTS credit_entry_account ON credit_entries(account_id,id);
    CREATE TRIGGER IF NOT EXISTS credit_entry_post AFTER INSERT ON credit_entries
      BEGIN UPDATE credit_accounts SET balance_micro=balance_micro+NEW.delta_micro
      WHERE id=NEW.account_id; END;
    CREATE TRIGGER IF NOT EXISTS credit_entry_sealed BEFORE INSERT ON credit_entries
      WHEN (SELECT sealed FROM credit_journals WHERE id=NEW.journal_id)!=0
      BEGIN SELECT RAISE(ABORT,'sealed credit journal'); END;
    CREATE TRIGGER IF NOT EXISTS credit_entry_no_update BEFORE UPDATE ON credit_entries
      BEGIN SELECT RAISE(ABORT,'immutable credit entry'); END;
    CREATE TRIGGER IF NOT EXISTS credit_entry_no_delete BEFORE DELETE ON credit_entries
      BEGIN SELECT RAISE(ABORT,'immutable credit entry'); END;
    CREATE TRIGGER IF NOT EXISTS credit_journal_no_delete BEFORE DELETE ON credit_journals
      BEGIN SELECT RAISE(ABORT,'immutable credit journal'); END;
    CREATE TRIGGER IF NOT EXISTS credit_journal_seal BEFORE UPDATE ON credit_journals
      WHEN NOT (OLD.sealed=0 AND NEW.sealed=1 AND OLD.id=NEW.id
      AND OLD.kind=NEW.kind AND OLD.source=NEW.source AND OLD.created=NEW.created
      AND OLD.metadata=NEW.metadata
      AND (SELECT COUNT(*) FROM credit_entries WHERE journal_id=OLD.id)=2
      AND (SELECT SUM(delta_micro) FROM credit_entries WHERE journal_id=OLD.id)=0)
      BEGIN SELECT RAISE(ABORT,'unbalanced or immutable credit journal'); END;
    CREATE TABLE IF NOT EXISTS credit_settings(
      user_id TEXT PRIMARY KEY REFERENCES users(id), enabled INTEGER NOT NULL DEFAULT 0,
      daily_limit_micro INTEGER NOT NULL DEFAULT 1000000 CHECK(daily_limit_micro BETWEEN 5 AND 1000000000),
      terms_version TEXT, updated INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS credit_budgets(
      user_id TEXT NOT NULL REFERENCES users(id), day INTEGER NOT NULL,
      spent_micro INTEGER NOT NULL DEFAULT 0 CHECK(spent_micro>=0),
      reserved_micro INTEGER NOT NULL DEFAULT 0 CHECK(reserved_micro>=0),
      PRIMARY KEY(user_id,day));
    CREATE TABLE IF NOT EXISTS credit_requests(
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
      key_id TEXT NOT NULL REFERENCES api_keys(id), idem TEXT NOT NULL,
      fingerprint TEXT NOT NULL, product TEXT NOT NULL, price_version TEXT NOT NULL,
      reserved_micro INTEGER NOT NULL, charged_micro INTEGER NOT NULL DEFAULT 0,
      day INTEGER NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('reserved','completed','released')),
      response TEXT, response_sha256 TEXT, UNIQUE(user_id,idem));
    CREATE INDEX IF NOT EXISTS credit_request_expiry ON credit_requests(user_id,status,expires);
    CREATE INDEX IF NOT EXISTS credit_request_replay ON credit_requests(user_id,status,created);
    CREATE TABLE IF NOT EXISTS credit_receipts(
      invoice_id TEXT PRIMARY KEY REFERENCES invoices(id), signature TEXT UNIQUE NOT NULL,
      proof TEXT NOT NULL, proof_sha256 TEXT NOT NULL, verified INTEGER NOT NULL);
    CREATE TRIGGER IF NOT EXISTS credit_receipt_no_update BEFORE UPDATE ON credit_receipts
      BEGIN SELECT RAISE(ABORT,'immutable credit receipt'); END;
    CREATE TRIGGER IF NOT EXISTS credit_receipt_no_delete BEFORE DELETE ON credit_receipts
      BEGIN SELECT RAISE(ABORT,'immutable credit receipt'); END;
    INSERT OR IGNORE INTO credit_accounts(id,kind) VALUES('system:settlement','settlement');
    INSERT OR IGNORE INTO credit_accounts(id,kind) VALUES('system:service','service');
    ''')


def _account(c, user_id):
    c.execute("INSERT OR IGNORE INTO credit_accounts(id,user_id,kind) VALUES(?,?,'customer')",
              ('user:'+user_id, user_id))
    c.execute('INSERT OR IGNORE INTO credit_settings(user_id,updated) VALUES(?,?)', (user_id, int(time.time())))
    return c.execute('SELECT * FROM credit_accounts WHERE user_id=?', (user_id,)).fetchone()


def _post(c, user_id, kind, source, amount, metadata):
    if type(amount) is not int or amount<=0:
        raise ValueError('Positive integer atomic USDC amount required')
    account=_account(c,user_id); ident='cj_'+secrets.token_hex(12)
    c.execute('INSERT INTO credit_journals(id,kind,source,created,metadata) VALUES(?,?,?,?,?)',
              (ident,kind,source,int(time.time()),json.dumps(metadata,separators=(',',':'))))
    counterpart='system:settlement' if kind=='topup' else 'system:service'
    signed=amount if kind=='topup' else -amount
    c.execute('INSERT INTO credit_entries(journal_id,account_id,delta_micro) VALUES(?,?,?)', (ident,account['id'],signed))
    c.execute('INSERT INTO credit_entries(journal_id,account_id,delta_micro) VALUES(?,?,?)', (ident,counterpart,-signed))
    c.execute('UPDATE credit_journals SET sealed=1 WHERE id=?', (ident,))
    return ident


def finalize_topup(invoice_id,proof):
    """Called only after the invoice's exact finalized receipt was verified.

    No public endpoint can pass an amount or directly post a ledger credit.
    """
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM invoices WHERE id=? AND purpose='credits'", (invoice_id,)).fetchone()
        if row is None or not row['signature'] or row['status'] not in {'pending','paid'}:
            raise CreditError('invalid_topup','This credit payment cannot be finalized.')
        if row['status']=='paid':
            if not c.execute("SELECT id FROM credit_journals WHERE source=? AND sealed=1", ('invoice:'+invoice_id,)).fetchone():
                raise CreditError('ledger_mismatch','The payment needs reconciliation before it can be used.',503)
            return
        from . import payments
        if not isinstance(proof,dict) or proof.get('signature')!=row['signature'] or proof.get('genesis')!=payments.GENESIS:
            raise CreditError('invalid_receipt','A verified mainnet payment receipt is required.',422)
        if not payments.validate_receipt(dict(row),proof.get('transaction'),proof.get('status')):
            raise CreditError('unfinalized_receipt','The payment has not finalized. No credit was added.',422)
        encoded=json.dumps(proof,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(encoded.encode())>262144:raise CreditError('receipt_too_large','The payment receipt needs manual reconciliation.',503)
        proof_hash=hashlib.sha256(encoded.encode()).hexdigest()
        c.execute('INSERT INTO credit_receipts VALUES(?,?,?,?,?)',(invoice_id,row['signature'],encoded,proof_hash,int(time.time())))
        _post(c,row['user_id'],'topup','invoice:'+invoice_id,row['amount']*10000,
              {'invoice_id':invoice_id,'signature':row['signature'],'mint':row['mint'],'recipient':row['recipient'],'receipt_sha256':proof_hash})
        c.execute("UPDATE invoices SET status='paid',paid_at=? WHERE id=? AND status='pending'", (int(time.time()),invoice_id))
        c.execute('INSERT INTO audit(user_id,action,created) VALUES(?,?,?)',
                  (row['user_id'],'Service credit payment finalized',int(time.time())))


def _release(c,row):
    if row['status']!='reserved': return
    c.execute('UPDATE credit_accounts SET reserved_micro=reserved_micro-? WHERE user_id=?', (row['reserved_micro']-row['trial_reserved_micro'],row['user_id']))
    if row['trial_reserved_micro']:
        c.execute('UPDATE trial_accounts SET reserved_micro=reserved_micro-? WHERE user_id=?',(row['trial_reserved_micro'],row['user_id']))
    c.execute('UPDATE credit_budgets SET reserved_micro=reserved_micro-? WHERE user_id=? AND day=?',
              (row['reserved_micro'],row['user_id'],row['day']))
    c.execute("UPDATE credit_requests SET status='released' WHERE id=? AND status='reserved'", (row['id'],))


def _expire(c,user_id,stamp):
    rows=c.execute("SELECT * FROM credit_requests WHERE user_id=? AND status='reserved' AND expires<? LIMIT 256", (user_id,stamp)).fetchall()
    for row in rows: _release(c,row)
    # Keep accounting and the request ID permanently; bound the large reply cache.
    c.execute('''UPDATE credit_requests SET response=NULL WHERE id IN
      (SELECT id FROM credit_requests WHERE user_id=? AND status='completed'
       AND created<? AND response IS NOT NULL LIMIT 128)''',(user_id,stamp-REPLAY_SECONDS))


def snapshot(user_id):
    stamp=int(time.time());day=stamp//86400*86400
    with db() as c:
        c.execute('BEGIN IMMEDIATE');_account(c,user_id);_expire(c,user_id,stamp)
        a=dict(c.execute('SELECT * FROM credit_accounts WHERE user_id=?',(user_id,)).fetchone())
        settings=dict(c.execute('SELECT * FROM credit_settings WHERE user_id=?',(user_id,)).fetchone())
        budget=c.execute('SELECT * FROM credit_budgets WHERE user_id=? AND day=?',(user_id,day)).fetchone()
        ledger=[dict(r) for r in c.execute('''SELECT j.id,j.kind,j.created,e.delta_micro,j.metadata
          FROM credit_entries e JOIN credit_journals j ON j.id=e.journal_id
          WHERE e.account_id=? AND j.sealed=1 ORDER BY e.id DESC LIMIT 30''', (a['id'],))]
        from .products import trial_snapshot
        trial=trial_snapshot(c,user_id)
        topups=[dict(r) for r in c.execute("SELECT id,amount,status,created,expires,payer,recipient,mint,signature,paid_at FROM invoices WHERE user_id=? AND purpose='credits' ORDER BY created DESC LIMIT 20",(user_id,))]
    for r in ledger: r['metadata']=json.loads(r['metadata'])
    return {'currency':'USDC','decimals':6,'balance_micro':a['balance_micro'],'reserved_micro':a['reserved_micro'],
            'available_micro':a['balance_micro']-a['reserved_micro'],'enabled':bool(settings['enabled']),
            'daily_limit_micro':settings['daily_limit_micro'],'today_spent_micro':budget['spent_micro'] if budget else 0,
            'today_reserved_micro':budget['reserved_micro'] if budget else 0,'terms_version':settings['terms_version'],
            'price_version':VERSION,'rates':RATES,'ledger':ledger,'topups':topups,'trial':trial,
            'total_available_micro':a['balance_micro']-a['reserved_micro']+trial['available_micro'],
            'stream_enabled':bool(settings['stream_enabled']),
            'service':'Retained data reads; partial coverage and provisional observations. No live feed or latency SLA.',
            'x402_enabled':False,'zoneless_connected':False}


def configure(user_id,enabled,daily_limit_micro,accepted_terms,stream_enabled=None):
    if type(daily_limit_micro) is not int or not 5<=daily_limit_micro<=1_000_000_000:
        raise CreditError('invalid_budget','Choose a daily budget between 0.000005 and 1,000 USDC.',422)
    if enabled and not accepted_terms:
        raise CreditError('accept_terms','Confirm the prepaid data scope and charges before enabling reads.',422)
    with db() as c:
        c.execute('BEGIN IMMEDIATE');_account(c,user_id)
        day=int(time.time())//86400*86400
        budget=c.execute('SELECT spent_micro,reserved_micro FROM credit_budgets WHERE user_id=? AND day=?',(user_id,day)).fetchone()
        if enabled and budget and daily_limit_micro<budget['spent_micro']+budget['reserved_micro']:
            raise CreditError('budget_below_usage','This limit is below today’s charged and reserved amount.',422)
        c.execute('UPDATE credit_settings SET enabled=?,daily_limit_micro=?,terms_version=?,updated=? WHERE user_id=?',
                  (int(enabled),daily_limit_micro,VERSION if accepted_terms else None,int(time.time()),user_id))
        if stream_enabled is not None:
            if stream_enabled and not accepted_terms:raise CreditError('accept_terms','Confirm stream time and bandwidth charges first.',422)
            c.execute('UPDATE credit_settings SET stream_enabled=? WHERE user_id=?',(int(stream_enabled),user_id))
    return snapshot(user_id)


def reserve(user_id,key_id,idem,product,query):
    if not isinstance(idem,str) or not re.fullmatch('[A-Za-z0-9._:-]{8,100}',idem):
        raise CreditError('idempotency_required','Send an Idempotency-Key of 8–100 letters, digits, dots, colons, underscores or hyphens.',422)
    if product not in RATES: raise CreditError('unsupported_product','This data product is unavailable.',422)
    fingerprint=hashlib.sha256(json.dumps({'product':product,'query':query},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    stamp=int(time.time());day=stamp//86400*86400;amount=RATES[product]['max_micro']
    with db() as c:
        c.execute('BEGIN IMMEDIATE');a=_account(c,user_id);_expire(c,user_id,stamp)
        def deny(code,message,status=409):
            # Cleanup is safe to commit even when a new read is denied. Otherwise
            # polling an abandoned request after a restart could retain its hold.
            c.commit()
            raise CreditError(code,message,status)
        old=c.execute('SELECT * FROM credit_requests WHERE user_id=? AND idem=?',(user_id,idem)).fetchone()
        if old:
            if old['fingerprint']!=fingerprint: deny('idempotency_conflict','This request ID was already used for a different query.')
            if old['status']=='completed': return dict(old)
            if old['status']=='released': deny('request_released','The prior attempt was not charged. Use a new request ID to retry.')
            deny('request_in_progress','This request is still running. Retry the same request ID shortly.')
        settings=c.execute('SELECT * FROM credit_settings WHERE user_id=?',(user_id,)).fetchone()
        if not settings['enabled'] or settings['terms_version']!=VERSION:
            deny('prepaid_disabled','Enable prepaid data reads in Console → Billing first.',403)
        # _expire may have released an older hold; always reload the balance.
        a=c.execute('SELECT * FROM credit_accounts WHERE user_id=?',(user_id,)).fetchone()
        if product=='stream' and not settings['stream_enabled']:
            deny('stream_disabled','Enable prepaid stream time and bandwidth in Billing first.',403)
        trial=c.execute('SELECT * FROM trial_accounts WHERE user_id=?',(user_id,)).fetchone()
        promo=min(amount,trial['balance_micro']-trial['reserved_micro']) if trial else 0
        if a['balance_micro']-a['reserved_micro']<amount-promo:
            deny('insufficient_credit','Not enough service credit for this read. Top up in Console → Billing.',402)
        c.execute('INSERT OR IGNORE INTO credit_budgets(user_id,day) VALUES(?,?)',(user_id,day))
        budget=c.execute('SELECT * FROM credit_budgets WHERE user_id=? AND day=?',(user_id,day)).fetchone()
        if budget['spent_micro']+budget['reserved_micro']+amount>settings['daily_limit_micro']:
            deny('daily_budget','Your prepaid daily budget is reached. No extra charge was made.',429)
        c.execute('UPDATE credit_accounts SET reserved_micro=reserved_micro+? WHERE user_id=?',(amount-promo,user_id))
        if promo:c.execute('UPDATE trial_accounts SET reserved_micro=reserved_micro+? WHERE user_id=?',(promo,user_id))
        c.execute('UPDATE credit_budgets SET reserved_micro=reserved_micro+? WHERE user_id=? AND day=?',(amount,user_id,day))
        ident='cr_'+secrets.token_hex(12)
        c.execute("INSERT INTO credit_requests(id,user_id,key_id,idem,fingerprint,product,price_version,reserved_micro,day,created,expires,trial_reserved_micro,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'reserved')",
                  (ident,user_id,key_id,idem,fingerprint,product,VERSION,amount,day,stamp,stamp+RESERVATION_SECONDS,promo))
        return dict(c.execute('SELECT * FROM credit_requests WHERE id=?',(ident,)).fetchone())


def release(ident):
    with db() as c:
        c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM credit_requests WHERE id=?',(ident,)).fetchone()
        if row:_release(c,row)


def complete(ident,data,meter=None):
    # Validate/serialize the exact result once; this byte string is also replayed.
    body=json.dumps(data,separators=(',',':'),ensure_ascii=True,allow_nan=False)
    with db() as c:
        c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM credit_requests WHERE id=?',(ident,)).fetchone()
        if not row or row['status']!='reserved': raise CreditError('request_released','This request was released without a charge.')
        if row['expires']<int(time.time()): raise CreditError('request_expired','This read took too long and was not charged.',503)
        active=c.execute('SELECT revoked FROM api_keys WHERE id=? AND user_id=?',(row['key_id'],row['user_id'])).fetchone()
        if not active or active['revoked'] is not None: raise CreditError('key_revoked','The API key was revoked. This read was not charged.',401)
        settings=c.execute('SELECT enabled,terms_version FROM credit_settings WHERE user_id=?',(row['user_id'],)).fetchone()
        if not settings or not settings['enabled'] or settings['terms_version']!=VERSION:
            raise CreditError('prepaid_disabled','Prepaid reads were disabled. This read was not charged.',403)
        rate=RATES[row['product']];size=len(body.encode())
        if size>MAX_BODY or size>rate['bytes_per_unit']*2 or row['product']=='price' and size>rate['bytes_per_unit']:
            raise CreditError('response_too_large','This response exceeds the prepaid page size. Use a smaller limit. No charge was made.',422)
        if row['product']=='stream':
            if not isinstance(meter,dict) or not isinstance(meter.get('seconds'),(int,float)) or not math.isfinite(meter['seconds']) or not 0<=meter['seconds']<=60 or type(meter.get('bytes')) is not int or not 0<meter['bytes']<=262144:
                raise CreditError('invalid_stream_meter','Invalid bounded stream meter. No charge was made.',503)
            amount=math.ceil(meter['seconds']/15)*100+math.ceil(meter['bytes']/32768)*50
        else:amount=rate['base_micro']*max(1,math.ceil(size/rate['bytes_per_unit']))
        if amount>row['reserved_micro']: raise CreditError('price_mismatch','The read exceeded its quoted budget. No charge was made.',503)
        _release(c,row)
        promo=min(amount,row['trial_reserved_micro'])
        if promo:c.execute('INSERT INTO trial_entries(user_id,source,delta_micro,created) VALUES(?,?,?,?)',(row['user_id'],'request:'+ident,-promo,int(time.time())))
        if amount-promo:_post(c,row['user_id'],'usage','request:'+ident,amount-promo,{'request_id':ident,'product':row['product'],
              'key_id':row['key_id'],'response_bytes':size,'price_version':row['price_version'],'meter':meter})
        c.execute('UPDATE credit_budgets SET spent_micro=spent_micro+? WHERE user_id=? AND day=?',(amount,row['user_id'],row['day']))
        c.execute("UPDATE credit_requests SET status='completed',charged_micro=?,trial_charged_micro=?,response=?,response_sha256=? WHERE id=?",
                  (amount,promo,body,hashlib.sha256(body.encode()).hexdigest(),ident))
        c.execute('UPDATE api_keys SET last_used=? WHERE id=?',(int(time.time()),row['key_id']))
        return dict(c.execute('SELECT * FROM credit_requests WHERE id=?',(ident,)).fetchone())


def reconcile():
    """Small indexed accounting check; never rewrites a discrepancy."""
    with db() as c:
        balances=[dict(r) for r in c.execute('''SELECT a.id,a.balance_micro,COALESCE(SUM(e.delta_micro),0) posted
          FROM credit_accounts a LEFT JOIN credit_entries e ON e.account_id=a.id GROUP BY a.id
          HAVING a.balance_micro!=COALESCE(SUM(e.delta_micro),0)''')]
        journals=[r['id'] for r in c.execute('''SELECT j.id FROM credit_journals j LEFT JOIN credit_entries e ON e.journal_id=j.id
          GROUP BY j.id HAVING j.sealed!=1 OR COUNT(e.id)!=2 OR SUM(e.delta_micro)!=0''')]
        holds=[r['id'] for r in c.execute('''SELECT a.id FROM credit_accounts a LEFT JOIN credit_requests r
          ON r.user_id=a.user_id AND r.status='reserved' WHERE a.kind='customer' GROUP BY a.id
          HAVING a.reserved_micro!=COALESCE(SUM(r.reserved_micro-r.trial_reserved_micro),0)''')]
        budgets=[dict(r) for r in c.execute('''SELECT b.user_id,b.day FROM credit_budgets b LEFT JOIN credit_requests r
          ON r.user_id=b.user_id AND r.day=b.day GROUP BY b.user_id,b.day
          HAVING b.reserved_micro!=COALESCE(SUM(CASE WHEN r.status='reserved' THEN r.reserved_micro ELSE 0 END),0)
          OR b.spent_micro!=COALESCE(SUM(CASE WHEN r.status='completed' THEN r.charged_micro ELSE 0 END),0)''')]
        trials=[dict(r) for r in c.execute('''SELECT a.user_id FROM trial_accounts a LEFT JOIN trial_entries e ON e.user_id=a.user_id GROUP BY a.user_id HAVING a.balance_micro!=COALESCE(SUM(e.delta_micro),0)''')]
        trial_holds=[dict(r) for r in c.execute('''SELECT a.user_id FROM trial_accounts a LEFT JOIN credit_requests r ON r.user_id=a.user_id AND r.status='reserved' GROUP BY a.user_id HAVING a.reserved_micro!=COALESCE(SUM(r.trial_reserved_micro),0)''')]
    return {'passed':not(balances or journals or holds or budgets or trials or trial_holds),'trial_mismatches':trials,'trial_reservation_mismatches':trial_holds,'balance_mismatches':balances,
            'unbalanced_journals':journals,'reservation_mismatches':holds,'budget_mismatches':budgets}
