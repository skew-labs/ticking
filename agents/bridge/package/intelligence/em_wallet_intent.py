"""Economic Machine wallet intents for the staging Ticking resolver.

Deterministic routing (no model call): one 0x wallet -> wallet card (or its money trace), "who borrowed USDG against NVDA"
-> ranked Morpho Blue positions, "NVDA holders" -> holders card. Data comes from the em-ticking-bridge
wallet tools (/api/public/em/tools/*), which read em-galaxy. Markets are first-class '<network>:<0x address>' rows. The sentence is built from those numbers only.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

TOOLS = os.environ.get('EM_TOOLS_BASE', 'http://127.0.0.1:8896/api/public/em/tools/')   # em-ticking-bridge (loopback)
CONSOLE = os.environ.get('EM_CONSOLE_BASE', 'http://127.0.0.1:19428')                     # Ticking console API (candles)
ADDR = re.compile(r'(?<![A-Za-z0-9])0x[a-fA-F0-9]{40}(?![A-Za-z0-9])')
MARKET = re.compile(r'(arbitrum|robinhood):0x[0-9a-f]{40}')
STABLE = {'USDG', 'USDC', 'USDT', 'USDT0', 'DAI', 'USDE', 'USD', 'USDCE', 'FRAX'}
STOP = {'WHO', 'THE', 'TOP', 'AND', 'ON', 'FOR', 'ME', 'SHOW', 'LIST', 'WALLET', 'WALLETS', 'HOLDERS', 'HOLDER', 'AGAINST', 'WITH',
        'BORROWED', 'BORROW', 'LARGEST', 'BIGGEST', 'MOST', 'OF', 'IN', 'THIS', 'THAT', 'WHAT', 'WHICH', 'IS', 'ARE', 'HOLD', 'HOLDS',
        'DEBT', 'LOANS', 'COLLATERAL', 'STOCK', 'TOKENS', 'TOKEN', 'MONEY', 'TRACE', 'WHERE', 'DID', 'GO', 'FROM', 'TO', 'IT', 'ITS',
        'MAP', 'WHALES', 'WHALE', 'OWNS', 'OWNERS', 'ROBINHOOD', 'ARBITRUM', 'CHAIN', 'ONE', 'MORPHO', 'AAVE', 'AI', 'USING', 'POSTED'}
CHAIN = {'robinhood': 'Robinhood Chain', 'arbitrum': 'Arbitrum One'}
FLAG = {'unverified_principal': 'a principal not confirmed by a receipt token', 'morpho_interest_runaway': 'a Morpho market with runaway bad debt',
        'vault_pass_through': 'vault assets that belong to depositors', 'debt_exceeds_assets': 'debt above its priced assets',
        'collateral_unpriced': 'collateral without a trusted price', 'lp_unpriced': 'LP without a trusted price',
        'supplied_unpriced': 'a supplied asset without a trusted price', 'lp_owner_inferred': 'LP ownership inferred from the sender',
        'unverified': 'balances far above its observed inflow'}


def short(a):
    return a[:6] + '…' + a[-4:] if a else ''


def usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return '—'
    a, s = abs(v), '-' if v < 0 else ''
    if a >= 1e9:
        return '%s$%.2fB' % (s, a / 1e9)
    if a >= 1e6:
        return '%s$%.2fM' % (s, a / 1e6)
    if a >= 1e3:
        return '%s$%.1fK' % (s, a / 1e3)
    return '%s$%.0f' % (s, a)


def get(tool, **q):
    url = TOOLS + tool + '?' + urllib.parse.urlencode({k: v for k, v in q.items() if v})
    try:
        with urllib.request.urlopen(url, timeout=45) as r:
            return json.loads(r.read(400_000))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read(20_000)).get('detail')
        except ValueError:
            detail = None
        raise LookupError(detail or 'Economic Machine data is unavailable for this request.')
    except OSError:
        raise LookupError('Economic Machine data is unavailable right now.')


def symbols(q):
    return [w.replace('.', '') for w in re.findall(r'(?<![A-Za-z0-9])[A-Z][A-Z0-9.]{1,9}(?![A-Za-z0-9])', q) if w not in STOP]


def wallet_message(c):
    s = c.get('sheet') or {}
    who = c.get('entity') or c.get('categoryName') or 'Wallet'
    head = '%s %s on %s' % (who, short(c['address']), CHAIN.get(c.get('chain'), c.get('chain')))
    if not s.get('net_worth') and not s.get('collateral') and not s.get('debt'):
        return head + ': no open lending, LP or perp positions were decoded, so its money is held, not deployed.'
    pos = c.get('positions') or []
    coll = [p for p in pos if p.get('kind') == 'collateral' and p.get('usd')]
    debt = [p for p in pos if p.get('kind') == 'debt' and p.get('usd')]
    parts = ['net worth %s' % usd(s.get('net_worth'))]
    if s.get('collateral'):
        names = ', '.join(dict.fromkeys(p['asset'] for p in coll[:4] if p.get('asset')))
        parts.append('%s posted as collateral%s' % (usd(s['collateral']), ' (%s)' % names if names else ''))
    if s.get('debt'):
        names = ', '.join(dict.fromkeys(p['asset'] for p in debt[:3] if p.get('asset')))
        where = ', '.join(dict.fromkeys(p['protocol'] for p in debt[:3] if p.get('protocol')))
        parts.append('%s borrowed%s%s' % (usd(abs(s['debt'])), ' in %s' % names if names else '', ' on %s' % where if where else ''))
    if s.get('lp'):
        parts.append('%s in liquidity positions' % usd(s['lp']))
    msg = head + ': ' + '; '.join(parts) + '.'
    if s.get('collateral') or s.get('lp'):
        msg += ' That money was not sold — it sits in positions the wallet still owns.'
    if c.get('flags'):
        msg += ' Unverified: ' + ', '.join(FLAG.get(f, f) for f in c['flags']) + '.'
    return msg


def top_message(c, sym):
    rows = c.get('rows') or []
    if not rows:
        return 'No wallet in the Economic Machine balance sheets borrowed USDG against %s.' % (sym or 'Robinhood stock tokens')
    r = rows[0]
    lead = ', '.join('%s %s' % (k, usd(v)) for k, v in list((r.get('collateral') or {}).items())[:3])
    return ('%d wallet%s borrowed USDG against %s on Morpho Blue. The largest, %s, posted %s as collateral and owes %s USDG '
            '(net worth %s). Select a wallet to open its dossier and see its actions on the chart.'
            % (len(rows), '' if len(rows) == 1 else 's', sym or 'Robinhood stock tokens', short(r['address']), lead, usd(r.get('usdgDebt')), usd(r.get('netWorth'))))


def holders_message(c):
    hs = c.get('holders') or []
    if not hs:
        return 'No holder data for %s.' % c.get('symbol')
    h = hs[0]
    who = (h.get('entity') or '').replace('_', ' ') or h.get('categoryName') or 'wallet'
    return '%s has %s holders. The largest is %s %s with %.2f%% of supply%s.' % (
        c.get('symbol'), '{:,}'.format(c['holderCount']) if c.get('holderCount') else 'many', who, short(h['address']), h.get('share') or 0,
        ' — liquidity, not a single investor' if h.get('category') in ('dex_pool', 'lending') else '')


def trace_message(c):
    cps = c.get('counterparties') or []
    if not cps:
        return 'No mapped counterparties for %s in the drawn galaxy; its full-map card is below.' % short(c['address'])
    top = cps[0]
    who = (top.get('entity') or '').replace('_', ' ') or top.get('categoryName') or 'a wallet'
    flow = 'received from' if top.get('direction') == 'in' else 'sent to' if top.get('direction') == 'out' else 'exchanged with'
    return '%s mostly moved money with %s %s: %s %s it%s across %s transfers. %d counterparties are listed below.' % (
        short(c['address']), who, short(top.get('address')), usd(top.get('usd')), flow,
        ' (%s)' % ', '.join(top['assets']) if top.get('assets') else '', '{:,}'.format(top.get('n') or 0), len(cps))


def pct(v):
    return '—' if not isinstance(v, (int, float)) else ('%.1f%%' % (v * 100) if abs(v) < 0.1 else '%.0f%%' % (v * 100))


def price_facts(market):
    """24h move of the market's USD token reference, straight from the console's candle API."""
    try:
        url = CONSOLE + '/api/public/engine/candles?' + urllib.parse.urlencode({'market': market, 'aggregation': 'token', 'interval': '1h', 'limit': 25})
        with urllib.request.urlopen(url, timeout=8) as r:
            bars = [b for b in json.loads(r.read(2_000_000)).get('candles', []) if not b.get('synthetic')]
    except (OSError, ValueError):
        return None
    if len(bars) < 2:
        return None
    first, last = bars[0], bars[-1]
    return {'change': last['close'] / first['open'] - 1, 'high': max(b['high'] for b in bars), 'low': min(b['low'] for b in bars),
            'close': last['close'], 'volume': sum(b.get('volume') or 0 for b in bars)}


def economy_card(kind, sym, chart):
    market = (chart or {}).get('asset')
    q = {'market': market} if isinstance(market, str) and MARKET.fullmatch(market) and (chart or {}).get('symbol', '').upper().startswith(sym) else {'symbol': sym}
    e = get('economy', window='24h', **q)
    market, symbol, chain = e['market'], e['symbol'], e['network']
    lev, flows, state = e.get('leverage') or {}, e.get('flows') or {}, e.get('state') or {}
    facts, asof, overlay, title, card_extra = [], None, 'events', '', {}
    if kind == 'liquidations' or kind == 'leverage':
        d = lev.get('data') or {}
        asof = lev.get('asOf')
        if d.get('positions') or d.get('ladder'):
            near = (d.get('positions') or [])[:3]
            facts += [{'label': 'Positions borrowing against %s' % symbol, 'value': '{:,}'.format(int((d.get('totals') or {}).get('positions') or len(d.get('positions') or [])))},
                      {'label': 'Debt at risk', 'value': usd((d.get('totals') or {}).get('debtUsd'))}]
            way = lambda v: '%.1f%% %s' % (abs(v or 0), 'below' if (v or 0) < 0 else 'above')
            facts += [{'label': 'Nearest: %s on %s' % (short(p.get('wallet')), str(p.get('protocol') or '?').replace('_', ' ').title()),
                       'value': '%s USD (%s)' % ('{:,.4g}'.format(p['liqPrice']), way(p.get('distancePct')))} for p in near if isinstance(p.get('liqPrice'), (int, float))]
            overlay = 'liquidations'
            message = '%s: %d positions owe %s against it; the nearest liquidation level is %s the current price.' % (
                symbol, int((d.get('totals') or {}).get('positions') or len(near)), usd((d.get('totals') or {}).get('debtUsd')), way(near[0].get('distancePct')) if near else 'not near')
        elif d.get('borrowers'):
            b = d['borrowers']
            facts += [{'label': 'Wallets borrowing against %s' % symbol, 'value': str(len(b))},
                      {'label': 'USDG borrowed', 'value': usd(sum(x.get('debtUsd') or 0 for x in b))}]
            facts += [{'label': short(x['wallet']), 'value': '%s collateral · %s debt' % (usd(x.get('collateralUsd')), usd(x.get('debtUsd')))} for x in b[:3]]
            message = '%d wallet%s borrow %s USDG on Morpho Blue against %s. Liquidation prices need live analytics; the borrowers are below.' % (
                len(b), '' if len(b) == 1 else 's', usd(sum(x.get('debtUsd') or 0 for x in b)), symbol)
        else:
            message = 'No lending positions use %s as collateral in the current Economic Machine data.' % symbol
        title = ('Liquidation levels' if kind == 'liquidations' else 'Who is leveraged') + ' · ' + symbol
    elif kind == 'flows':
        d = flows.get('data') or {}
        asof = flows.get('asOf')
        if d.get('classes'):
            names = {'whale': 'Whales', 'retail': 'Retail wallets', 'mev': 'MEV bots', 'exchange': 'Exchanges', 'contract': 'Contracts & vaults'}
            facts = [{'label': c.get('label') or names.get(c.get('class'), c.get('class')), 'value': ('+' if (c.get('netUsd') or 0) >= 0 else '-') + usd(abs(c.get('netUsd') or 0)) + ' net'} for c in d['classes'][:6]]
            message = 'Net flows into %s over 24h by wallet class.' % symbol
        elif d.get('actions'):
            facts = [{'label': str(a.get('action', '')).replace('_', ' ').capitalize(), 'value': usd(a.get('usd'))} for a in d['actions'][:6]]
            message = 'Where %s money moved across the mapped history (a 24h class split arrives with live analytics).' % symbol
        else:
            message = 'Flows for %s are not available yet.' % symbol
        title = 'Money flow · ' + symbol
    else:   # why did X move: tie the price change to the money, numbers only
        import time as _t
        px = price_facts(market)
        now = int(_t.time())
        ev = get('events', market=market, **{'from': str(now - 86400), 'to': str(now)})
        asof = ev.get('asOf') or flows.get('asOf')
        names = {'whale': 'whales', 'retail': 'retail wallets', 'mev': 'MEV bots', 'exchange': 'exchanges', 'contract': 'contracts and vaults'}
        fd = flows.get('data') or {}
        classes = fd.get('classes') or []
        clauses = []
        if classes:
            top = max(classes, key=lambda c: abs(c.get('netUsd') or 0))
            n = top.get('netUsd') or 0
            clauses.append('%s were net %s (%s%s)' % (names.get(top.get('class'), top.get('class')), 'buyers' if n >= 0 else 'sellers', '+' if n >= 0 else '−', usd(abs(n))))
            mev = next((c for c in classes if c.get('class') == 'mev'), None)
            if mev and top.get('class') != 'mev' and (mev.get('buyUsd') or 0) + (mev.get('sellUsd') or 0) > 2 * abs(n):
                clauses.append('MEV bots churned %s' % usd((mev.get('buyUsd') or 0) + (mev.get('sellUsd') or 0)))
        lend = fd.get('lending') or {}
        tail = []
        if lend.get('events'):
            nc = lend.get('netCollateralUsd') or 0
            tail.append('%d lending event%s %s %s of collateral' % (lend['events'], '' if lend['events'] == 1 else 's', 'added' if nc >= 0 else 'withdrew', usd(abs(nc))))
        tail.append('%s was liquidated' % usd(lend['liquidatedUsd']) if (lend.get('liquidatedUsd') or 0) > 0 else 'no liquidations')
        ld = lev.get('data') or {}
        cluster = None
        price = ld.get('price') or (px or {}).get('close')
        near = [b for b in ld.get('ladder') or [] if price and b.get('price') and b['price'] >= price * 0.9 and (b.get('debtUsd') or 0) > 0]
        if near:
            cluster = max(near, key=lambda b: b.get('debtUsd') or 0)
            lo = cluster.get('priceLow') or cluster['price']
            tail.append('the nearest liquidation cluster is %s of debt between %s and %s USD (%.1f–%.1f%% below)' % (
                usd(cluster['debtUsd']), '{:,.4g}'.format(lo), '{:,.4g}'.format(cluster['price']), max(0.0, (1 - cluster['price'] / price) * 100), (1 - lo / price) * 100))
        move = ('%s %s%.1f%% in 24h' % (symbol, '+' if px['change'] >= 0 else '−', abs(px['change']) * 100)) if px else '%s over 24h' % symbol
        headline = move + (' while ' + ' and '.join(clauses) if clauses else '') + ('; ' + '; '.join(tail) if tail else '') + '.'
        big = sorted([x for x in ev.get('events') or [] if isinstance(x.get('usd'), (int, float))], key=lambda x: -x['usd'])[:3]
        if px:
            facts.append({'label': '24h range', 'value': '%s – %s USD' % ('{:,.4g}'.format(px['low']), '{:,.4g}'.format(px['high']))})
        facts += [{'label': '%s UTC · %s' % (_t.strftime('%H:%M', _t.gmtime(x['time'])), x.get('text') or x.get('kind')), 'value': usd(x['usd'])} for x in big]
        titled = {'whale': 'Whales', 'retail': 'Retail wallets', 'mev': 'MEV bots', 'exchange': 'Exchanges', 'contract': 'Contracts and vaults'}
        facts += [{'label': 'Net flow · ' + titled.get(c.get('class'), str(c.get('class'))), 'value': ('+' if (c.get('netUsd') or 0) >= 0 else '−') + usd(abs(c.get('netUsd') or 0))}
                  for c in sorted(classes, key=lambda c: -abs(c.get('netUsd') or 0))[:3]]
        if cluster:
            facts.append({'label': 'Nearest liquidation cluster', 'value': '%s at %s–%s USD' % (usd(cluster['debtUsd']), '{:,.4g}'.format(cluster.get('priceLow') or cluster['price']), '{:,.4g}'.format(cluster['price']))})
        message = headline if (classes or big or px) else 'No flow, event or price data is available for %s right now.' % symbol
        title = 'Why %s moved' % symbol
        card_extra = {'headline': headline, 'events': [{'time': x['time'], 'kind': x.get('kind'), 'usd': x['usd']} for x in big]}
    lead = next((b for b in sorted((state.get('data') or {}).get('buckets') or [], key=lambda b: -(b.get('share') or 0)) if b.get('key') != 'other'), None)
    if lead and kind not in ('flows', 'move'):
        facts.append({'label': lead.get('label'), 'value': pct(lead.get('share')) + ' of supply'})
    card = {**(card_extra if kind == 'move' else {}), 'kind': 'economy', 'title': title, 'market': market, 'symbol': symbol, 'network': chain, 'asOf': asof or state.get('asOf'),
            'facts': facts[:7], 'overlay': overlay,
            'note': 'Every value comes from the Economic Machine and Ticking candles; %s data.' % ('live' if any(x.get('status') == 'live' for x in (lev, flows, state)) else 'map-snapshot')}
    return {'state': 'wallet_card', 'message': message, 'card': card}


def resolve(q, chart):
    """Return a resolver result for wallet questions, or None to fall through to the Ticking routing."""
    text = q.lower()
    addrs = list(dict.fromkeys(a.lower() for a in ADDR.findall(q)))
    ctx = re.sub(r'[^A-Z0-9]', '', (chart or {}).get('symbol', '').upper()) or None
    try:
        if len(addrs) > 1:
            return {'state': 'clarify', 'message': 'Ask about one wallet at a time.'}
        if addrs and re.search(r'\b(chart|candles?|price|token|terminal|ohlc)\b', text) and not re.search(r'\b(wallet|holder|trace|flows?)\b', text):
            return None   # a token-address chart request; Ticking's native routing opens the network market
        if addrs:
            if re.search(r'\b(trace|flows?|where|counterpart\w*|sent|received|went|go|goes|move[ds]?)\b', text):
                card = get('trace', address=addrs[0])
                return {'state': 'wallet_card', 'message': trace_message(card), 'card': card}
            card = get('wallet', address=addrs[0])
            return {'state': 'wallet_card', 'message': wallet_message(card), 'card': card}
        econ = ('liquidations' if re.search(r'\bliquidat\w*', text) else 'leverage' if re.search(r'\b(who is leveraged|leveraged|leverage on)\b', text)
                else 'flows' if re.search(r'\b(money flows?|net flows?|inflows?|outflows?|flow of money|flows? for)\b', text)
                else 'move' if re.search(r'\bwhy\b', text) and re.search(r'\b(move[ds]?|moving|pump\w*|dump\w*|drop\w*|fell|fall\w*|rise|rose|up|down|crash\w*|rall\w*)\b', text) else None)
        if econ:
            syms = [s for s in symbols(q) if s not in STABLE] or ([ctx] if ctx and ctx not in STABLE else [])
            if not syms:
                return {'state': 'clarify', 'message': 'Which token? Try “liquidation levels for NVDA” or open an Arbitrum One or Robinhood Chain market first.'}
            return economy_card(econ, syms[0], chart)
        if re.search(r'\b(borrow\w*|debt|loans?|leverag\w*)\b', text) and re.search(r'\b(usdg|stock|collateral|against)\b', text):
            syms = [s for s in symbols(q) if s not in STABLE] or ([ctx] if ctx and ctx not in STABLE else [])
            sym = syms[0] if syms else ''
            card = get('top', symbol=sym)
            return {'state': 'wallet_card', 'message': top_message(card, sym), 'card': card}
        if re.search(r'\b(holders?|who holds|whales?|top wallets|largest wallets|who owns|owners?)\b', text):
            syms = [s for s in symbols(q) if s not in STABLE] or ([ctx] if ctx else [])
            if not syms:
                return {'state': 'clarify', 'message': 'Which token? Try “NVDA holders”, or open a Robinhood Chain or Arbitrum market first.'}
            market = (chart or {}).get('asset')
            if not (isinstance(market, str) and MARKET.fullmatch(market) and syms[0] == ctx):
                market = None
            try:
                card = get('holders', **({'market': market} if market else {'symbol': syms[0]}))
            except LookupError as e:
                return {'state': 'message', 'message': str(e) if 'Unknown market' not in str(e) else 'Holders are mapped for reviewed Arbitrum One and Robinhood Chain markets.'}
            return {'state': 'wallet_card', 'message': holders_message(card), 'card': card}
    except LookupError as e:
        return {'state': 'message', 'message': str(e)[:300]}
    return None
