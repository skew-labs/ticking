"""em-charts read server: the HTTP API on its own process, so candle/archive/analytics reads never wait behind the
analytics refresh (token state, flows, flow map) or the live tail. Those run in worker.py (a separate process).

usage: python serve_reads.py --port 8895 [--status /srv/skew/em-charts/data/worker-status.json]
- Uses server.py unchanged (its own --no-analytics --no-live switches), with a listen backlog of 128 (default was 5).
- /api/health keeps reporting live-tail and analytics state, read from the worker's status file (written every 15 s).
- Reader metadata (markets, pools, price overlay) refreshes in one background thread instead of inside requests.
"""
import json
import os
import sys
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ThreadingHTTPServer.request_queue_size = 128     # a 1-5 s pause must queue, not refuse, the engine's archive reads
import server  # noqa: E402
import threading  # noqa: E402
from emread import Reader  # noqa: E402

# Reader.refresh (market metadata + the hot-store price overlay, every 30 s) used to run inside whichever request found it
# stale, and every concurrent request ran it again: all endpoints paused for seconds every ~30 s. Here it is stale-while-
# revalidate: requests keep the current metadata and one background thread rebuilds it.
_refresh = Reader.refresh


def _refresh_in_background(self, force=False):
    if force or not hasattr(self, '_markets'):
        return _refresh(self, force=True)
    if time.time() - self._meta_at < 8:  # chart-cache 2026-10-23; was 15
        return
    with _REFRESH_LOCK:
        if getattr(self, '_refreshing', False):
            return
        self._refreshing = True

    def run():
        try:
            _refresh(self, force=True)
        except Exception as e:   # keep serving the last metadata; the next request retries
            server.log(f"{self.chain} metadata refresh failed: {e}")
            self._meta_at = time.time() - 20
        finally:
            self._refreshing = False
    threading.Thread(target=run, daemon=True, name=f'meta-{self.chain}').start()


_REFRESH_LOCK = threading.Lock()
Reader.refresh = _refresh_in_background

STATUS = sys.argv[sys.argv.index('--status') + 1] if '--status' in sys.argv else os.path.join(server.ROOT, 'data', 'worker-status.json')


class WorkerState:
    """Duck-types LiveTail / AnalyticsJob for health_response(): last_cycle, errors and last come from the worker process."""
    _cache = (0.0, {})

    def __init__(self, chain, kind):
        self.chain, self.kind = chain, kind

    def _state(self):
        stamp, data = WorkerState._cache
        if time.time() - stamp > 5:
            try:
                with open(STATUS) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            WorkerState._cache = (time.time(), data)
        return (data.get(self.chain) or {}).get(self.kind) or {}

    def __getattr__(self, name):
        if name in ('last_cycle', 'errors', 'last'):
            return self._state().get(name)
        raise AttributeError(name)


if __name__ == '__main__':
    from common import CHAINS
    for name in CHAINS:
        server.LIVE[name] = WorkerState(name, 'live')
        server.ANALYTICS[name] = WorkerState(name, 'analytics')
    argv = [a for a in sys.argv[1:] if a not in ('--status', STATUS)]
    sys.argv = [sys.argv[0]] + argv + ['--no-analytics', '--no-live']
    server.main()
