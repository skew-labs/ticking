"""Billing quotes, immutable invoice terms, and one-time annual entitlements."""
import base64
import copy
import sqlite3
import struct

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.signature import Signature
from solders.transaction import Transaction

from . import app as api, payments as pay, store
from .test_console import client, signup, receipt, verified_receipt


def create(client, period='yearly', payer=None, **overrides):
    response = client.post('/api/billing/invoices', json={
        'plan': 'Data', 'payer': payer or str(Keypair().pubkey()),
        'billing_period': period, 'accepted_preview': True, **overrides,
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('plan,monthly,yearly', [
    ('Charts', 2900, 27840), ('Data', 5900, 56640),
])
def test_catalog_and_invoice_use_same_integer_quote(client, plan, monthly, yearly):
    signup(client)
    catalog = client.get('/api/config').json()['plans'][plan]['billing']
    assert catalog['monthly'] == {'amount': monthly, 'term_days': 30, 'discount_percent': 0}
    assert catalog['yearly'] == {'amount': yearly, 'term_days': 365, 'discount_percent': 20}
    inv = create(client, plan=plan, amount=1, term_days=9999, discount_percent=100)
    assert (inv['amount'], inv['term_days'], inv['billing_period']) == (yearly, 365, 'yearly')


def test_period_deduplication_and_default_compatibility(client):
    signup(client)
    payer = str(Keypair().pubkey())
    monthly = create(client, 'monthly', payer)
    yearly = create(client, 'yearly', payer)
    assert monthly['id'] != yearly['id']
    assert create(client, 'monthly', payer)['id'] == monthly['id']
    assert create(client, 'yearly', payer)['id'] == yearly['id']
    legacy = client.post('/api/billing/invoices', json={
        'plan': 'Data', 'payer': payer, 'accepted_preview': True,
    }).json()
    assert legacy['id'] == monthly['id']
    assert (legacy['amount'], legacy['term_days']) == (5900, 30)
    rows = client.get('/api/console').json()['invoices']
    assert {(i['billing_period'], i['term_days']) for i in rows} == {('monthly', 30), ('yearly', 365)}
    for period in ['annual', '', 'weekly', None]:
        assert client.post('/api/billing/invoices', json={
            'plan': 'Data', 'payer': payer, 'accepted_preview': True, 'billing_period': period,
        }).status_code == 422


def test_unsigned_annual_amount_and_underpayment_rejected(client, monkeypatch):
    signup(client)
    inv = create(client)
    def rpc(method, params=None):
        if method == 'getGenesisHash': return pay.GENESIS
        if method == 'getLatestBlockhash':
            return {'value': {'blockhash': str(Hash.default()), 'lastValidBlockHeight': 123}}
        raise AssertionError(method)
    monkeypatch.setattr(pay, 'rpc', rpc)
    transaction = Transaction.from_bytes(base64.b64decode(pay.unsigned_transaction(inv)['transaction']))
    assert struct.unpack('<BQB', transaction.message.instructions[1].data) == (12, 566400000, 6)
    assert all(sig == Signature.default() for sig in transaction.signatures)
    tx, status = receipt(inv)
    assert pay.validate_receipt(inv, tx, status)
    underpaid = copy.deepcopy(tx)
    underpaid['transaction']['message']['instructions'][0]['parsed']['info']['tokenAmount']['amount'] = '59000000'
    underpaid['meta']['postTokenBalances'][0]['uiTokenAmount']['amount'] = '59000000'
    with pytest.raises(pay.PaymentError): pay.validate_receipt(inv, underpaid, status)


def test_annual_credit_once_renewal_preserves_remaining_time_and_terms(client, monkeypatch):
    user = signup(client)
    clock = api.now()
    monkeypatch.setattr(api, 'now', lambda: clock)
    monkeypatch.setattr(api, 'verify_invoice', verified_receipt)
    monthly = create(client, 'monthly')
    assert client.post('/api/billing/invoices/' + monthly['id'] + '/verify', json={
        'signature': str(Signature.new_unique()),
    }).json()['status'] == 'paid'
    assert client.get('/api/console').json()['plan']['expires'] == clock + 30 * 86400

    clock += 5 * 86400
    annual = create(client)
    signature = str(Signature.new_unique())
    # A later price/policy change must not alter an already issued invoice.
    monkeypatch.setitem(pay.PLANS['Data'], 'usd', 200)
    monkeypatch.setitem(pay.BILLING_PERIODS['yearly'], 'term_days', 400)
    for _ in range(2):
        confirmed = client.post('/api/billing/invoices/' + annual['id'] + '/verify', json={'signature': signature}).json()
        assert (confirmed['status'], confirmed['amount'], confirmed['term_days']) == ('paid', 56640, 365)
    active = client.get('/api/console').json()['plan']
    assert active['expires'] == clock + (25 + 365) * 86400
    assert active['billing_period'] == 'yearly' and active['term_days'] == 365
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM entitlements WHERE user_id=?', (user['id'],)).fetchone()[0] == 2


def test_existing_invoices_migrate_as_monthly_without_repricing(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.sqlite3'
    monkeypatch.setattr(store, 'DB_PATH', path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE invoices(id TEXT PRIMARY KEY,user_id TEXT,plan TEXT,amount INTEGER,method TEXT,status TEXT,created INTEGER,expires INTEGER,payer TEXT,reference TEXT,signature TEXT,provider_id TEXT,paid_at INTEGER)')
        db.execute("INSERT INTO invoices(id,plan,amount,status) VALUES('old-invoice','Build',14900,'paid')")
    store.initialize()
    store.initialize()
    with store.db() as db:
        inv = dict(db.execute("SELECT * FROM invoices WHERE id='old-invoice'").fetchone())
    assert (inv['amount'], inv['billing_period'], inv['term_days'], inv['status']) == (14900, 'monthly', 30, 'paid')
    assert api.invoice_public(inv)['term_days'] == 30
