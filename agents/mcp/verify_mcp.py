#!/usr/bin/env python3
"""MCP verification: calls every tool over Streamable HTTP and compares with independent ground truth.
Usage: verify_mcp.py [base_url ...]   (key read from /root/.secrets/ticking_mcp_test_key; never printed)"""
import json, sys, time, urllib.request, urllib.error
KEY = open('/root/.secrets/ticking_mcp_test_key').read().strip()
VIT = '0xd8da6bf26964af9d7eed9e03e53415d37aa96045'
BIN = '0x28c6c06298d514db089934071355e5743bf21d60'   # Binance 14 hot wallet (Ethereum)
TRUTH_RPC = {'ethereum': 'https://eth.llamarpc.com', 'arbitrum': 'https://arb1.arbitrum.io/rpc', 'base': 'https://mainnet.base.org',
             'optimism': 'https://mainnet.optimism.io', 'linea': 'https://rpc.linea.build', 'polygon': 'https://polygon-rpc.com',
             'unichain': 'https://mainnet.unichain.org', 'robinhood': 'https://robinhood.drpc.org', 'arc': 'https://arc.drpc.org',
             'hyperevm': 'https://hyperliquid-rpc.publicnode.com', 'xlayer': 'https://xlayerrpc.okx.com'}
IDS = {'ethereum': 1, 'optimism': 10, 'unichain': 130, 'polygon': 137, 'xlayer': 196, 'hyperevm': 999, 'robinhood': 4663, 'arc': 5042,
       'base': 8453, 'arbitrum': 42161, 'linea': 59144}
TOKENS = [('arbitrum', '0xaf88d065e77c8cc2239327c5edb3a432268e5831', 'USDC', 6), ('arbitrum', '0x912ce59144191c1204e64559fe8253a0e49e6548', 'ARB', 18),
          ('ethereum', '0xdac17f958d2ee523a2206206994597c13d831ec7', 'USDT', 6), ('ethereum', '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2', 'WETH', 18),
          ('base', '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913', 'USDC', 6)]
RES = []


def rec(area, name, ok, ev):
    RES.append((area, name, 'PASS' if ok is True else ('WARN' if ok is None else 'FAIL'), ev))


def http(url, body=None, headers=None, method=None):
    h = {'content-type': 'application/json', 'accept': 'application/json, text/event-stream', 'user-agent': 'ticking-mcp-verify/1'}
    h.update(headers or {})
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


EXTRA = {'ethereum': ['https://1rpc.io/eth', 'https://ethereum-rpc.publicnode.com'], 'polygon': ['https://1rpc.io/matic', 'https://polygon-bor-rpc.publicnode.com'], 'arbitrum': ['https://1rpc.io/arb'],
         'base': ['https://1rpc.io/base', 'https://base-rpc.publicnode.com'], 'optimism': ['https://1rpc.io/op'], 'linea': ['https://1rpc.io/linea'], 'robinhood': ['https://robinhood-rpc.publicnode.com'],
         'arc': ['https://arc-rpc.publicnode.com'], 'hyperevm': ['https://rpc.hyperliquid.xyz/evm'], 'xlayer': ['https://rpc.xlayer.tech'], 'unichain': ['https://unichain-rpc.publicnode.com']}


