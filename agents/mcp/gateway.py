"""ticking MCP gateway (Streamable HTTP, MCP 2025-11-25/06-18/03-26) in front of the console's /api/mcp.

- Read-only Economic Machine tools backed by the existing loopback services (identity overlay :19447, EM bridge :8896,
  em-galaxy :8890, em-lookup tunnel :18790, ticking-chains :19448) plus public chain RPCs for heads / token metadata.
- list_markets / get_candles are forwarded unchanged to the console (:19428 /api/mcp): same key, entitlement and metering.
- Auth: the console's own api_keys table (read-only), scope mcp:read. EM tools serve data that is already public under
  /api/public/em/*, so they need a valid mcp:read key but no Data plan; they are rate limited per key.
- Fail-open: nginx falls back to the console's /api/mcp when this process is down.
Every EM tool result carries chain, chain_id, as_of, as_of_utc, source and decimals."""
import asyncio, hashlib, json, os, re, secrets, sqlite3, time
from datetime import datetime, timezone
import httpx
try:
    import em_v1_adapter as _v1a
except Exception:
    _v1a = None
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

DB = os.environ.get('TICKING_DB', '/srv/skew/tick-market-ui-20261007/.state/console.sqlite3')
CONSOLE = os.environ.get('CONSOLE', 'http://127.0.0.1:19428')
OVERLAY = os.environ.get('EM_OVERLAY', 'http://127.0.0.1:19447')
BRIDGE = os.environ.get('EM_BRIDGE', 'http://127.0.0.1:8896')
GALAXY = os.environ.get('EM_GALAXY', 'http://127.0.0.1:8890')
LOOKUP = os.environ.get('EM_LOOKUP', 'http://127.0.0.1:18790')
CHAINS_FEED = os.environ.get('CHAINS_FEED', 'http://127.0.0.1:19448')
VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
RPM = int(os.environ.get('MCP_RPM', '120'))
ALLOWED_ORIGINS = {'https://ticking.dev', 'https://www.ticking.dev', 'https://api.ticking.dev'}
RPC = {
    'ethereum': ['https://ethereum-rpc.publicnode.com'],
    'arbitrum': ['https://arbitrum-one-rpc.publicnode.com', 'https://arb1.arbitrum.io/rpc'],
    'base': ['https://base-rpc.publicnode.com', 'https://mainnet.base.org'],
    'optimism': ['https://optimism-rpc.publicnode.com', 'https://mainnet.optimism.io'],
    'linea': ['https://linea-rpc.publicnode.com', 'https://rpc.linea.build'],
    'polygon': ['https://polygon-bor-rpc.publicnode.com'],
    'unichain': ['https://unichain-rpc.publicnode.com', 'https://mainnet.unichain.org'],
    'robinhood': ['https://rpc.mainnet.chain.robinhood.com', 'https://robinhood-rpc.publicnode.com'],
    'arc': ['https://rpc.mainnet.arc.io', 'https://arc-rpc.publicnode.com'],
    'hyperevm': ['https://rpc.hyperliquid.xyz/evm', 'https://hyperliquid-rpc.publicnode.com'],
    'xlayer': ['https://rpc.xlayer.tech', 'https://xlayerrpc.okx.com'],
}
STATIC_IDS = {'ethereum': 1, 'optimism': 10, 'unichain': 130, 'polygon': 137, 'xlayer': 196, 'hyperevm': 999, 'robinhood': 4663,
              'arc': 5042, 'base': 8453, 'arbitrum': 42161, 'linea': 59144}
ALIASES = {'eth': 'ethereum', 'mainnet': 'ethereum', 'arb': 'arbitrum', 'arbitrum one': 'arbitrum', 'op': 'optimism', 'matic': 'polygon',
           'pol': 'polygon', 'hyperliquid': 'hyperevm', 'hyper': 'hyperevm', 'hype': 'hyperevm', 'x layer': 'xlayer', 'okx': 'xlayer',
           'robinhood chain': 'robinhood', 'uni': 'unichain'}
ADDR = re.compile(r'^0x[0-9a-fA-F]{40}$')
NATIVE = '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
USD_DECIMALS = 2
http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=3), headers={'user-agent': 'ticking-mcp-gateway/1'})
SESSION_DB = os.environ.get('MCP_SESSION_DB', '/srv/skew/ticking-mcp-gateway/state/sessions.sqlite3')
SESSION_IDLE = int(os.environ.get('MCP_SESSION_IDLE', str(7 * 86400)))
CURATED = os.environ.get('MCP_CURATED', '/srv/skew/ticking-mcp-gateway/curated_labels.json')


def sdb():
    os.makedirs(os.path.dirname(SESSION_DB), mode=0o700, exist_ok=True)
    c = sqlite3.connect(SESSION_DB, timeout=3)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, key_id TEXT NOT NULL, protocol TEXT, created INTEGER NOT NULL, seen INTEGER NOT NULL)')
    return c


def session_new(key_id, protocol):
    sid = secrets.token_urlsafe(24)
    with sdb() as c:
        c.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (sid, key_id, protocol, now(), now()))
        c.execute('DELETE FROM sessions WHERE seen<?', (now() - SESSION_IDLE,))
    return sid


def session_touch(sid, key_id):
    with sdb() as c:
        r = c.execute('SELECT key_id FROM sessions WHERE id=? AND seen>=?', (sid, now() - SESSION_IDLE)).fetchone()
        if not r or r[0] != key_id:
            return False
        c.execute('UPDATE sessions SET seen=? WHERE id=?', (now(), sid))
    return True


