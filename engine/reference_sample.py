"""Causal multi-pool price samples, distinct from a raw execution candle.

Use the trailing five-second VWAP of each pool and then its distinct-pool median.
A high-fee venue or repeated dust legs cannot set both extremes
of a liquid token's reference by alternating with other venues. Actual pool
fills remain untouched. No future sample or exchange comparison enters pricing.
"""
import statistics
from collections import deque


class PoolReferenceWindow:
    def __init__(self, age=5, maximum=64):
        self.age = age
        self.maximum = maximum
        self.pools = {}

    def update(self, stamp, flows):
        for pool, values in list(self.pools.items()):
            while values and stamp-values[0][0]>self.age:values.popleft()
            if not values or values[-1][0]>stamp:del self.pools[pool]
        for pool, (base, quote) in flows.items():
            values=self.pools.setdefault(pool,deque())
            if values and values[-1][0]==stamp:values.pop()
            values.append((stamp,base,quote))
        if len(self.pools) > self.maximum:
            self.pools = dict(sorted(self.pools.items(), key=lambda row: (row[1][-1][0], sum(v[2] for v in row[1]), row[0]), reverse=True)[:self.maximum])
        return statistics.median(sum(v[2] for v in values)/sum(v[1] for v in values) for values in self.pools.values())
