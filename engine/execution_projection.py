"""Bounded, WAL-ordered execution projection with shared candle views.

Only accepted normalized executions enter this module. It has no provider,
consensus or trade authority. Minute aggregates and indexes are updated once
per fill; request-specific rollups are bounded and reused across consumers.
"""
import bisect
import hashlib
import heapq
import math
import threading
import uuid
from collections import OrderedDict, deque
from typing import NamedTuple

INTERVALS = (1, 5, 15, 60, 240, 1440)


class Minute(NamedTuple):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    base: float
    count: int
    first: tuple
    last: tuple
    oldest: int
    volume_error: float = 0.
    base_error: float = 0.


def compensated(total, error, value):
    """Neumaier sum: arrival order does not discard small accepted amounts."""
    updated = total + value
    error += ((total - updated) + value if abs(total) >= abs(value)
              else (value - updated) + total)
    return updated, error


class ExecutionTape:
    def __init__(self, capacity=20000, replay=2048, cache_entries=128,
                 cache_items=40000):
        if min(capacity, replay, cache_entries, cache_items) < 1:
            raise ValueError('Projection bounds must be positive')
        self.capacity = capacity
        self.rows = {}
        self.market_rows = {}
        self.market_order = {}
        self.fork_rows = {}
        self.bucket_rows = {}
        self.bucket_order = {}
        self.bucket_prices = {}
        self.bucket_times = {}
        self.minutes = {}
        self.minute_times = {}
        self.versions = {}
        self.heap = []
        self.log = deque(maxlen=replay)
        self.sequence = 0
        self.epoch = uuid.uuid4().hex
        self.root = 0
        self.truncated = False
        self.lock = threading.Lock()
        self.views = OrderedDict()
        self.cache_entries = cache_entries
        self.cache_items = cache_items
        self.view_items = 0
        self.metrics = dict(viewBuilds=0, viewHits=0, bucketRebuilds=0,
                            capturedMinutes=0)

    @staticmethod
    def order(row):
        return (row['slot'], row['transactionIndex'], row['eventIndex'], row['id'])

    def _add(self, row):
        key = row['id']; market = row['marketId']; order = self.order(row)
        self.rows[key] = row
        self.market_rows.setdefault(market, set()).add(key)
        bisect.insort_right(self.market_order.setdefault(market, []), (order, key))
        self.fork_rows.setdefault(row['fork'], set()).add(key)
        bucket = row['time'] // 60 * 60
        self.bucket_rows.setdefault((market, bucket), set()).add(key)
        bisect.insort_right(self.bucket_order.setdefault((market, bucket), []), (order, key))
        bisect.insort_right(self.bucket_prices.setdefault((market, bucket), []), (row['price'], key))
        bisect.insort_right(self.bucket_times.setdefault((market, bucket), []), (row['time'], key))
        minutes = self.minutes.setdefault(market, {})
        previous = minutes.get(bucket)
        if previous is None:
            bisect.insort_right(self.minute_times.setdefault(market, []), bucket)
            minutes[bucket] = Minute(bucket, row['price'], row['price'], row['price'],
                                     row['price'], row['quoteVolume'], row['baseVolume'],
                                     1, order, order, row['time'])
        else:
            volume, ve = compensated(previous.volume, previous.volume_error, row['quoteVolume'])
            base, be = compensated(previous.base, previous.base_error, row['baseVolume'])
            minutes[bucket] = Minute(bucket,
                row['price'] if order < previous.first else previous.open,
                max(previous.high, row['price']), min(previous.low, row['price']),
                row['price'] if order > previous.last else previous.close,
                volume, base, previous.count + 1, min(order, previous.first),
                max(order, previous.last), min(row['time'], previous.oldest), ve, be)
        heapq.heappush(self.heap, (order, key))

    def _remove(self, key, dirty):
        row = self.rows.pop(key)
        market = row['marketId']; bucket = row['time'] // 60 * 60
        self.market_rows[market].remove(key)
        ordered = self.market_order[market]
        entry = (self.order(row), key)
        offset = bisect.bisect_left(ordered, entry)
        if offset >= len(ordered) or ordered[offset] != entry:
            raise RuntimeError('Execution projection order index mismatch')
        ordered.pop(offset)
        fork = self.fork_rows[row['fork']]; fork.remove(key)
        if not fork: del self.fork_rows[row['fork']]
        self.bucket_rows[(market, bucket)].remove(key)
        for index, entry in ((self.bucket_order, (self.order(row), key)),
                             (self.bucket_prices, (row['price'], key)),
                             (self.bucket_times, (row['time'], key))):
            values = index[(market, bucket)]
            offset = bisect.bisect_left(values, entry)
            if offset >= len(values) or values[offset] != entry:
                raise RuntimeError('Execution projection minute index mismatch')
            values.pop(offset)
        previous = self.minutes[market][bucket]
        volume, ve = compensated(previous.volume, previous.volume_error, -row['quoteVolume'])
        base, be = compensated(previous.base, previous.base_error, -row['baseVolume'])
        self.minutes[market][bucket] = previous._replace(volume=volume, base=base,
            volume_error=ve, base_error=be, count=previous.count-1)
        dirty.add((market, bucket))
        if not ordered:
            del self.market_order[market]; del self.market_rows[market]
        return market

    def _rebuild(self, market, bucket):
        """Indexed endpoints/extrema repair without rescanning retained fills."""
        self.metrics['bucketRebuilds'] += 1
        keys = self.bucket_rows[(market, bucket)]
        if not keys:
            del self.bucket_rows[(market, bucket)]
            del self.bucket_order[(market, bucket)]
            del self.bucket_prices[(market, bucket)]
            del self.bucket_times[(market, bucket)]
            del self.minutes[market][bucket]
            times = self.minute_times[market]
            times.pop(bisect.bisect_left(times, bucket))
            if not times:
                del self.minutes[market]; del self.minute_times[market]
            return
        ordered = self.bucket_order[(market, bucket)]
        first_order, first = ordered[0]; last_order, last = ordered[-1]
        prices = self.bucket_prices[(market, bucket)]
        self.minutes[market][bucket] = self.minutes[market][bucket]._replace(
            open=self.rows[first]['price'], high=prices[-1][0], low=prices[0][0],
            close=self.rows[last]['price'], count=len(keys), first=first_order,
            last=last_order, oldest=self.bucket_times[(market, bucket)][0][0])

    def apply(self, event, accepted, sequence=None):
        with self.lock:
            if sequence is not None:
                if sequence != self.sequence+1: raise ValueError('Projection journal cursor gap')
                self.sequence = sequence
            if not accepted: return
            if event.action == 2:
                self.root = max(self.root, event.slot)
                return
            additions = []; deleted = []; dirty = set(); touched = set()
            if event.action == 1:
                fork = bytes(event.fork).hex()
                # Canonical order also makes correction messages reproducible.
                deleted = sorted(k for k in self.fork_rows.get(fork, ())
                                 if self.rows[k]['slot'] > self.root)
                for key in deleted: touched.add(self._remove(key, dirty))
            else:
                key = str(event.market) + ':' + bytes(event.id).hex()
                if key in self.rows: return
                row = dict(id=key, marketId=event.market, fork=bytes(event.fork).hex(),
                    slot=event.slot, transactionIndex=event.transaction,
                    eventIndex=event.event, time=event.time, price=event.price,
                    quoteVolume=event.volume, baseVolume=event.base,
                    executionStatus='executed')
                if not all(math.isfinite(row[k]) and row[k] > 0
                           for k in ('price', 'quoteVolume', 'baseVolume')): return
                self._add(row); additions = [row]; touched.add(event.market)
            if not additions and not deleted: return
            if len(self.rows) > self.capacity:
                while self.heap:
                    order, oldest = heapq.heappop(self.heap)
                    # Retracted IDs may later reappear. A stale heap entry must
                    # never evict the replacement with a different order.
                    if oldest in self.rows and self.order(self.rows[oldest]) == order: break
                else: raise RuntimeError('Execution projection retention index mismatch')
                touched.add(self._remove(oldest, dirty))
                deleted.append(oldest); self.truncated = True
            for market, bucket in dirty: self._rebuild(market, bucket)
            if len(self.heap) > self.capacity * 2:
                self.heap = [(self.order(r), k) for k, r in self.rows.items()]
                heapq.heapify(self.heap)
            if sequence is None: self.sequence += 1
            for market in touched:
                if market in self.market_rows: self.versions[market] = self.sequence
                else: self.versions.pop(market, None)
            self.log.append(dict(sequence=self.sequence, trades=additions, deleted=deleted))

    @staticmethod
    def _public(row, markets, inverted):
        result = {k: v for k, v in row.items() if k not in ('fork', 'marketId')}
        result['pool'] = markets[row['marketId']]
        if result['pool'] in inverted:
            result.update(price=1 / row['price'], baseVolume=row['quoteVolume'],
                          quoteVolume=row['baseVolume'])
        if not math.isfinite(result['price']):
            raise ValueError('Requested quote exceeds finite price range')
        return result

    @staticmethod
    def _rollup(captured, markets, inverted, width, limit):
        groups = {}
        for market, values in captured.items():
            pool = markets[market]; inverse = pool in inverted
            for bar in values:
                start = bar.time // width * width
                first = (*bar.first[:3], pool, bar.first[3])
                last = (*bar.last[:3], pool, bar.last[3])
                prices = ((1 / bar.open, 1 / bar.low, 1 / bar.high, 1 / bar.close)
                          if inverse else (bar.open, bar.high, bar.low, bar.close))
                if not all(math.isfinite(p) and p > 0 for p in prices):
                    raise ValueError('Requested quote exceeds finite price range')
                volume = bar.volume + bar.volume_error
                base = bar.base + bar.base_error
                group = groups.setdefault(start, [])
                group.append((first, last, prices, base if inverse else volume,
                              volume if inverse else base, bar.count))
        bars = []
        for start in sorted(groups):
            rows = groups[start]
            opening = min(rows, key=lambda r: r[0])[2][0]
            closing = max(rows, key=lambda r: r[1])[2][3]
            volume = math.fsum(r[3] for r in rows); base = math.fsum(r[4] for r in rows)
            if not math.isfinite(volume) or not math.isfinite(base):
                raise ValueError('Retained execution volume exceeds finite range')
            bars.append(dict(time=start, open=opening, high=max(r[2][1] for r in rows),
                             low=min(r[2][2] for r in rows), close=closing,
                             volume=volume, baseVolume=base, count=sum(r[5] for r in rows)))
        return tuple(bars[-limit:]), len(bars) > limit

    def _capture(self, markets, width, before, limit):
        captured = {}
        for market in markets:
            times = self.minute_times.get(market, ())
            end = (bisect.bisect_left(times, ((before + width - 1) // width) * width)
                   if before else len(times))
            values = []; previous = None; groups = 0
            for offset in range(end - 1, -1, -1):
                minute = times[offset]; group = minute // width
                if group != previous:
                    groups += 1; previous = group
                    if groups > limit + 1: break
                values.append(self.minutes[market][minute])
            captured[market] = values
        self.metrics['capturedMinutes'] += sum(map(len, captured.values()))
        return captured

    def snapshot(self, selection, registry, interval=1, before=0, limit=300,
                 after=None, epoch=None, includeCandles=True, includeSnapshotTrades=True):
        if interval not in INTERVALS or not isinstance(limit, int) or not 1 <= limit <= 600:
            raise ValueError('Unsupported interval or page size')
        if not isinstance(before, int) or before < 0:
            raise ValueError('Invalid history cursor')
        if after is not None and (not isinstance(after, int) or after < 0):
            raise ValueError('Invalid execution cursor')
        markets = {registry[a]: a for a in selection['pools']}
        inverted = set(selection.get('invertedPools', ()))
        membership = hashlib.sha256(('|'.join(selection['pools']) + ';' +
                                    '|'.join(sorted(inverted))).encode()).hexdigest()[:16]
        selection_epoch = self.epoch + ':' + membership if selection['mode'] == 'token' else self.epoch
        with self.lock:
            sequence = self.sequence
            gap = after is not None and (epoch != selection_epoch or after > sequence or
                (after < sequence and (not self.log or after < self.log[0]['sequence'] - 1)))
            changes = []
            if after is not None and not gap:
                size = 0
                for value in reversed(self.log):
                    if value['sequence'] <= after: break
                    size += len(value['trades']) + len(value['deleted'])
                    if size > 4096: gap = True; changes = []; break
                    changes.append(value)
                changes.reverse()
            need_trades = (includeCandles and includeSnapshotTrades) or gap or after is None
            versions = tuple((m, a, self.versions.get(m, 0), a in inverted)
                             for m, a in sorted(markets.items()))
            cache_key = (versions, interval, before, limit, bool(includeCandles), need_trades)
            cached = self.views.get(cache_key)
            if cached is not None:
                self.metrics['viewHits'] += 1; self.views.move_to_end(cache_key)
            else:
                self.metrics['viewBuilds'] += 1
                # At most 200 newest entries per pool are needed for a global
                # 200-trade page. No entire retained tape is sorted on reads.
                ordered = [[self.rows[k] for _, k in self.market_order.get(m, ())[-min(limit, 200):]][::-1]
                           for m in markets] if need_trades else []
                captured = self._capture(markets, interval * 60, before, limit) if includeCandles else {}
                retained_count = sum(len(self.market_rows.get(m, ())) for m in markets)
                oldest = min((self.minutes[m][self.minute_times[m][0]].oldest
                              for m in markets if m in self.minute_times), default=None) if (need_trades or includeCandles) else None
            truncated = self.truncated
        # Rollup and page serialization stay outside the publication lock.
        # Immutable Minute snapshots make concurrent fork repairs safe.
        if cached is None:
            newest = heapq.merge(*ordered, key=lambda r: (*self.order(r)[:3], markets[r['marketId']], r['id']), reverse=True)
            trades = tuple(self._public(r, markets, inverted)
                           for _, r in zip(range(min(limit, 200)), newest))
            bars, has_more = self._rollup(captured, markets, inverted, interval * 60, limit) if includeCandles else ((), False)
            cached = (bars, has_more, trades, retained_count, oldest)
            weight = len(bars) + len(trades) + len(versions)
            with self.lock:
                # Never install a computation as the current version after a
                # concurrent fill/retraction. Its own response remains coherent.
                if all(self.versions.get(m, 0) == v for m, _, v, _ in versions) and weight <= self.cache_items:
                    previous = self.views.pop(cache_key, None)
                    if previous is not None: self.view_items -= previous[5]
                    self.views[cache_key] = (*cached, weight); self.view_items += weight
                    while len(self.views) > self.cache_entries or self.view_items > self.cache_items:
                        _, removed = self.views.popitem(last=False); self.view_items -= removed[5]
        bars, has_more, trades, retained_count, oldest = cached[:5]
        delta_rows = [self._public(r, markets, inverted) for v in changes for r in v['trades']
                      if r['marketId'] in markets]
        deleted = [k for v in changes for k in v['deleted'] if int(k.split(':', 1)[0]) in markets]
        # Consumer mutation cannot poison a shared cached view.
        return {**selection, 'sequence': sequence, 'epoch': selection_epoch, 'gap': gap,
            'candles': [dict(b) for b in bars], 'hasMore': has_more,
            'nextBefore': bars[0]['time'] if bars else None,
            'trades': [dict(t) for t in trades], 'tradeChanges': delta_rows, 'deletedTrades': deleted,
            'coverage': {**selection['coverage'], 'windowTruncated': truncated,
                         'retainedExecutions': retained_count, 'oldestExecutionTime': oldest},
            'source': 'slotline_verified_normalized_executions', 'latencyGuaranteeMs': None}