def session_drop(sid, key_id):
    with sdb() as c:
        return c.execute('DELETE FROM sessions WHERE id=? AND key_id=?', (sid, key_id)).rowcount == 1


def session_count():
    try:
        with sdb() as c:
            return c.execute('SELECT COUNT(*) FROM sessions WHERE seen>=?', (now() - SESSION_IDLE,)).fetchone()[0]
    except sqlite3.Error:
        return None


def curated():
    try:
        st = os.stat(CURATED)
    except OSError:
        return {}
    hit = CACHE.get('curated')
    if hit and hit[2] == st.st_mtime_ns:
        return hit[1]
    try:
        d = json.load(open(CURATED))
    except ValueError:
        d = {}
    CACHE['curated'] = (float('inf'), d, st.st_mtime_ns)
    return d


def license_reviewed(entry):
    if 'license_reviewed' in entry:
        return bool(entry['license_reviewed'])
    src = str(entry.get('source') or '').lower()
    return not ('not reviewed' in src or 'owner to confirm' in src or 'unverified' in src)
RATE = {}
CACHE = {}


class ToolError(Exception):
    pass


def now():
    return int(time.time())


def utc(ts):
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def norm_chain(c):
    if c is None or c == '':
        return None
    c = str(c).strip().lower()
    c = ALIASES.get(c, c)
    if c not in STATIC_IDS:
        raise ToolError('Unknown chain %r. Use list_chains for the supported ids.' % c)
    return c


def addr_arg(a):
    a = str(a or '').strip()
    if not ADDR.match(a):
        raise ToolError('address must be a 0x-prefixed 20-byte hex address.')
    return a.lower()


def env(chain, as_of, source, decimals, data, **extra):
    out = {'chain': chain, 'chain_id': STATIC_IDS.get(chain) if chain else None, 'as_of': as_of, 'as_of_utc': utc(as_of),
           'source': source, 'decimals': decimals}
    out.update(extra)
    out['data'] = data
    return out


async def cached(key, ttl, fn):
    hit = CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    val = await fn()
    CACHE[key] = (time.time() + ttl, val)
    if len(CACHE) > 5000:
        CACHE.clear()
    return val


async def get_json(base, path, params=None, timeout=30, ok404=False):
    try:
        r = await http.get(base + path, params=params, timeout=timeout)
    except httpx.HTTPError as e:
        raise ToolError('Upstream %s unavailable (%s).' % (path.split('?')[0], type(e).__name__))
    if r.status_code == 404 and ok404:
        return None
    if r.status_code != 200:
        try:
            msg = r.json().get('detail') or r.text[:200]
        except Exception:
            msg = r.text[:200]
        raise ToolError('Upstream %s returned %d: %s' % (path.split('?')[0], r.status_code, msg))
    return r.json()


async def rpc(chain, method, params):
    last = None
    for url in RPC.get(chain, []):
        try:
            r = await http.post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, timeout=8)
            j = r.json()
            if 'result' in j:
                return j['result'], url
            last = str(j.get('error'))[:200]
        except Exception as e:
            last = type(e).__name__
    raise ToolError('No RPC answered %s on %s (%s).' % (method, chain, last))


def _abi_str(h):
    try:
        b = bytes.fromhex(h[2:])
        if len(b) >= 96 and int.from_bytes(b[32:64], 'big') <= 256:
            n = int.from_bytes(b[32:64], 'big')
            return b[64:64 + n].decode('utf-8', 'replace')
        return b.rstrip(b'\x00').decode('utf-8', 'replace') or None
    except Exception:
        return None


async def erc20_meta(chain, token):
    async def load():
        if token == NATIVE:
            return {'symbol': 'native', 'decimals': 18, 'source_rpc': None}
        calls = {'name': '0x06fdde03', 'symbol': '0x95d89b41', 'decimals': '0x313ce567', 'totalSupply': '0x18160ddd'}
        res = await asyncio.gather(*[rpc(chain, 'eth_call', [{'to': token, 'data': d}, 'latest']) for d in calls.values()], return_exceptions=True)
        out = {}
        for (k, _), r in zip(calls.items(), res):
            if isinstance(r, Exception) or not r[0] or r[0] == '0x':
                out[k] = None
                continue
            out['source_rpc'] = r[1]
            out[k] = _abi_str(r[0]) if k in ('name', 'symbol') else int(r[0], 16)
        if out.get('decimals') is not None and out.get('totalSupply') is not None:
            out['totalSupplyRaw'] = str(out['totalSupply'])
            out['totalSupply'] = out['totalSupply'] / (10 ** out['decimals'])
        return out
    return await cached('meta:%s:%s' % (chain, token), 3600, load)


# ------------------------------------------------------------------ Economic Machine tools
async def wallet_card(address, chain=None, limit=8):
    p = {'address': address, 'positions_limit': str(limit)}
    if chain:
        p['chain'] = chain
    return await cached('card:%s:%s:%s' % (address, chain, limit), 60,
                        lambda: get_json(OVERLAY, '/api/public/em/tools/wallet', p, timeout=60))


async def identity(address):
    try:
        return await cached('idn:' + address, 300, lambda: get_json(LOOKUP, '/v1/identity/' + address, timeout=8))
    except ToolError:
        return None


