"""Seek retained range endpoints without scanning every historical candle.

The caller supplies a fixed, internal SELECT of its indexed time column with
parameterized identity and eligibility predicates. Both endpoints use the same
read transaction so a concurrent backfill cannot split their snapshot.
"""
def indexed_span(connection, select, parameters):
    first = connection.execute(select+' ORDER BY time LIMIT 1', parameters).fetchone()
    if first is None:
        return None
    last = connection.execute(select+' ORDER BY time DESC LIMIT 1', parameters).fetchone()
    return first[0], last[0]

def recorded_window_start(connection,mint,period,limit,bound,origin=0):
    """Last N occupied buckets, expanding only when retained history is sparse.

    Minute pages use the primary key directly. Larger pages group a bounded
    indexed range first; geometric expansion preserves sparse-history paging
    without grouping the asset's entire lifetime on every current read.
    """
    if period==60:
        return connection.execute('SELECT min(time) FROM (SELECT time FROM bars WHERE mint=? AND time<? ORDER BY time DESC LIMIT ?)',(mint,bound,limit)).fetchone()[0]
    span=indexed_span(connection,'SELECT time FROM bars WHERE mint=? AND time<?',(mint,bound))
    if span is None:return None
    first,last=span;head=(last-origin)//period*period+origin
    width=max(period,period*limit);lower=max(first,head-width+period)
    while True:
        rows=connection.execute('SELECT ((time-?)/?)*?+? AS bucket FROM bars WHERE mint=? AND time>=? AND time<? GROUP BY bucket ORDER BY bucket DESC LIMIT ?',
            (origin,period,period,origin,mint,lower,bound,limit)).fetchall()
        if len(rows)>=limit or lower<=first:return rows[-1][0] if rows else None
        width*=4;lower=max(first,head-width+period)
