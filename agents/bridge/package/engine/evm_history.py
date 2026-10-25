"""Arbitrum One and Robinhood Chain execution history behind the bridge's reader contract.

One identity, two read-only sources:
  hot   em-charts/hot/<chain>/history.sqlite3, the live tail's suffix in the observed store's
        fills/bars DDL (identity '<tx>:<log>', slot = block, tx_order = tx index, event_order = log
        index, 60/300/3600 bars, `state.data_version` bumped per commit);
  deep  the em-charts loopback read API over its DuckDB archive, for buckets before the suffix.
Pool keys are '0x<pool>'; the token reference is the 'token:0x<token>' composite. Prices and
quote volumes are USD (em_pool_reference_v2 converts the quote leg with a same-minute anchor
rate); base volume is in base-token units. Nothing here writes, and no provider is contacted.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from chart_intervals import WEEK, bucket_start
from identity import EVM, EVM_ADDRESS, EVM_POOL

POLICY = 'em_pool_reference_v2'
HOT_ROOT = Path(os.environ.get('TICKING_EVM_HOT_ROOT', '/srv/skew/em-charts/hot'))
DEEP = os.environ.get('TICKING_EVM_DEEP', 'http://127.0.0.1:8895')
EXPLORERS = {'arbitrum': 'https://arbiscan.io', 'robinhood': 'https://robinhoodchain.blockscout.com'}
CHAIN_NAMES = {'arbitrum': 'Arbitrum One', 'robinhood': 'Robinhood Chain'}
MAX_CARRY = 5
SAMPLING = ('Per minute the most established pool printing sets the reference; pools within 5% of it are admitted; '
            'open/close are the earliest/latest admitted prints; wicks beyond 10% are dropped.')
CONVERSION = ('USD stables at $1; WETH/ETH, BTC and ARB quotes are converted with a same-minute composite of their '
              'own USD-stable pools (as-of, at most 6 h old).')


def label(seconds):
    for unit, size in (('w', WEEK), ('d', 86400), ('h', 3600), ('m', 60)):
        if seconds % size == 0:
            return '%d%s' % (seconds // size, unit)
    raise ValueError('Intervals start at 1m')


class EvmHistory:
    stored_intervals = (60, 300, 3600)
    source_union_policy = 'em_deep_prefix_hot_suffix_v1'

    def __init__(self, network, hot_root=HOT_ROOT, deep=DEEP):
        if network not in EVM:
            raise ValueError('Unsupported EVM network')
        self.network = network
        self.path = Path(hot_root) / network / 'history.sqlite3'
        self.deep_base = deep.rstrip('/')
        self.lock = threading.Lock()
        self.memo = {}

    # ---------- sources
    @contextmanager
    def connect(self):
        if not self.path.exists():
            raise ValueError('EVM hot store unavailable')
        c = sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True, timeout=3)
        try:
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA query_only=ON')
            with c:
                yield c
        finally:
            c.close()

    def deep(self, path, ttl, **query):
        """Bounded loopback read; responses are memoised per exact query."""
        key = path + '?' + urlencode(sorted((k, v) for k, v in query.items() if v is not None))
        now = time.monotonic()
        with self.lock:
            hit = self.memo.get(key)
            if hit and now - hit[0] < hit[1]:
                return hit[2]
        try:
            with urlopen(Request(self.deep_base + key, headers={'Accept': 'application/json'}), timeout=4) as r:
                if r.status != 200:
                    raise ValueError('Deep history unavailable')
                data = json.loads(r.read(4 * 1024 ** 2))
        except (OSError, ValueError):
            if hit:   # the archive paused (rebuild, sparkline pass): answer from the last good copy of this exact read
                return hit[2]
            raise
        with self.lock:
            if len(self.memo) > 512:
                for k in sorted(self.memo, key=lambda k: self.memo[k][0])[:128]:
                    self.memo.pop(k, None)
            self.memo[key] = (now, ttl, data)
        return data

    def market(self, token):
        """Archive market detail (pools, first bucket). Stale-while-revalidate: the archive's detail read can
        pause while it rebuilds sparklines, so a known market is answered from the last copy."""
        key = 'market:' + token
        with self.lock:
            hit = self.memo.get(key)
            if hit and time.monotonic() - hit[0] > 300 and not hit[1]:
                self.memo[key] = (hit[0], True, hit[2])
                threading.Thread(target=self._market, args=(token,), daemon=True).start()
        return hit[2] if hit else self._market(token)

    def _market(self, token):
        key = 'market:' + token
        try:
            with urlopen(Request(self.deep_base + '/api/public/markets/%s:%s' % (self.network, token), headers={'Accept': 'application/json'}), timeout=8) as r:
                data = json.loads(r.read(4 * 1024 ** 2))
        except (OSError, ValueError):
            with self.lock:
                hit = self.memo.get(key)
                if hit:
                    self.memo[key] = (hit[0], False, hit[2])
            return hit[2] if hit else None
        with self.lock:
            self.memo[key] = (time.monotonic(), False, data)
        return data

    def catalog(self):
        """Market observations for catalog rows. Served from the last snapshot while a refresh runs
        off the request path, so a slow archive read never holds a bridge lane."""
        with self.lock:
            snap, stamp, busy = getattr(self, '_catalog', None), getattr(self, '_catalog_at', 0), getattr(self, '_catalog_busy', False)
            if snap is not None and time.monotonic() - stamp > 30 and not busy:
                self._catalog_busy = True
                threading.Thread(target=self._refresh_catalog, daemon=True).start()
        return snap if snap is not None else self._refresh_catalog()

    def _refresh_catalog(self):
        try:
            with urlopen(Request(self.deep_base + '/api/public/markets?' + urlencode({'network': self.network}), headers={'Accept': 'application/json'}), timeout=6) as r:
                rows = json.loads(r.read(16 * 1024 ** 2))
            rows = rows.get('tokens') or rows.get('markets') or []
            snap = {'network': self.network, 'policy': POLICY,
                    'tokens': [{k: t.get(k) for k in ('address', 'symbol', 'name', 'kind', 'price', 'change', 'volume', 'usdVolumeAllTime', 'swaps', 'pools',
                                                      'updated_at', 'stale', 'sparkline', 'quality', 'qualityNote', 'uiMultiplier')}
                               for t in rows if t.get('network') == self.network and EVM_ADDRESS.fullmatch(t.get('address') or '')]}
            with self.lock:
                self._catalog, self._catalog_at = snap, time.monotonic()
            return snap
        finally:
            with self.lock:
                self._catalog_busy = False

    # ---------- identity
    def resolve_token(self, token):
        m = self.market(token)
        if not m:
            return {'network': self.network, 'mint': token, 'ready': False, 'state': 'token_reference_pending', 'coverage': None}
        first = (m.get('firstTime') or 0) // 1000 or None
        last = (m.get('dataUntil') or 0) // 1000 or None
        return {'network': self.network, 'mint': token, 'ready': bool(first), 'state': 'reference_history_available' if first else 'awaiting_priced_executions',
                'minuteCandles': None, 'first': first, 'last': last, 'coverage': self.coverage(token=token, market=m),
                'quoteMint': 'USD', 'aggregation': POLICY, 'historyPolicy': POLICY, 'live': False}

    def pools_for(self, token):
        m = self.market(token)
        items = ((m or {}).get('pools') or {}).get('items') or []
        observed = {}
        if self.path.exists() and items:
            with self.connect() as c:
                for p in items[:100]:
                    observed[p['address']] = c.execute('SELECT max(time) FROM bars WHERE pool=? AND interval=60', (p['address'],)).fetchone()[0]
        return [{'pool': p['address'], 'baseMint': token, 'quoteMint': 'USD', 'quote': p.get('quote'), 'quoteToken': p.get('quoteToken'),
                 'program': 'evm_dex_pool', 'minuteCandles': p.get('swaps') or 0, 'executions': p.get('swaps') or 0, 'usdVolume': p.get('usdVolume'),
                 'rank': p.get('rank'), 'inverted': False, 'observedThrough': observed.get(p['address'])} for p in items[:100]], len(items) > 100

    def pool_token(self, pool):
        if self.path.exists():
            with self.connect() as c:
                row = c.execute('SELECT token FROM pools WHERE pool=?', (pool,)).fetchone()
            if row:
                return row[0]
        try:
            d = self.deep('/api/public/engine/candles', 300, aggregation='pool', pool='%s:%s' % (self.network, pool), interval='1d', limit=1, gap_policy='recorded')
        except (OSError, ValueError):
            return None
        mint = (d.get('mint') or '').split(':')[-1]
        return mint if EVM_ADDRESS.fullmatch(mint) else None

    def resolve(self, token=None, pool=None, quote_mint=None):
        if bool(token) == bool(pool) or token and not EVM_ADDRESS.fullmatch(token) or pool and not EVM_POOL.fullmatch(pool):
            raise ValueError('Supply one %s token or pool' % CHAIN_NAMES[self.network])
        if quote_mint not in (None, 'USD'):
            raise ValueError('EVM references are quoted in USD')
        if pool:
            base = self.pool_token(pool)
            pools = [p for p in self.pools_for(base)[0] if p['pool'] == pool] if base else []
            if base and not pools:
                pools = [{'pool': pool, 'baseMint': base, 'quoteMint': 'USD', 'program': 'evm_dex_pool', 'minuteCandles': 0, 'executions': 0, 'inverted': False, 'observedThrough': None}]
            more = False
        else:
            pools, more = self.pools_for(token)
        preferred = pools[0] if pools and pools[0]['executions'] else None
        return {'network': self.network, 'mode': 'historical_follower_verified', 'ready': bool(preferred), 'liveReady': False,
                'state': 'history_available' if preferred else 'awaiting_recorded_executions' if pools else 'no_verified_base_pool',
                'preferredPool': preferred, 'pools': pools, 'hasMorePools': more, 'mint': token or (pools[0]['baseMint'] if pools else None),
                'mintAccount': None, 'rwaClaims': [], 'observationAvailable': any(p.get('observedThrough') for p in pools),
                'entireSolanaComplete': False, 'tokenAggregationApplied': False}

    def coverage(self, token=None, market=None, start=None, end=None):
        pools = (market or {}).get('pools')
        pools = pools if isinstance(pools, int) else len((pools or {}).get('items') or []) or None
        return {'complete': False, 'archiveCompleteness': 'not_attested', 'source': 'onchain_dex_swaps', 'network': self.network,
                'chain': CHAIN_NAMES[self.network], 'policy': POLICY, 'historyPolicy': POLICY, 'commitment': 'follower_verified_chunks',
                'priceSampling': SAMPLING, 'quoteConversion': CONVERSION, 'volumeDefinition': 'USD turnover of admitted pool prints (quote leg).',
                'start': start, 'end': end, 'observedPoolCount': pools, 'pricedPoolCount': pools,
                'sourceUnionPolicy': self.source_union_policy, 'canonicalFinalityVerified': False, 'shredJoinVerified': False,
                'seamPolicy': 'Deep archive buckets before the hot suffix\'s first retained minute; hot bars from then on. Neither source is merged inside one bucket.'}

    # ---------- candles
    def hot_window(self, c, key, period, bound):
        first = c.execute('SELECT min(time) FROM bars WHERE pool=? AND interval=60', (key,)).fetchone()[0]
        if first is None:
            return None, []
        start = bucket_start(first, period)
        hot_from = start if start == first else start + period
        base = 60   # the suffix is at most a few days of minutes; never depend on a coarser bucket being written yet
        groups = {}
        for r in c.execute('SELECT time,open,high,low,close,base_volume,quote_volume,count FROM bars WHERE pool=? AND interval=? AND time>=? AND time<? ORDER BY time',
                           (key, base, hot_from, bound)):
            t = bucket_start(r['time'], period)
            if t + period > bound:
                continue
            b = groups.get(t)
            if b is None:
                groups[t] = dict(time=t, open=r['open'], high=r['high'], low=r['low'], close=r['close'], baseVolume=r['base_volume'],
                                 volume=r['quote_volume'], count=r['count'])
            else:
                b.update(high=max(b['high'], r['high']), low=min(b['low'], r['low']), close=r['close'], baseVolume=b['baseVolume'] + r['base_volume'],
                         volume=b['volume'] + r['quote_volume'], count=b['count'] + r['count'])
        return hot_from, [groups[t] for t in sorted(groups)]

    def deep_bars(self, key, period, before, limit, settled=False):
        token = key.startswith('token:')
        ident = '%s:%s' % (self.network, key[6:] if token else key)
        immutable = before is not None and before < time.time() - 3 * 86400
        d = self.deep('/api/public/engine/candles', 600 if immutable else 300 if settled else 15, aggregation='token' if token else 'pool',
                      **({'mint': ident} if token else {'pool': ident}), interval=label(period), limit=limit,
                      before=before * 1000 if before is not None else None, gap_policy='recorded')
        bars = [dict(time=b['time'] // 1000, open=b['open'], high=b['high'], low=b['low'], close=b['close'], baseVolume=b.get('baseVolume') or 0.0,
                     volume=b.get('volume') or 0.0, count=b.get('count') or 0) for b in d.get('candles', []) if not b.get('synthetic')]
        return bars, bool(d.get('hasMore')), d

    def history(self, key, interval=60, limit=300, before=None, gap_policy='recorded'):
        if not 60 <= interval <= WEEK or interval % 60 or not 1 <= limit <= 600 or gap_policy not in ('recorded', 'carry'):
            raise ValueError('Unsupported EVM history request')
        token = key.startswith('token:')
        address = key[6:] if token else key
        if not (EVM_ADDRESS if token else EVM_POOL).fullmatch(address):
            raise ValueError('Invalid EVM history key')
        bound = bucket_start(before, interval) if before is not None else 2 ** 62
        hot_from, hot, through = None, [], None
        if self.path.exists():
            with self.connect() as c:
                c.execute('BEGIN')
                hot_from, hot = self.hot_window(c, key, interval, bound)
                through = c.execute('SELECT max(time) FROM bars WHERE pool=? AND interval=60', (key,)).fetchone()[0]
                state = dict(c.execute('SELECT key,value FROM state').fetchall())
        else:
            state = {}
        bars, more, meta = hot[-limit:], True, {}
        if len(hot) < limit:
            deep_before = min(bound, hot_from) if hot_from is not None else (before and bound)
            older, more, meta = self.deep_bars(key, interval, deep_before, limit - len(hot) if hot else limit, settled=hot_from is not None)
            if hot_from is not None:
                older = [b for b in older if b['time'] < hot_from]
            bars = (older + hot)[-limit:]
        base = address if token else self.pool_token(address)
        for b in bars:
            b.update(synthetic=False, sourceTime=b['time'])
        carried = 0
        if gap_policy == 'carry' and bars:
            filled = [bars[0]]
            for b in bars[1:]:
                prev = filled[-1]
                gap = (b['time'] - prev['time']) // interval - 1
                for i in range(1, gap + 1 if 0 < gap <= MAX_CARRY else 1):
                    p = prev['close']
                    filled.append(dict(time=prev['time'] + i * interval, open=p, high=p, low=p, close=p, baseVolume=0, volume=0, count=0,
                                       synthetic=True, gapKind='unverified_no_record', sourceTime=prev['sourceTime']))
                filled.append(b)
            bars = filled[-limit:]
            carried = sum(b['synthetic'] for b in bars)
        observed = through + 60 if through is not None else None
        first = (meta.get('history') or {}).get('start') or ((self.market(address) or {}).get('firstTime') if token else None)
        start = first // 1000 if isinstance(first, int) else bars[0]['time'] if bars and not (bars and more) else None
        identity = {'network': self.network, 'market': address, 'baseMint': base, 'quoteMint': 'USD',
                    'series': 'reference' if token else 'trade', 'historyPolicy': POLICY, 'venue': 'onchain_dex',
                    **({'aggregation': POLICY} if token else {'pool': address})}
        return {'candles': bars, 'identity': identity, 'sequence': int(time.time() * 1000), 'hasMore': bool(bars) and more,
                'nextBefore': bars[0]['time'] if bars else None, 'observationAvailable': hot_from is not None, 'observedThrough': observed,
                'engineThrough': int(state['data_until_ms']) // 1000 if state.get('data_until_ms') else None,
                'coverage': {**self.coverage(market=self.market(address) if token else None, start=start, end=observed), 'hotFrom': hot_from,
                             'resolutionStarts': {k: v // 1000 for k, v in ((meta.get('history') or {}).get('resolutionStarts') or {}).items() if isinstance(v, int)} or None},
                'carriedBars': carried, 'fillPolicy': 'recorded',
                'gapDisclosure': 'At most five short intervals carry the prior close; longer gaps remain unfilled. Completeness of the on-chain swap archive is not attested.'}

    # ---------- executions
    def row(self, r):
        tx = r['tx'] or r['identity'].split(':')[0]
        return {'identity': r['identity'], 'pool': r['pool'], 'signature': tx, 'tx': tx, 'time': r['time'], 'slot': r['slot'], 'tx_order': r['tx_order'],
                'event_order': r['event_order'], 'price': r['price'], 'reference_price': r['price'], 'reference_eligible': bool(r['eligible']),
                'base_volume': r['base_volume'], 'quote_volume': r['quote_volume'], 'quote_mint': 'USD', 'usd': r['usd'], 'side': r['side'] if r['side'] in ('B', 'A') else None,
                'trader': r['trader'], 'tradeId': r['slot'] * 100000 + r['event_order'], 'seq': 0, 'commitment': 'follower_verified_chunks',
                'provenance': {'schema': 1, 'network': self.network, 'pool': r['pool'], 'transaction': tx, 'block': r['slot'],
                               'transactionIndex': r['tx_order'], 'logIndex': r['event_order'], 'basis': 'decoded_swap_log',
                               'commitmentSource': 'em_follower_verified_chunks', 'independentConsensusVerified': False,
                               'amountOrientation': 'usd_converted_quote_leg', 'exactAmountsAvailable': False,
                               'explorer': EXPLORERS[self.network] + '/tx/' + tx,
                               'side': 'B = the transaction sender received the market token, A = sent it'}}

    def tape(self, where, args, limit, windows=(None,)):
        """Newest executions first in SQL, returned oldest first like the Solana tape.

        A busy token holds most of the suffix: recent windows on the time index find its
        page without sorting every retained fill; sparse tokens fall back to the token
        index. Order is always block, transaction, log, identity.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid tape limit')
        if not self.path.exists():
            return []
        rows = []
        with self.connect() as c:
            c.execute('BEGIN')
            now = int(time.time())
            for window in windows:
                hint, clause, extra = (' INDEXED BY reference_time', ' AND f.time>=?', [now - window]) if window else ('', '', [])
                rows = c.execute('SELECT f.identity,f.pool,f.time,f.slot,f.tx_order,f.event_order,f.price,f.base_volume,f.quote_volume,'
                                 'm.side,m.trader,m.tx,m.usd,m.eligible FROM fills f' + hint + ' JOIN fill_meta m ON m.identity=f.identity WHERE ' + where + clause +
                                 ' ORDER BY f.slot DESC,f.tx_order DESC,f.event_order DESC,f.identity DESC LIMIT ?', [*args, *extra, limit]).fetchall()
                if len(rows) >= limit:
                    break
        return [self.row(r) for r in reversed(rows)]

    def trades(self, token, limit=50):
        rows = [{**r, 'mint': token} for r in self.tape('m.token=?', [token], limit, (900, 7200, None))]
        return {'trades': rows, 'identity': {'network': self.network, 'market': token, 'baseMint': token, 'quoteMint': 'USD', 'series': 'pool_executions'},
                'commitment': 'follower_verified_chunks', 'live': False, 'latencySla': None,
                'coverage': {'window': 'hot suffix (3 days)', 'side': 'B = the transaction sender received the market token, A = sent it'}}

    def pool_trades(self, pool, limit=50):
        base = self.pool_token(pool)
        if not base:
            raise ValueError('Unknown pool')
        rows = [{**r, 'mint': base} for r in self.tape('f.pool=? AND m.token=?', [pool, base], limit)]
        return {'identity': {'network': self.network, 'market': pool, 'pool': pool, 'baseMint': base, 'quoteMint': 'USD', 'series': 'pool_executions'},
                'trades': rows, 'commitment': 'follower_verified_chunks', 'live': False,
                'dataQuality': {'schema': 1, 'completePoolHistoryVerified': False, 'independentConsensusVerified': False,
                                'sampledExecutions': len(rows), 'exactAmountRecords': 0, 'emptyTapeMeans': 'no_retained_executions_in_requested_page',
                                **({'sampledSlotRange': {'first': rows[0]['slot'], 'last': rows[-1]['slot']}} if rows else {})}}