async def t_wallet_profile(a):
    address = addr_arg(a.get('address'))
    chain = norm_chain(a.get('chain'))
    card, idn = await asyncio.gather(wallet_card(address, chain, 1), identity(address))
    ch = card.get('chain')
    try:
        xc = await cached('xc:' + address, 120, lambda: get_json(BRIDGE, '/api/public/em/address-chains', {'address': address}, timeout=20))
    except ToolError:
        xc = None
    idn = idn or {}
    data = {
        'address': address, 'name': card.get('entity') or (idn.get('identity') or {}).get('name'), 'identity': idn.get('identity'),
        'ens': idn.get('ens'), 'category': card.get('category'), 'category_name': card.get('categoryName'),
        'label_basis': card.get('labelBasis'), 'label_hidden': card.get('labelHidden'),
        'labels_by_chain': idn.get('labels'), 'in_map': card.get('inGalaxy'), 'node_id': card.get('nodeId'),
        'first_seen': card.get('first_seen'), 'first_seen_utc': utc(card.get('first_seen')),
        'first_seen_all_chains': card.get('first_seen_all_chains') or idn.get('firstSeen'),
        'first_seen_by_chain': card.get('firstSeenByChain') or idn.get('firstSeenByChain'),
        'last_seen': card.get('last_seen'), 'last_seen_utc': utc(card.get('last_seen')),
        'tx_in': card.get('tx_in'), 'tx_out': card.get('tx_out'), 'counterparties': card.get('degree'),
        'balance_sheet_usd': card.get('sheet'), 'total_usd_all_chains': card.get('totalUsdAllChains') or idn.get('totalUsd'),
        'headline': card.get('headline'), 'flags': card.get('flags'),
        'chains_seen': [{'chain': c.get('chain'), 'chain_id': c.get('chainId'), 'first_seen': c.get('firstSeen'),
                         'last_seen': c.get('lastSeen'), 'kind': c.get('kind'), 'tx_sent': c.get('txSent'), 'tx_received': c.get('txReceived')}
                        for c in ((xc or {}).get('chains') or [])],
    }
    src = 'em-identity-overlay (/api/public/em/tools/wallet) + em-lookup identity + address-chains'
    if _v1a and _v1a.enabled() and ch and _v1a._ok_chain(ch):
        try:
            v = await _v1a.identity(address, ch)
            if v and v.get('activity'):
                data['activity_v1'] = v['activity']
                data['_src_v1'] = 'em-serve/v1'
                src = src + ' + em-serve/v1'
                if v.get('firstSeen') is not None and not data.get('first_seen'):
                    data['first_seen'] = v['firstSeen']; data['first_seen_utc'] = utc(v['firstSeen'])
                fsb = dict(data.get('first_seen_by_chain') or {})
                if v.get('firstSeen') is not None: fsb[ch] = v['firstSeen']
                data['first_seen_by_chain'] = fsb
        except Exception:
            pass
    as_of = card.get('asOf') or card.get('last_seen') or now()
    return env(ch, as_of, src,
               {'usd': USD_DECIMALS, 'timestamps': 'unix seconds (UTC)'}, data,
               note='first_seen is scoped to `chain`; first_seen_all_chains is the earliest across chains.')


async def t_wallet_holdings(a):
    address = addr_arg(a.get('address'))
    chain = norm_chain(a.get('chain'))
    limit = int(a.get('limit') or 50)
    if not 1 <= limit <= 100:
        raise ToolError('limit must be 1..100.')
    card, idn = await asyncio.gather(wallet_card(address, chain, limit), identity(address))
    ch = card.get('chain')
    try:
        pnl = await cached('pnl:%s:%s' % (address, ch), 120, lambda: get_json(BRIDGE, '/api/public/em/wallet-pnl-xc', {'address': address, 'chain': ch} if ch else {'address': address}, timeout=25))
    except ToolError:
        pnl = None
    pos = card.get('positions') or []
    metas = await asyncio.gather(*[erc20_meta(ch, (p.get('token') or p.get('asset') or '').lower()) for p in pos[:25]
                                   if ADDR.match(str(p.get('token') or p.get('asset') or ''))], return_exceptions=True)
    meta_by = {}
    k = 0
    for p in pos[:25]:
        t = (p.get('token') or p.get('asset') or '').lower()
        if ADDR.match(t):
            m = metas[k]
            k += 1
            if not isinstance(m, Exception):
                meta_by[t] = m
    rows = []
    for p in pos:
        t = (p.get('token') or p.get('asset') or '').lower()
        m = meta_by.get(t) or {}
        rows.append({'symbol': p.get('symbol'), 'token': t or None, 'kind': p.get('kind'), 'protocol': p.get('protocol'),
                     'amount': p.get('amount'), 'amount_text': p.get('amountText'), 'usd': p.get('usd'),
                     'decimals': m.get('decimals'), 'held_back': bool(p.get('held')), 'held_reason': p.get('heldReason'),
                     'price_pending': p.get('pricePending'), 'flag': p.get('flag')})
    pnl_rows = [{'symbol': r.get('symbol'), 'token': r.get('token'), 'balance': r.get('balance'), 'value_usd': r.get('valueUsd'),
                 'price': r.get('price'), 'price_basis': r.get('priceBasis'), 'avg_cost_usd': r.get('avgCostUsd'),
                 'realized_usd': r.get('realizedUsd'), 'unrealized_usd': r.get('unrealizedUsd'), 'pnl_usd': r.get('pnlUsd'),
                 'trades': (r.get('buys') or 0) + (r.get('sells') or 0), 'verified': r.get('verified')}
                for r in ((pnl or {}).get('rows') or []) if not ch or r.get('chain') == ch]
    bal_all = [{'chain': b.get('chain'), 'chain_id': STATIC_IDS.get(b.get('chain')), 'net_worth_usd': b.get('net_worth_usd'),
                'liquid_usd': b.get('liquid_usd'), 'rows': b.get('rows'), 'unpriced_rows': b.get('unpriced_rows')}
               for b in ((idn or {}).get('balances') or [])]
    src_h = 'EM balance sheet (bridge tools/wallet) + wallet-pnl-xc + on-chain ERC-20 decimals'
    if (not rows) and _v1a and _v1a.enabled() and ch and _v1a._ok_chain(ch):
        try:
            vh = await _v1a.holdings(address, ch)
            if vh:
                for h in vh:
                    rows.append({'symbol': h.get('symbol'), 'token': h.get('token'), 'kind': h.get('kind'), 'protocol': None,
                                 'amount': None, 'amount_text': None, 'usd': h.get('usd'),
                                 'decimals': h.get('decimals'), 'held_back': False, 'held_reason': None,
                                 'price_pending': h.get('usd') is None, 'flag': None, 'balance_raw': h.get('balance_raw'), '_src': 'em-serve/v1'})
                src_h = src_h + ' + em-serve/v1'
        except Exception:
            pass
    data = {'address': address, 'balance_sheet_usd': card.get('sheet'), 'positions': rows,
            'positions_total': card.get('positionsTotal') or len(rows), 'positions_returned': len(rows),
            'truncated': card.get('positionsTruncated'), 'pnl': {'summary': next((c.get('summary') for c in ((pnl or {}).get('chains') or []) if c.get('chain') == ch), None), 'rows': pnl_rows},
            'balances_all_chains': bal_all}
    return env(ch, card.get('asOf') or (pnl or {}).get('asOf') or now(), src_h,
               {'usd': USD_DECIMALS, 'amount': 'human units (already divided by 10**decimals)', 'per_position': 'positions[].decimals'}, data,
               note='held_back positions are withheld by the econ reconciliation check (amount/usd null), not zero.')


