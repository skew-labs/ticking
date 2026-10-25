"""Memory-bounded transfer ASOF, preserving the complete source row multiset.

Each row-number slice probes all of bm: the preceding minute may lie outside
its Parquet row group or file. No aggregation or floating-point summation occurs.
Only DuckDB is required; file_row_number lets its Parquet reader prune slices.
"""
import os
import time


def _stamp(path):
    s = os.stat(path)
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def build_window(c, files, minimum_block, *, batch_rows=65536, log=None):
    if not 1024 <= batch_rows <= 262144:
        raise ValueError('window batch rows must be 1024..262144')
    minimum_block = int(minimum_block)
    c.execute('CREATE OR REPLACE TABLE xf(token VARCHAR, holder VARCHAR, delta DOUBLE, minute BIGINT)')
    stats = dict(files=0, rowGroupsRead=0, rowGroupsSkipped=0, batches=0, inputRows=0, maxBatchRows=0)
    started = time.monotonic()
    for path in files:
        path = os.fspath(path)
        before = _stamp(path)
        groups = c.execute('''SELECT row_group_id, row_group_num_rows,
            try_cast(coalesce(stats_max_value, stats_max) AS UBIGINT)
            FROM parquet_metadata(?) WHERE path_in_schema = 'bn' ORDER BY row_group_id''', [path]).fetchall()
        if not groups:
            # An empty, valid file can have no groups. Still enforce its schema.
            names = {r[0] for r in c.execute('DESCRIBE SELECT * FROM read_parquet(?)', [path]).fetchall()}
            if not {'bn', 'token', 'holder', 'delta'}.issubset(names):
                raise ValueError('transfer window schema incomplete')
        offset = 0
        for _, count, maximum in groups:
            end = offset + count
            if maximum is not None and maximum < minimum_block:
                stats['rowGroupsSkipped'] += 1
            else:
                stats['rowGroupsRead'] += 1
                for start in range(offset, end, batch_rows):
                    stop = min(start + batch_rows, end)
                    c.execute('''INSERT INTO xf
                        SELECT x.token, x.holder, x.delta, b.minute
                        FROM (SELECT bn, token, holder, delta
                              FROM read_parquet(?, file_row_number=true)
                              WHERE file_row_number >= ? AND file_row_number < ? AND bn >= ?) x
                        ASOF JOIN bm b ON x.bn >= b.first_block''', [path, start, stop, minimum_block])
                    stats['batches'] += 1
                    stats['inputRows'] += stop - start
                    stats['maxBatchRows'] = max(stats['maxBatchRows'], stop - start)
            offset = end
        if _stamp(path) != before:
            raise ValueError('transfer window input changed during refresh')
        stats['files'] += 1
    stats['seconds'] = round(time.monotonic() - started, 3)
    if log:
        log('transfer window ' + str(stats))
    return stats