def truth_rpc(chain, method, params):
    for url in [TRUTH_RPC[chain]] + EXTRA.get(chain, []):
        try:
            s, _, b = http(url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
            r = json.loads(b).get('result')
            if r is not None:
                return r
        except Exception:
            pass
    raise RuntimeError('no independent RPC answered %s on %s' % (method, chain))


class Client:
    def __init__(self, base):
        self.base, self.sid, self.n = base, None, 0

    def call(self, method, params=None, raw=False, extra=None):
        self.n += 1
        h = {'authorization': 'Bearer ' + KEY}
        if self.sid:
            h['mcp-session-id'] = self.sid
        h.update(extra or {})
        s, hd, b = http(self.base, {'jsonrpc': '2.0', 'id': self.n, 'method': method, 'params': params or {}}, h)
        if raw:
            return s, hd, b
        return s, hd, json.loads(b)

    def tool(self, name, args):
        t = time.time()
        s, hd, j = self.call('tools/call', {'name': name, 'arguments': args})
        res = j.get('result') or {}
        txt = (res.get('content') or [{}])[0].get('text')
        if res.get('isError'):
            return None, txt, time.time() - t
        return res.get('structuredContent') or json.loads(txt), None, time.time() - t


def envelope_ok(o):
    return all(k in o for k in ('chain', 'chain_id', 'as_of', 'as_of_utc', 'source', 'decimals', 'data'))


def run(base):
    c = Client(base)
    # ---- protocol
    s, hd, _ = http(base, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
    rec('protocol', 'no key -> 401', s == 401, 'HTTP %d' % s)
    s, hd, j = c.call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'verify', 'version': '1'}})
    c.sid = {k.lower(): v for k, v in hd.items()}.get('mcp-session-id')
    rec('protocol', 'initialize + Mcp-Session-Id', s == 200 and bool(c.sid) and j['result']['protocolVersion'] == '2025-06-18',
        'HTTP %d, session=%s, server=%s' % (s, bool(c.sid), j.get('result', {}).get('serverInfo')))
    s, _, _ = http(base, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, {'authorization': 'Bearer ' + KEY, 'mcp-session-id': c.sid})
    rec('protocol', 'notification -> 202', s == 202, 'HTTP %d' % s)
    s, hd, j = c.call('tools/list')
    names = [t['name'] for t in j['result']['tools']]
    rec('protocol', 'tools/list (10 tools)', len(names) == 10, ', '.join(names))
    s, hd, b = c.call('ping', raw=True, extra={'accept': 'text/event-stream'})
    rec('protocol', 'SSE response mode', s == 200 and 'text/event-stream' in {k.lower(): v for k, v in hd.items()}.get('content-type', '') and b.startswith('event: message'), b[:60].replace('\n', ' '))
    s, _, _ = http(base, {'jsonrpc': '2.0', 'id': 9, 'method': 'ping'}, {'authorization': 'Bearer ' + KEY, 'mcp-session-id': 'bogus'})
    rec('protocol', 'unknown session -> 404', s == 404, 'HTTP %d' % s)
    s, _, _ = http(base, None, {'authorization': 'Bearer ' + KEY}, method='GET')
    rec('protocol', 'GET -> 405', s == 405, 'HTTP %d' % s)
    # ---- console tools (forwarded; test account has no Data plan)
    for nm, args in (('list_markets', {}), ('get_candles', {'market': 'SOL-USDC', 'interval': '15m', 'limit': 3})):
        o, err, dt = c.tool(nm, args)
        rec('tool', nm + ' (forwarded to console)', True if o is not None else (None if err and 'entitlement' in err.lower() or 'Direct data' in (err or '') else False),
            'ok' if o is not None else 'isError: %s (expected: internal test key has no Data plan)' % err)
    # ---- wallet_profile vitalik
    o, err, dt = c.tool('wallet_profile', {'address': VIT})
    if o:
        d = o['data']
        xc = {x['chain']: x for x in d.get('chains_seen') or []}
        ok = envelope_ok(o) and d['name'] == 'vitalik.eth' and o['chain_id'] == IDS.get(o['chain']) and \
            (o['chain'] not in xc or d['first_seen'] == xc[o['chain']]['first_seen']) and d['first_seen'] >= 1622505600 if o['chain'] == 'arbitrum' else True
        rec('tool', 'wallet_profile vitalik', ok, 'chain=%s id=%s name=%s first_seen=%s (all chains %s) %.1fs' % (o['chain'], o['chain_id'], d['name'], d['first_seen_utc'], d['first_seen_all_chains'], dt))
    else:
        rec('tool', 'wallet_profile vitalik', False, err)
    o, err, dt = c.tool('wallet_profile', {'address': VIT, 'chain': 'ethereum'})
    rec('tool', 'wallet_profile vitalik ethereum', bool(o) and o['chain'] == 'ethereum' and o['chain_id'] == 1 and o['data']['first_seen'] == 1443428683,
        'first_seen=%s chain_id=%s' % ((o or {}).get('data', {}).get('first_seen_utc'), (o or {}).get('chain_id')) if o else err)
    # ---- Binance
    o, err, dt = c.tool('resolve_label', {'query': BIN})
    idn = ((o or {}).get('data') or {}).get('identity') or {}
    rec('tool', 'resolve_label Binance 14', bool(o) and idn.get('type') == 'exchange' and 'binance' in (idn.get('name') or '').lower(), '%s / %s' % (idn.get('name'), idn.get('source')) if o else err)
    o, err, dt = c.tool('resolve_label', {'query': 'binance'})
    d = (o or {}).get('data') or {}
    cur = d.get('curated') or []
    hit = next((x for x in cur if x['address'] == BIN), None)
    rec('tool', 'resolve_label name "binance"', bool(hit) and hit['license_reviewed'] is False and hit['grade'] == 'unreviewed',
        '%d curated + %d search + %d entities; Binance 14 license_reviewed=%s grade=%s' % (len(cur), len(d.get('results') or []), len(d.get('entities') or []), hit and hit['license_reviewed'], hit and hit['grade']))
    o, err, dt = c.tool('resolve_label', {'query': 'binance', 'exclude_unreviewed': True})
    rec('tool', 'resolve_label exclude_unreviewed', bool(o) and not o['data']['curated'], 'curated=%d' % len((o or {}).get('data', {}).get('curated') or []))
    o, err, dt = c.tool('wallet_profile', {'address': BIN, 'chain': 'ethereum'})
    rec('tool', 'wallet_profile Binance 14', bool(o) and 'Binance' in (o['data']['name'] or '') and o['chain_id'] == 1,
        'name=%s category=%s net_worth=%s' % (o['data']['name'], o['data']['category'], (o['data']['balance_sheet_usd'] or {}).get('net_worth')) if o else err)
    o, err, dt = c.tool('wallet_holdings', {'address': BIN, 'chain': 'ethereum', 'limit': 10})
    if o:
        pos = o['data']['positions']
        usdt = next((p for p in pos if (p['token'] or '') == '0xdac17f958d2ee523a2206206994597c13d831ec7'), None)
        held = sum(1 for p in pos if p['held_back'])
        bal = int(truth_rpc('ethereum', 'eth_call', [{'to': '0xdac17f958d2ee523a2206206994597c13d831ec7', 'data': '0x70a08231' + '0' * 24 + BIN[2:]}, 'latest']), 16) / 1e6
        amt_ok = usdt and usdt['amount'] is not None and abs(usdt['amount'] - bal) / max(bal, 1) < 0.05
        rec('tool', 'wallet_holdings Binance 14 (USDT vs chain)', True if amt_ok else (None if usdt and usdt['amount'] is None else False),
            'positions=%d held_back=%d USDT decimals=%s em_amount=%s chain_balance=%.0f' % (len(pos), held, usdt and usdt['decimals'], usdt and usdt['amount'], bal))
    else:
        rec('tool', 'wallet_holdings Binance 14', False, err)
    o, err, dt = c.tool('wallet_holdings', {'address': VIT, 'chain': 'arbitrum'})
    if o:
        rows = o['data']['pnl']['rows']
        weth = next((r for r in rows if r['symbol'] == 'WETH'), None)
        rec('tool', 'wallet_holdings vitalik arbitrum (+PnL)', envelope_ok(o) and o['chain_id'] == 42161,
            'positions=%d pnl_rows=%d WETH bal=%s avgCost=%s' % (len(o['data']['positions']), len(rows), weth and weth['balance'], weth and weth['avg_cost_usd']))
    else:
        rec('tool', 'wallet_holdings vitalik arbitrum', False, err)
    # ---- history
    o, err, dt = c.tool('wallet_history', {'address': VIT, 'chain': 'arbitrum', 'limit': 12})
    if o:
        txs = o['data']['transactions']
        bad_rh = [t for t in txs if 'Robinhood' in (t['text'] or '')]
        zero = [l for t in txs for l in t['legs'] if l['usd'] == 0]
        chk = txs[0] if txs else None
        rc = truth_rpc('arbitrum', 'eth_getTransactionReceipt', [chk['tx_hash']]) if chk else None
        blk_ok = rc and int(rc['blockNumber'], 16) == chk['block']
        rec('tool', 'wallet_history vitalik arbitrum', bool(txs) and not bad_rh and not zero and blk_ok,
            '%d txs; "Robinhood" texts=%d; usd==0 legs=%d; tx %s… block matches Arbitrum RPC=%s' % (len(txs), len(bad_rh), len(zero), chk and chk['tx_hash'][:12], bool(blk_ok)))
    else:
        rec('tool', 'wallet_history vitalik arbitrum', False, err)
    o, err, dt = c.tool('wallet_history', {'address': BIN, 'chain': 'ethereum'})
    rec('tool', 'wallet_history Binance ethereum', None if o and not o['data']['transactions'] else bool(o),
        ('%d txs; activity_by_chain=%d chains (%s)' % (len(o['data']['transactions']), len(o['data']['activity_by_chain'] or []), o['data']['transactions_note'])) if o else err)
    # ---- counterparties
    o, err, dt = c.tool('wallet_counterparties', {'address': VIT, 'chain': 'arbitrum', 'limit': 10})
    cps = ((o or {}).get('data') or {}).get('counterparties') or []
    rec('tool', 'wallet_counterparties vitalik arbitrum', bool(cps) and all(str(x.get('address', '')).startswith('0x') for x in cps),
        '%d counterparties, named=%d' % (len(cps), sum(1 for x in cps if x.get('name'))) if o else err)
    o, err, dt = c.tool('wallet_counterparties', {'address': BIN, 'chain': 'ethereum', 'limit': 10})
    cps = ((o or {}).get('data') or {}).get('counterparties') or []
    rec('tool', 'wallet_counterparties Binance ethereum', True if cps else None, '%d counterparties (%s)' % (len(cps), ((o or {}).get('data') or {}).get('note')) if o else err)
    # ---- tokens
    for ch, tok, sym, dec in TOKENS:
        o, err, dt = c.tool('token_info', {'chain': ch, 'token': tok})
        if o:
            d = o['data']
            ts = int(truth_rpc(ch, 'eth_call', [{'to': tok, 'data': '0x18160ddd'}, 'latest']), 16) / 10 ** dec
            ok = d['symbol'] == sym and d['decimals'] == dec == o['decimals'] and abs((d['total_supply'] or 0) - ts) / max(ts, 1) < 0.01 and o['chain_id'] == IDS[ch]
            if ch in ('arbitrum', 'robinhood'):
                t0 = (d['top_holders'] or [None])[0]
                if t0:
                    hb = int(truth_rpc(ch, 'eth_call', [{'to': tok, 'data': '0x70a08231' + '0' * 24 + t0['address'][2:]}, 'latest']), 16) / 10 ** dec
                    hok = abs(hb - t0['amount']) / max(hb, 1) < 0.05
                    rec('tool', 'token_info %s %s top holder vs chain' % (ch, sym), hok, '#1 %s (%s) em=%.6g chain_now=%.6g as_of=%s' % (t0['address'][:10], t0.get('name'), t0['amount'], hb, d.get('holders_as_of_utc')))
                else:
                    rec('tool', 'token_info %s %s top holders' % (ch, sym), None if 'holder_index_empty' in (d.get('holders_note') or '') else False, d.get('holders_note'))
            rec('tool', 'token_info %s %s' % (ch, sym), ok, 'symbol=%s decimals=%s supply=%.4g (truth %.4g) holders=%s top=%d%s' % (
                d['symbol'], d['decimals'], d['total_supply'] or 0, ts, d['holder_count'], len(d['top_holders']), (' note=' + d['holders_note'][:80]) if d.get('holders_note') else ''))
        else:
            rec('tool', 'token_info %s %s' % (ch, sym), False, err)
    # ---- chains
    o, err, dt = c.tool('list_chains', {})
    rows = {r['chain']: r for r in ((o or {}).get('data') or {}).get('chains') or []}
    rec('tool', 'list_chains', set(IDS) <= set(rows) and all(rows[k]['chain_id'] == v for k, v in IDS.items()), '%d chains: %s' % (len(rows), ','.join(rows)))
    table = []
    for ch in IDS:
        o, err, dt = c.tool('chain_stats', {'chain': ch})
        try:
            cid = int(truth_rpc(ch, 'eth_chainId', []), 16)
            tb = int(truth_rpc(ch, 'eth_blockNumber', []), 16)
        except Exception as e:
            cid, tb = None, None
        if not o:
            table.append((ch, 'FAIL', err, '', '', ''))
            rec('chain', ch, False, err)
            continue
        d = o['data']
        hd = d['head'] or {}
        rt = (d['data_layers'].get('realtime') or {})
        drift = (tb - hd['block']) if tb and hd.get('block') else None
        ok = o['chain_id'] == IDS[ch] == (cid or IDS[ch]) and hd.get('age_seconds') is not None and hd['age_seconds'] < 300 and drift is not None and abs(drift) < 2000
        layers = ', '.join('%s %s' % (k, fmt_lag(v['lag_seconds'])) for k, v in d['data_layers'].items())
        table.append((ch, IDS[ch], hd.get('block'), hd.get('age_seconds'), fmt_lag(rt.get('lag_seconds')) if rt else '—', layers or d.get('note')))
        rec('chain', ch, ok, 'chain_id=%s (rpc %s) head=%s age=%ss drift_vs_independent_rpc=%s | layers: %s' % (o['chain_id'], cid, hd.get('block'), hd.get('age_seconds'), drift, layers or d.get('note')))
    s, _, _ = http(base, None, {'authorization': 'Bearer ' + KEY, 'mcp-session-id': c.sid}, method='DELETE')
    rec('protocol', 'DELETE session -> 204', s == 204, 'HTTP %d' % s)
    return table