async def t_wallet_history(a):
    address = addr_arg(a.get('address'))
    chain = norm_chain(a.get('chain'))
    limit = int(a.get('limit') or 20)
    if not 1 <= limit <= 100:
        raise ToolError('limit must be 1..100.')
    card = await wallet_card(address, chain, 1)
    ch = card.get('chain')
    txs, src = [], None
    if card.get('nodeId') is not None and card.get('inGalaxy'):
        act = await get_json(GALAXY, '/api/activity/%s/%d' % (ch, int(card['nodeId'])), {'limit': str(limit)}, timeout=30, ok404=True)
        if act:
            src = 'em-galaxy econ ledger (activity)'
            for t in (act.get('txs') or [])[:limit]:
                txs.append({'time': t.get('time'), 'time_utc': utc(t.get('time')), 'block': t.get('block'), 'tx_hash': t.get('tx_hash'),
                            'text': t.get('en'), 'usd': t.get('usd'),
                            'legs': [{'action': l.get('action'), 'protocol': l.get('protocol_name'), 'asset': l.get('asset'),
                                      'amount': l.get('amount'), 'usd': l.get('usd'), 'usd_basis': l.get('usd_basis')} for l in t.get('legs') or []]})
    summary = None
    w = None
    try:
        w = await cached('w:' + address, 300, lambda: get_json(LOOKUP, '/v1/wallet/' + address, timeout=20))
    except ToolError:
        pass
    if w:
        summary = [{'chain': c.get('chain'), 'chain_id': STATIC_IDS.get(c.get('chain')), **{k: (c.get('activity') or {}).get(k) for k in
                    ('first_time', 'last_time', 'tx_sent', 'tx_received', 'token_transfers_in', 'token_transfers_out', 'distinct_counterparties')}}
                   for c in (w.get('chains') or []) if c.get('activity')]
    data = {'address': address, 'transactions': txs, 'transactions_note': None if txs else
            'Per-transaction history is served for wallets in the map of arbitrum/robinhood; other chains return activity counts only.',
            'activity_by_chain': summary}
    as_of = max([t['time'] for t in txs if t.get('time')] or [card.get('last_seen') or now()])
    return env(ch, as_of, (src + ' + ' if src else '') + 'em-lookup activity', {'usd': USD_DECIMALS, 'amount': 'human units'}, data)


