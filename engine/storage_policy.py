"""Bounded storage budgets on the shared Canada host; never delete originals.

2026-10-09 owner removed the fixed original-retention quota. Keep 32 GiB for
filesystem, other services and recovery. Available disk is still finite;
records are never deleted to keep a collector running.
"""
MIN_FREE_BYTES=32*1024**3
MAX_OBSERVED_BYTES=None
def require_observer_space(free_bytes,projection_bytes):
    if free_bytes<MIN_FREE_BYTES:
        raise ValueError('Observed chart storage reserve reached; retain data')
