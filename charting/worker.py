"""em-charts background worker: the analytics refresh (token state, flows/events, flow map) and the live tail, in a
process of their own, so the read server (serve_reads.py) is never starved by them. Writes the same files as before
(analytics.sqlite3, the hot SQLite suffix, tail state); readers open them read-only.
The flow-map precompute runs inside the analytics refresh through flowmap.py's loader, which re-imports flowmap_core.py
whenever that file changes, so flow-map code reloads happen here, in the worker; the HTTP routes keep reading the shared
analytics sqlite rows.

usage: python worker.py [--status /srv/skew/em-charts/data/worker-status.json] [--no-analytics] [--no-live]
"""
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emapi  # noqa: E402
from common import CHAINS, ROOT  # noqa: E402
import common  # noqa: E402
from emread import Reader  # noqa: E402

STATUS = sys.argv[sys.argv.index('--status') + 1] if '--status' in sys.argv else os.path.join(ROOT, 'data', 'worker-status.json')


def log(msg):
    import server   # same log file and format as the read server
    server.log(msg)


def main():
    readers = {name: Reader(name) for name in CHAINS if os.path.exists(f"{ROOT}/data/{name}/candles.duckdb")}
    jobs, tails = {}, {}
    if '--no-analytics' not in sys.argv:
        for name, ch in readers.items():
            if name in getattr(common, "COMPACT", ()):   # compact-served chains: no full econ on skew, no analytics
                continue
            jobs[name] = emapi.AnalyticsJob(name, ch, log=log)
            jobs[name].start()
    if '--no-live' not in sys.argv:
        from live import LiveTail
        for name in readers:
            tails[name] = LiveTail(name, log=log)
            tails[name].start()
    log(f"em-charts worker running chains={list(readers)} analytics={bool(jobs)} live={bool(tails)}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        state = {name: {'live': {'last_cycle': getattr(tails.get(name), 'last_cycle', None), 'errors': getattr(tails.get(name), 'errors', None)},
                        'analytics': {'last': getattr(jobs.get(name), 'last', None), 'errors': getattr(jobs.get(name), 'errors', None)}}
                 for name in readers}
        tmp = STATUS + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, default=str)
        os.replace(tmp, STATUS)
        time.sleep(15)


if __name__ == '__main__':
    main()