async def t_wallet_counterparties(a):
    address = addr_arg(a.get('address'))
    chain = norm_chain(a.get('chain'))
    limit = int(a.get('limit') or 20)
    if not 1 <= limit <= 50:
        raise ToolError('limit must be 1..50.')
    card = await wallet_card(address, chain, 1)
    ch = card.get('chain')
    rows, src = [], 'none'
    if card.get('nodeId') is not None and card.get('inGalaxy'):
        nb = await get_json(GALAXY, '/api/neighbors/%s/%d' % (ch, int(card['nodeId'])), {'limit': str(limit)}, timeout=30, ok404=True)
        src = 'em-galaxy address graph (neighbors)'
        for n in ((nb or {}).get('neighbors') or [])[:limit]:
            rows.append({'address': n.get('address'), 'name': n.get('entity_name'), 'category': n.get('category'), 'direction': n.get('dir'),
                         'transfers_in': n.get('in_n'), 'transfers_out': n.get('out_n'), 'usd_in': n.get('in_usd'), 'usd_out': n.get('out_usd'),
                         'assets': n.get('assets')})
    else:
        if _v1a and _v1a.enabled() and ch and _v1a._ok_chain(ch):
            try:
                vc = await _v1a.counterparties(address, ch)
                if vc:
                    src = 'em-serve/v1 counterparties'
                    for n in vc[:limit]:
                        rows.append(n)
            except Exception:
                pass
        if not rows:
            w = await cached('w:' + address, 300, lambda: get_json(LOOKUP, '/v1/wallet/' + address, timeout=20))
            src = 'em-lookup wallet counterparties'
            for n in (w.get('counterparties') or [])[:limit]:
                rows.append(n)
    names = await asyncio.gather(*[identity(r['address'].lower()) for r in rows[:15] if ADDR.match(str(r.get('address') or ''))])
    for r, idn in zip([r for r in rows[:15] if ADDR.match(str(r.get('address') or ''))], names):
        if idn and (idn.get('identity') or idn.get('ens')):
            r['name'] = r.get('name') or (idn.get('identity') or {}).get('name') or idn.get('ens')
            r['identity'] = idn.get('identity')
    data = {'address': address, 'counterparties': rows, 'note': None if rows else 'No counterparty graph for this wallet/chain yet.'}
    return env(ch, card.get('last_seen') or now(), src + ' + em-lookup identity names', {'usd': USD_DECIMALS}, data)


async def t_resolve_label(a):
    q = str(a.get('query') or a.get('address') or '').strip()
    if not q or len(q) > 120:
        raise ToolError('query (address or name) is required.')
    chain = norm_chain(a.get('chain'))
    if ADDR.match(q):
        idn = await identity(q.lower())
        if idn is None:
            raise ToolError('Identity service unavailable.')
        data = {'address': q.lower(), 'identity': idn.get('identity'), 'ens': idn.get('ens'),
                'labels': [l for l in (idn.get('labels') or []) if not chain or l.get('chain') == chain],
                'ai_label_floor': idn.get('aiFloor'),
                'note': 'labels[].shown=false means below the AI-label confidence floor; identity (curated/research/ENS) is authoritative.'}
        return env(chain, now(), 'em-lookup identity (curated + research + ENS + EM labels)', None, data)
    p = {'q': q, 'limit': '10'}
    if chain:
        p['chain'] = chain
    s, e = await asyncio.gather(get_json(BRIDGE, '/api/public/em/search', p, timeout=15),
                                get_json(BRIDGE, '/api/public/em/entities', {'q': q, 'limit': '10'}, timeout=15), return_exceptions=True)
    ql = q.lower()
    cur = []
    for addr, ent in curated().items():
        if ql in str(ent.get('name') or '').lower() or ql == str(ent.get('type') or '').lower():
            ok = license_reviewed(ent)
            cur.append({'address': addr, 'name': ent.get('name'), 'type': ent.get('type'), 'basis': 'curated', 'source': ent.get('source'),
                        'license_reviewed': ok, 'grade': 'official' if ok else 'unreviewed',
                        'note': None if ok else 'Source licence not yet reviewed by the owner; treat as provisional and do not republish.'})
    if a.get('exclude_unreviewed'):
        cur = [x for x in cur if x['license_reviewed']]
    data = {'query': q, 'curated': cur, 'results': [] if isinstance(s, Exception) else s.get('results') or [],
            'entities': [] if isinstance(e, Exception) else e.get('rows') or []}
    if not data['results'] and not data['entities'] and not data['curated']:
        data['note'] = 'No named match. Name search covers the research-label institutions; curated exchange tags (e.g. Binance hot wallets) resolve by address.'
    return env(chain, now(), 'curated label index + EM search + entities', None, data)


async def public_chains():
    return await cached('pubchains', 300, lambda: get_json(CHAINS_FEED, '/api/public/chains', timeout=10))


async def t_list_chains(a):
    em, pub = await asyncio.gather(cached('emchains', 120, lambda: get_json(BRIDGE, '/api/public/em/chains', timeout=15)), public_chains(), return_exceptions=True)
    stats = {} if isinstance(pub, Exception) else ((pub.get('em') or {}).get('chains') or {})
    rows = []
    for c in ([] if isinstance(em, Exception) else em.get('chains') or []):
        s = stats.get(c['id']) or {}
        rows.append({'chain': c['id'], 'chain_id': c.get('chainId'), 'name': c.get('name'), 'status': c.get('status'),
                     'has_data': c.get('hasData'), 'realtime_age_s': c.get('realtimeAgeS'), 'capabilities': c.get('capabilities'),
                     'native': c.get('native'), 'explorer': c.get('explorer'),
                     'addresses_indexed': s.get('addressesIndexed'), 'labeled_addresses': s.get('labeledAddresses'),
                     'transactions_indexed': s.get('transactionsIndexed'), 'map_snapshot_at': s.get('mapSnapshotAt'),
                     'map_snapshot_utc': utc(s.get('mapSnapshotAt'))})
    return env(None, now(), 'EM bridge chain registry + ticking-chains feed', None, {'chains': rows,
               'note': 'chain_stats(chain) adds the live head, follower lag and data freshness per chain.'})


async def head(chain):
    async def load():
        blk, url = await rpc(chain, 'eth_getBlockByNumber', ['latest', False])
        return {'block': int(blk['number'], 16), 'time': int(blk['timestamp'], 16), 'rpc': url}
    return await cached('head:' + chain, 5, load)


