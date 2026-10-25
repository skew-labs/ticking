"""Unit checks for the engine side of first-class EVM networks (identity, EvmHistory, commit dependencies).

usage (Canada host, staging engine copy): cd fc/engine && <live-python> -m unittest ../src/tests/test_engine_evm.py
A temporary hot store uses em-charts' exact DDL; a loopback stub plays the deep archive. Nothing production is opened.
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.getcwd())
import identity  # noqa: E402
from evm_history import EvmHistory  # noqa: E402
from commit_notifications import dependencies  # noqa: E402

TOKEN, POOL, TX = '0x' + 'ab' * 20, '0x' + 'cd' * 20, '0x' + 'ef' * 32
DDL = """CREATE TABLE fills(identity TEXT PRIMARY KEY,pool TEXT,time INTEGER,slot INTEGER,tx_order INTEGER,event_order INTEGER,price REAL,base_volume REAL,quote_volume REAL);
CREATE INDEX ftime ON fills(pool,time);CREATE INDEX fslot ON fills(slot);CREATE INDEX ford ON fills(pool,slot,tx_order,event_order,identity);
CREATE INDEX reference_time ON fills(time,slot,tx_order,event_order,identity);
CREATE TABLE bars(pool TEXT,interval INTEGER,time INTEGER,open REAL,high REAL,low REAL,close REAL,base_volume REAL,quote_volume REAL,count INTEGER,first_order TEXT,last_order TEXT,PRIMARY KEY(pool,interval,time));
CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE fill_meta(identity TEXT PRIMARY KEY,token TEXT,side TEXT,trader TEXT,tx TEXT,usd REAL,eligible INTEGER);
CREATE INDEX fm_token ON fill_meta(token);CREATE TABLE pools(pool TEXT PRIMARY KEY,token TEXT,quote TEXT);"""
NOW = (int(time.time()) // 3600) * 3600
HOT_FROM = NOW - 2 * 3600 + 1800          # hot store starts mid-hour: that hour must come from deep
DEEP = {'fail': False, 'calls': 0, 'candles': 0}


class Deep(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        DEEP['calls'] += 1
        if DEEP['fail']:
            self.send_response(503); self.end_headers(); return
        u = urlsplit(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        DEEP['candles'] += u.path.endswith('/candles')
        if u.path.startswith('/api/public/markets/'):
            body = {'firstTime': (NOW - 30 * 86400) * 1000, 'dataUntil': NOW * 1000, 'pools': {'items': [{'address': POOL, 'quote': 'USDG', 'swaps': 9, 'usdVolume': 1.0, 'rank': 1}]}}
        elif u.path == '/api/public/markets':
            body = {'tokens': [{'network': q['network'], 'address': TOKEN, 'symbol': 'TST', 'price': 2.0}]}
        else:
            before = int(q.get('before') or (NOW + 3600) * 1000) // 1000
            hours = [t for t in range(NOW - 10 * 3600, NOW + 3600, 3600) if t < before][-int(q['limit']):]
            body = {'candles': [dict(time=t * 1000, open=1.0, high=1.5, low=0.5, close=1.0, volume=10.0, baseVolume=5.0, count=1) for t in hours],
                    'hasMore': True, 'history': {'start': (NOW - 30 * 86400) * 1000, 'resolutionStarts': {'1m': (NOW - 60 * 86400) * 1000, '1h': None}}}
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


class EvmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        os.makedirs(cls.dir + '/robinhood')
        c = sqlite3.connect(cls.dir + '/robinhood/history.sqlite3'); c.executescript(DDL)
        for key in ('token:' + TOKEN, POOL):
            for t in range(HOT_FROM, NOW + 60, 60):
                c.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (key, 60, t, 2.0, 2.0, 2.0, 2.0, 1.0, 2.0, 1, '', ''))
        for i in range(5):
            ident = '%s:%d' % (TX, i)
            c.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?)', (ident, POOL, NOW - i, 100 - i, 0, i, 2.0, 1.0, 2.0))
            c.execute('INSERT INTO fill_meta VALUES(?,?,?,?,?,?,?)', (ident, TOKEN, 'B' if i % 2 else 'A', '0x' + '12' * 20, TX, 2.0, 1))
        c.execute("INSERT INTO pools VALUES(?,?,?)", (POOL, TOKEN, '0x' + '00' * 20))
        c.execute("INSERT INTO state VALUES('data_version','3'),('data_until_ms',?)", (str(NOW * 1000),)); c.commit(); c.close()
        cls.srv = ThreadingHTTPServer(('127.0.0.1', 0), Deep)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.evm = EvmHistory('robinhood', hot_root=cls.dir, deep='http://127.0.0.1:%d' % cls.srv.server_port)

    def test_identity(self):
        self.assertTrue(identity.valid_address('robinhood', TOKEN))
        self.assertFalse(identity.valid_address('robinhood', TOKEN.upper().replace('0X', '0x')))   # exact lowercase identity only
        self.assertFalse(identity.valid_address('arbitrum', 'So11111111111111111111111111111111111111112'))
        self.assertTrue(identity.valid_pool('arbitrum', '0x' + 'ab' * 32))
        self.assertRaises(ValueError, identity.network_of, 'base')
        self.assertEqual(identity.network_of(None), 'solana')

    def test_seam_uses_deep_before_the_first_full_hot_bucket(self):
        d = self.evm.history('token:' + TOKEN, 3600, 6)
        times = [b['time'] for b in d['candles']]
        self.assertEqual(times, sorted(set(times)))
        by = {b['time']: b for b in d['candles']}
        self.assertEqual(by[NOW - 2 * 3600]['close'], 1.0)    # partial hot hour comes from deep
        self.assertEqual(by[NOW - 3600]['close'], 2.0)        # full hot hours come from the hot store
        self.assertEqual(d['identity'], {'network': 'robinhood', 'market': TOKEN, 'baseMint': TOKEN, 'quoteMint': 'USD', 'series': 'reference',
                                         'historyPolicy': 'em_pool_reference_v2', 'venue': 'onchain_dex', 'aggregation': 'em_pool_reference_v2'})
        self.assertEqual(d['coverage']['start'], NOW - 30 * 86400)

    def test_minute_page_is_served_by_the_hot_store_alone(self):
        calls = DEEP['candles']; d = self.evm.history(POOL, 60, 30)
        self.assertEqual(len(d['candles']), 30); self.assertEqual(DEEP['candles'], calls)
        self.assertEqual(d['identity']['pool'], POOL)

    def test_deep_outage_answers_from_the_last_good_copy(self):
        first = self.evm.history('token:' + TOKEN, 3600, 8, before=(NOW - 4 * 3600))
        DEEP['fail'] = True
        try:
            self.evm.memo = {k: (0, 0, v[2]) for k, v in self.evm.memo.items()}   # expire every copy
            again = self.evm.history('token:' + TOKEN, 3600, 8, before=(NOW - 4 * 3600))
        finally:
            DEEP['fail'] = False
        self.assertEqual([b['time'] for b in first['candles']], [b['time'] for b in again['candles']])

    def test_tapes_are_block_ordered_with_side_trader_and_mint(self):
        t = self.evm.trades(TOKEN, 5)['trades']
        self.assertEqual([r['slot'] for r in t], sorted(r['slot'] for r in t))
        self.assertTrue(all(r['mint'] == TOKEN and r['side'] in ('A', 'B') and r['quote_mint'] == 'USD' and r['signature'] == TX for r in t))
        p = self.evm.pool_trades(POOL, 3)
        self.assertEqual(len(p['trades']), 3); self.assertEqual(p['identity']['pool'], POOL)

    def test_carry_is_bounded_and_flagged(self):
        d = self.evm.history('token:' + TOKEN, 3600, 12, gap_policy='carry')
        self.assertEqual(d['fillPolicy'], 'recorded')
        self.assertTrue(all(b['count'] == 0 and b['volume'] == 0 for b in d['candles'] if b['synthetic']))

    def test_resolve_and_catalog(self):
        r = self.evm.resolve(token=TOKEN)
        self.assertEqual(r['preferredPool']['pool'], POOL); self.assertEqual(r['network'], 'robinhood')
        self.assertEqual(self.evm.resolve(pool=POOL)['pools'][0]['baseMint'], TOKEN)
        self.assertEqual(self.evm.catalog()['tokens'][0]['address'], TOKEN)
        self.assertRaises(ValueError, self.evm.resolve, token=TOKEN, quote_mint='So11111111111111111111111111111111111111112')

    def test_commit_dependencies(self):
        self.assertEqual(dependencies('/token/candles', {'network': ['arbitrum']}), frozenset(('evm_arbitrum', 'config')))
        self.assertIn('token', dependencies('/token/candles', {}))   # Solana unchanged


if __name__ == '__main__':
    unittest.main()
