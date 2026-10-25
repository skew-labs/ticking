#!/usr/bin/env python3
"""Shared write locks and SQLite retry for files that several Economic Machine jobs update.

Locks are flock(2) on /srv/skew/em-data/locks/<name>.lock (released automatically when the holder exits):
  galaxy.db          /srv/skew/em-galaxy/galaxy.db writers: em-refresh galaxy_v3.py / apply_known.py (any other writer
                     should take it too)
  labels-<chain>     /srv/skew/em-graphloop/export/<chain>/labels.parquet read-modify-write: em-refresh apply_known.py,
                     em-labeldelta export_merge
Readers never take them. Hold a lock only around the write phase, not while computing.

galaxy.db uses a rollback journal and is read by several servers, so a commit has to wait until no reader holds the
file; while the writer waits (PENDING lock) new readers are held back too. Writers therefore use one short
transaction per batch (BEGIN IMMEDIATE ... COMMIT, <= 500 rows), a short busy_timeout, and on SQLITE_BUSY roll back,
back off (exponential, jittered) and retry the same batch, which lets the readers through in between.
"""
import contextlib
import fcntl
import os
import random
import sqlite3
import time

LOCKS = "/srv/skew/em-data/locks"


@contextlib.contextmanager
def hold(name, timeout=1800, log=None):
    """Exclusive flock on LOCKS/<name>.lock; waits up to `timeout` seconds, then raises TimeoutError."""
    os.makedirs(LOCKS, exist_ok=True)
    f = open(f"{LOCKS}/{name}.lock", "a+")
    t0 = time.time()
    told = False
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() - t0 > timeout:
                f.close()
                raise TimeoutError(f"lock {name}: still held by another job after {timeout}s")
            if log and not told:
                f.seek(0)
                log(f"waiting for lock {name} (holder: {f.read().strip() or '?'})")
                told = True
            time.sleep(1 + random.random())
    try:
        f.seek(0)
        f.truncate()
        f.write(f"pid {os.getpid()} since {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        f.flush()
        if log and told:
            log(f"lock {name} acquired after {time.time() - t0:.0f}s")
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def is_busy(e):
    m = str(e).lower()
    return isinstance(e, sqlite3.OperationalError) and ("locked" in m or "busy" in m)


def retry(fn, what="write", tries=12, base=0.5, cap=30.0, log=None):
    """fn() must be one self-contained, idempotent transaction. Retried on SQLITE_BUSY / 'database is locked'."""
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if not is_busy(e) or i == tries - 1:
                raise
            d = min(cap, base * 2 ** i) * random.uniform(0.5, 1.0)
            if log:
                log(f"{what}: {e}; retry {i + 1}/{tries - 1} in {d:.1f}s")
            time.sleep(d)


def connect_writer(path, busy_ms=3000):
    """Autocommit connection (transactions are explicit) with a short busy_timeout for the commit wait."""
    g = sqlite3.connect(path, timeout=busy_ms / 1000, isolation_level=None)
    g.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    return g


def tx(g, fn):
    """Run fn(g) inside BEGIN IMMEDIATE / COMMIT; rolls back on any error (so a retry starts clean)."""
    try:
        g.execute("BEGIN IMMEDIATE")
        r = fn(g)
        g.execute("COMMIT")
        return r
    except BaseException:
        try:
            g.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def batched(g, sql, rows, n=500, what="write", log=None, pause=0.05):
    """executemany in batches of <= n rows, one retried short transaction each. Returns rows written."""
    for i in range(0, len(rows), n):
        part = rows[i:i + n]
        retry(lambda: tx(g, lambda c: c.executemany(sql, part)), what=what, log=log)
        time.sleep(pause)
    return len(rows)