async def t_chain_stats(a):
    chain = norm_chain(a.get('chain'))
    if not chain:
        raise ToolError('chain is required.')
    h, fr, pub, em = await asyncio.gather(head(chain), get_json(BRIDGE, '/api/public/em/freshness', {'chains': chain}, timeout=15),
                                          public_chains(), cached('emchains', 120, lambda: get_json(BRIDGE, '/api/public/em/chains', timeout=15)),
                                          return_exceptions=True)
    f = {} if isinstance(fr, Exception) else ((fr.get('chains') or {}).get(chain) or {})
    reg = {} if isinstance(em, Exception) else next((c for c in em.get('chains') or [] if c.get('id') == chain), {})
    hd = None if isinstance(h, Exception) else h
    layers = {}
    for k, v in f.items():
        if isinstance(v, dict) and v.get('block') is not None:
            lag = (hd['time'] - v['time']) if hd and v.get('time') else None
            layers[k] = {'block': v.get('block'), 'time': v.get('time'), 'time_utc': utc(v.get('time')), 'source': v.get('source'),
                         'lag_seconds': lag, 'lag_blocks': (hd['block'] - v['block']) if hd and isinstance(v.get('block'), int) else None}
            if lag is not None and lag > 7 * 86400:
                layers[k]['stalled'] = True
            if v.get('time') is None:
                layers[k]['time_note'] = 'block timestamp unavailable'
    s = {} if isinstance(pub, Exception) else (((pub.get('em') or {}).get('chains') or {}).get(chain) or {})
    data = {'head': {'block': hd['block'], 'time': hd['time'], 'time_utc': utc(hd['time']), 'age_seconds': now() - hd['time'], 'rpc': hd['rpc']} if hd else None,
            'head_error': str(h) if isinstance(h, Exception) else None,
            'status': reg.get('status'), 'has_data': reg.get('hasData'), 'realtime_age_s': reg.get('realtimeAgeS'),
            'data_layers': layers, 'live_follower': (layers.get('realtime') or {}).get('lag_seconds'),
            'indexed': {k: s.get(k) for k in ('addressesIndexed', 'labeledAddresses', 'transactionsIndexed', 'tokenTransfersIndexed', 'connections', 'mapSnapshotAt') if s.get(k) is not None},
            'note': None if layers else 'No EM data layers reported for this chain yet (registry status: %s).' % reg.get('status')}
    return env(chain, hd['time'] if hd else now(), 'public RPC head + EM freshness + ticking-chains feed', None, data)


async def t_token_info(a):
    chain = norm_chain(a.get('chain'))
    tok = str(a.get('token') or '').strip()
    sym = str(a.get('symbol') or '').strip()
    if not chain:
        raise ToolError('chain is required.')
    if not tok and sym:
        s = await get_json(BRIDGE, '/api/public/em/search', {'q': sym, 'limit': '10', 'chain': chain}, timeout=15)
        cand = [r for r in (s.get('results') or []) if r.get('kind') in ('token', 'market') and ADDR.match(str(r.get('address') or r.get('token') or ''))]
        if not cand:
            raise ToolError('No token %r on %s in the EM index; pass its contract address.' % (sym, chain))
        tok = cand[0].get('address') or cand[0].get('token')
    tok = addr_arg(tok)
    meta = await erc20_meta(chain, tok)
    if meta.get('decimals') is None and meta.get('symbol') is None:
        raise ToolError('%s is not an ERC-20 contract on %s.' % (tok, chain))
    holders, top, hnote, h_asof = None, [], None, None
    if chain in ('arbitrum', 'robinhood'):
        # tools/holders only knows reviewed catalog markets and lists holders for Robinhood stock tokens; the general
        # holder index is holders-at (snapshot + exact deltas), which must be asked at its own head block.
        try:
            probe = await cached('hhead:' + chain, 120, lambda: get_json(BRIDGE, '/api/public/em/holders-at', {'market': '%s:%s' % (chain, tok), 't': str(now())}, timeout=60))
            hb = probe.get('head')
            if not hb:
                raise ToolError(probe.get('reason') or 'holder index head unknown')
            ha = await cached('hat:%s:%s:%s' % (chain, tok, hb), 600, lambda: get_json(BRIDGE, '/api/public/em/holders-at', {'market': '%s:%s' % (chain, tok), 'block': str(hb), 'limit': '15'}, timeout=120))
            snap = (ha.get('snaps') or [{}])[0]
            if snap.get('available') and not snap.get('holders') and not snap.get('top'):
                hnote = 'holder_index_empty: the EM holder snapshot has no balances for this token at block %s (data gap, not zero holders).' % snap.get('block')
            elif snap.get('available'):
                holders = snap.get('holders')
                h_asof = snap.get('time')
                sup = meta.get('totalSupply')
                for h in (snap.get('top') or [])[:15]:
                    top.append({'address': h.get('address'), 'name': h.get('entity'), 'category': h.get('category'), 'class': h.get('class'),
                                'amount': h.get('amount'), 'share': h.get('pct') if h.get('pct') is not None else (round(h['amount'] / sup, 6) if sup and h.get('amount') is not None else None)})
                hnote = None if snap.get('pct') is not None else 'share = amount / current totalSupply (supply at the snapshot block is not readable)'
            else:
                hnote = '%s: %s' % (snap.get('code'), snap.get('reason'))
        except ToolError as e:
            hnote = str(e)
    else:
        hnote = 'holder_history_not_served: the EM holder index covers arbitrum and robinhood today.'
    data = {'token': tok, 'name': meta.get('name'), 'symbol': meta.get('symbol'), 'decimals': meta.get('decimals'),
            'total_supply': meta.get('totalSupply'), 'total_supply_raw': meta.get('totalSupplyRaw'),
            'holder_count': holders, 'holders_as_of': h_asof, 'holders_as_of_utc': utc(h_asof), 'top_holders': top, 'holders_note': hnote}
    return env(chain, h_asof or now(), 'on-chain ERC-20 calls (%s) + EM holders-at' % meta.get('source_rpc'), meta.get('decimals'), data)


