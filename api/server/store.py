"""Durable, isolated console state. Secrets are one-way hashes, never plaintext."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get('TICKING_DB', './api/.state/console.sqlite3'))

@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def initialize():
    DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL,password TEXT NOT NULL,created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_identities(subject TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),profile TEXT NOT NULL,created INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS identity_user ON auth_identities(user_id);
        CREATE TABLE IF NOT EXISTS auth_link_intents(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),session_hash TEXT NOT NULL,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS api_keys(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),name TEXT NOT NULL,hash TEXT UNIQUE NOT NULL,prefix TEXT NOT NULL,scopes TEXT NOT NULL,created INTEGER NOT NULL,last_used INTEGER,revoked INTEGER);
        CREATE TABLE IF NOT EXISTS display_tokens(hash TEXT PRIMARY KEY,key_id TEXT NOT NULL REFERENCES api_keys(id),origin TEXT NOT NULL,mint TEXT NOT NULL,pool TEXT NOT NULL,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS usage_events(id INTEGER PRIMARY KEY,user_id TEXT NOT NULL,key_id TEXT,endpoint TEXT NOT NULL,created INTEGER NOT NULL,units INTEGER NOT NULL DEFAULT 1);
        CREATE INDEX IF NOT EXISTS usage_user_time ON usage_events(user_id,created);
        CREATE TABLE IF NOT EXISTS feeds(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),name TEXT NOT NULL,markets TEXT NOT NULL,interval TEXT NOT NULL,created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS invoices(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),plan TEXT NOT NULL,amount INTEGER NOT NULL,method TEXT NOT NULL,status TEXT NOT NULL,created INTEGER NOT NULL,expires INTEGER NOT NULL,payer TEXT,reference TEXT UNIQUE,signature TEXT UNIQUE,provider_id TEXT UNIQUE,paid_at INTEGER);
        CREATE TABLE IF NOT EXISTS entitlements(user_id TEXT NOT NULL REFERENCES users(id),source TEXT UNIQUE NOT NULL,plan TEXT NOT NULL,expires INTEGER NOT NULL,PRIMARY KEY(user_id,source));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,user_id TEXT NOT NULL,action TEXT NOT NULL,created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS rate_limits(bucket TEXT PRIMARY KEY,count INTEGER NOT NULL,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS listing_requests(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL,mint TEXT NOT NULL,name TEXT NOT NULL,symbol TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('pending','in_review','listed','rejected')),analysis TEXT,public_note TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL,updated INTEGER NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS listing_active_mint ON listing_requests(mint) WHERE status IN ('pending','in_review','listed');
        CREATE TABLE IF NOT EXISTS listing_reviews(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL REFERENCES listing_requests(id),reviewer TEXT NOT NULL,decision TEXT NOT NULL,note TEXT NOT NULL,created INTEGER NOT NULL);
        ''')
        columns={r['name'] for r in c.execute('PRAGMA table_info(invoices)')}
        if 'aggregation' not in {r['name'] for r in c.execute('PRAGMA table_info(display_tokens)')}:
            c.execute("ALTER TABLE display_tokens ADD COLUMN aggregation TEXT NOT NULL DEFAULT 'pool' CHECK(aggregation IN ('pool','token'))")
        if 'quote_mint' not in {r['name'] for r in c.execute('PRAGMA table_info(display_tokens)')}:
            c.execute("ALTER TABLE display_tokens ADD COLUMN quote_mint TEXT")
        c.execute('CREATE TABLE IF NOT EXISTS exchange_display_tokens(hash TEXT PRIMARY KEY,key_id TEXT NOT NULL REFERENCES api_keys(id),origin TEXT NOT NULL,asset TEXT NOT NULL,market TEXT NOT NULL,source TEXT NOT NULL,quote TEXT NOT NULL,expires INTEGER NOT NULL)')
        for name in ('recipient','mint'):
            if name not in columns: c.execute('ALTER TABLE invoices ADD COLUMN '+name+' TEXT')
        if 'billing_period' not in columns:
            c.execute("ALTER TABLE invoices ADD COLUMN billing_period TEXT NOT NULL DEFAULT 'monthly' CHECK(billing_period IN ('monthly','yearly'))")
        if 'term_days' not in columns:
            c.execute('ALTER TABLE invoices ADD COLUMN term_days INTEGER NOT NULL DEFAULT 30 CHECK(term_days IN (30,365))')
        if 'purpose' not in columns:
            c.execute("ALTER TABLE invoices ADD COLUMN purpose TEXT NOT NULL DEFAULT 'plan' CHECK(purpose IN ('plan','credits'))")
        from .credits import initialize as initialize_credits
        initialize_credits(c)
        from .products import initialize as initialize_products
        initialize_products(c)
        from .chart_watches import initialize as initialize_watches
        initialize_watches(c)
    os.chmod(DB_PATH, 0o600)