def session_survives(base):
    c = Client(base)
    s, hd, j = c.call('initialize', {'protocolVersion': '2025-11-25'})
    return {k.lower(): v for k, v in hd.items()}.get('mcp-session-id')


def fmt_lag(s):
    if s is None:
        return '—'
    s = int(s)
    return '%ds' % s if s < 120 else ('%.0fm' % (s / 60) if s < 7200 else ('%.1fh' % (s / 3600) if s < 172800 else '%.1fd' % (s / 86400)))


if __name__ == '__main__':
    if sys.argv[1:2] == ['--session-open']:
        print(session_survives(sys.argv[2]) or '');sys.exit()
    if sys.argv[1:2] == ['--session-check']:
        s, _, _ = http(sys.argv[2], {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, {'authorization': 'Bearer ' + KEY, 'mcp-session-id': sys.argv[3]})
        print('session after restart: HTTP %d' % s);sys.exit()
    bases = sys.argv[1:] or ['http://127.0.0.1:19471/api/mcp']
    for base in bases:
        RES.clear()
        print('#### %s' % base)
        table = run(base)
        for r in RES:
            print('%-8s %-4s %-45s %s' % (r[0], r[2], r[1], r[3]))
        print('-- chains: chain | chain_id | head block | head age s | realtime lag | layers')
        for t in table:
            print(' | '.join(str(x) for x in t))
        print('SUMMARY pass=%d warn=%d fail=%d' % tuple(sum(1 for r in RES if r[2] == k) for k in ('PASS', 'WARN', 'FAIL')))