EM_TOOLS = {
    'wallet_profile': (t_wallet_profile, 'Identity, labels, first/last seen, activity counts and balance-sheet totals for an EVM wallet (Economic Machine). first_seen is per chain.',
                       {'address': {'type': 'string', 'description': '0x address'}, 'chain': {'type': 'string', 'description': 'optional chain id (ethereum, arbitrum, base, ...)'}}, ['address']),
    'wallet_holdings': (t_wallet_holdings, 'Token positions (amount, USD, decimals), balance sheet and cost-basis PnL for an EVM wallet on one chain.',
                        {'address': {'type': 'string'}, 'chain': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, ['address']),
    'wallet_history': (t_wallet_history, 'Recent economic actions (swaps, bridges, lending...) with USD values, plus per-chain activity counts.',
                       {'address': {'type': 'string'}, 'chain': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, ['address']),
    'wallet_counterparties': (t_wallet_counterparties, 'Top counterparties of a wallet from the address graph, with names where known.',
                              {'address': {'type': 'string'}, 'chain': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}, ['address']),
    'resolve_label': (t_resolve_label, 'Resolve an address to its identity/labels (exchange, fund, person, ENS), or search a name.',
                      {'query': {'type': 'string', 'description': 'address or name'}, 'chain': {'type': 'string'},
                       'exclude_unreviewed': {'type': 'boolean', 'description': 'drop curated names whose source licence is not reviewed'}}, ['query']),
    'list_chains': (t_list_chains, 'Chains the Economic Machine covers, with chain ids, status and indexed counts.', {}, []),
    'chain_stats': (t_chain_stats, 'Live chain head (block/time) and the lag of each Economic Machine data layer behind it.',
                    {'chain': {'type': 'string'}}, ['chain']),
    'token_info': (t_token_info, 'ERC-20 metadata read on-chain (name, symbol, decimals, supply) plus top holders from the Economic Machine.',
                   {'chain': {'type': 'string'}, 'token': {'type': 'string', 'description': '0x contract'}, 'symbol': {'type': 'string'}}, ['chain']),
}


def tool_list(console_tools):
    out = list(console_tools)
    for name, (_, desc, props, req) in EM_TOOLS.items():
        out.append({'name': name, 'description': desc,
                    'inputSchema': {'type': 'object', 'properties': props, 'required': req, 'additionalProperties': False},
                    'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}})
    return out


CONSOLE_TOOLS_FALLBACK = [
    {'name': 'list_markets', 'description': 'List verified ticking markets and their recorded-history readiness.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}},
    {'name': 'get_candles', 'description': 'Read retained executed OHLCV candles. Use a Solana mint or catalog symbol; coverage is partial.',
     'inputSchema': {'type': 'object', 'properties': {'market': {'type': 'string'}, 'interval': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500}},
                     'required': ['market'], 'additionalProperties': False}, 'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}}]


# ------------------------------------------------------------------ auth / transport
def key_row(request):
    h = request.headers.get('authorization', '')
    if not h.startswith('Bearer ') or len(h) > 200:
        return None, (401, 'A ticking API key is required.')
    try:
        c = sqlite3.connect('file:%s?mode=ro' % DB, uri=True, timeout=2)
        c.row_factory = sqlite3.Row
        row = c.execute('SELECT id,user_id,scopes FROM api_keys WHERE hash=? AND revoked IS NULL', (hashlib.sha256(h[7:].encode()).hexdigest(),)).fetchone()
        c.close()
    except sqlite3.Error:
        return None, (503, 'Key store unavailable.')
    if not row:
        return None, (401, 'This API key is invalid or revoked.')
    if 'mcp:read' not in json.loads(row['scopes']):
        return None, (403, 'This API key does not have the required scope.')
    return dict(row), None


def rate_ok(key_id):
    t = time.time()
    q = [x for x in RATE.get(key_id, []) if x > t - 60]
    if len(q) >= RPM:
        RATE[key_id] = q
        return False
    q.append(t)
    RATE[key_id] = q
    return True


def reply(request, payload, status=200, headers=None):
    headers = dict(headers or {})
    acc = request.headers.get('accept', '')
    if 'text/event-stream' in acc and 'application/json' not in acc and payload is not None:
        body = 'event: message\ndata: %s\n\n' % json.dumps(payload, separators=(',', ':'))
        return Response(body, status, headers={**headers, 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'}, media_type='text/event-stream')
    return JSONResponse(payload, status, headers={**headers, 'Cache-Control': 'no-store'})


async def console_call(request, body):
    hdr = {k: v for k, v in request.headers.items() if k.lower() in ('authorization', 'user-agent', 'x-real-ip', 'origin')}
    hdr.update({'content-type': 'application/json', 'host': 'api.ticking.dev', 'x-forwarded-proto': 'https'})
    r = await http.post(CONSOLE + '/api/mcp', content=json.dumps(body), headers=hdr, timeout=40)
    return r


async def mcp_post(request: Request):
    origin = request.headers.get('origin')
    if origin and origin not in ALLOWED_ORIGINS and not origin.endswith('.ticking.dev'):
        return JSONResponse({'detail': 'Origin not allowed.'}, 403)
    row, err = key_row(request)
    if err:
        return JSONResponse({'detail': err[1]}, err[0], headers={'WWW-Authenticate': 'Bearer realm="ticking"'} if err[0] == 401 else None)
    if not rate_ok(row['id']):
        return JSONResponse({'detail': 'Rate limit reached.'}, 429, headers={'Retry-After': '30'})
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}, 400)
    if not isinstance(body, dict) or body.get('jsonrpc') != '2.0':
        return JSONResponse({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'A single JSON-RPC 2.0 message is required.'}}, 400)
    method, mid, params = body.get('method'), body.get('id'), body.get('params') or {}
    sid = request.headers.get('mcp-session-id')
    if method != 'initialize' and sid is not None:
        try:
            alive = session_touch(sid, row['id'])
        except sqlite3.Error:
            alive = True    # fail open: the session store must not take the endpoint down
        if not alive:
            return JSONResponse({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32001, 'message': 'Session not found; re-initialize.'}}, 404)
    if 'method' not in body:   # a client response / stray message
        return Response(status_code=202)
    if mid is None:            # notification
        return Response(status_code=202)
    if not isinstance(params, dict):
        return reply(request, {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'Invalid params'}})
    if method == 'initialize':
        v = params.get('protocolVersion')
        try:
            new = session_new(row['id'], v if v in VERSIONS else VERSIONS[0])
        except sqlite3.Error:
            new = None
        res = {'protocolVersion': v if v in VERSIONS else VERSIONS[0], 'capabilities': {'tools': {'listChanged': False}},
               'serverInfo': {'name': 'ticking', 'title': 'ticking + Economic Machine', 'version': '0.2.0'},
               'instructions': 'Read-only. list_markets/get_candles: retained Solana execution history (Data plan). '
                               'wallet_*/resolve_label/token_info/list_chains/chain_stats: Economic Machine multichain wallet, label and chain data. '
                               'Every EM result carries chain, chain_id, as_of (unix s) and source. No trading or wallet access.'}
        return reply(request, {'jsonrpc': '2.0', 'id': mid, 'result': res}, headers={'Mcp-Session-Id': new} if new else None)
    if method == 'ping':
        return reply(request, {'jsonrpc': '2.0', 'id': mid, 'result': {}})
    if method == 'tools/list':
        tools = CONSOLE_TOOLS_FALLBACK
        return reply(request, {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': tool_list(tools)}})
    if method == 'tools/call':
        name = params.get('name')
        args = params.get('arguments') or {}
        if name in EM_TOOLS:
            if not isinstance(args, dict):
                return reply(request, {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': 'Invalid tool arguments.'}], 'isError': True}})
            allowed = set(EM_TOOLS[name][2])
            extra = set(args) - allowed
            try:
                if extra:
                    raise ToolError('Unexpected argument(s): %s' % ', '.join(sorted(extra)))
                out = await asyncio.wait_for(EM_TOOLS[name][0](args), 75)
                res = {'content': [{'type': 'text', 'text': json.dumps(out, separators=(',', ':'), default=str)}], 'structuredContent': out, 'isError': False}
            except ToolError as e:
                res = {'content': [{'type': 'text', 'text': str(e)}], 'isError': True}
            except asyncio.TimeoutError:
                res = {'content': [{'type': 'text', 'text': 'The Economic Machine took too long to answer; retry.'}], 'isError': True}
            except (ValueError, TypeError) as e:
                res = {'content': [{'type': 'text', 'text': 'Invalid tool arguments (%s).' % type(e).__name__}], 'isError': True}
            return reply(request, {'jsonrpc': '2.0', 'id': mid, 'result': res})
        try:
            r = await console_call(request, body)
        except httpx.HTTPError:
            return reply(request, {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': 'Market data service unavailable.'}], 'isError': True}})
        if r.status_code != 200:
            try:
                d = r.json().get('detail')
            except Exception:
                d = None
            return reply(request, {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': d or ('Upstream %d' % r.status_code)}], 'isError': True}})
        return reply(request, r.json())
    return reply(request, {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'Method not found'}})


async def mcp_get(request):
    return Response(status_code=405, headers={'Allow': 'POST, DELETE'})


async def mcp_delete(request):
    sid = request.headers.get('mcp-session-id')
    row, err = key_row(request)
    if err:
        return JSONResponse({'detail': err[1]}, err[0])
    if not sid or not session_drop(sid, row['id']):
        return Response(status_code=404)
    return Response(status_code=204)


async def health(request):
    return JSONResponse({'ok': True, 'layer': 'ticking-mcp-gateway', 'sessions': session_count(), 'tools': 2 + len(EM_TOOLS)})


routes = []
for p in ('/api/mcp', '/mcp'):
    routes += [Route(p, mcp_post, methods=['POST']), Route(p, mcp_get, methods=['GET']), Route(p, mcp_delete, methods=['DELETE'])]
routes.append(Route('/healthz', health))
app = Starlette(routes=routes)
