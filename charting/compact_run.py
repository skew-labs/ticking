"""compact_run.py <chain> <step> [args]: em-charts build steps for a compact-served chain under the Option A caps.

Steps: bars <range_id ...> | recent | tail | db
The em-charts scripts run unchanged; common.connect / guard are wrapped first:
  - at most 4 threads and 8 GB per DuckDB connection, temp capped at 15 GB (run it under nice -n 19 ionice -c3);
  - the disk guard is the Option A pre-flight (75 GB free) instead of em-charts' 100 GB;
  - executemany with no rows is a no-op (chains without stock tokens; build_db.py itself stays untouched, so the
    anchored edit arc_enable.py makes there still applies);
  - MIN_USD for the chain comes from evm_cfg.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, "/srv/skew/em-realtime")
os.environ.setdefault("EM_THREADS", "4")
import common  # noqa: E402
import evm_cfg  # noqa: E402

_connect, _guard = common.connect, common.guard
PREFLIGHT_GB = 75


def _gb(m):
    m = str(m).upper().strip()
    return float(m[:-2]) if m.endswith("GB") else float(m[:-2]) / 1024 if m.endswith("MB") else 8.0


class _Conn(object):
    def __init__(self, c):
        self._c = c

    def executemany(self, q, rows=None):
        if not rows:
            return self._c
        return self._c.executemany(q, rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._c.close()

    def __getattr__(self, k):
        return getattr(self._c, k)


def connect(path=":memory:", threads=4, mem="6GB", read_only=False):
    c = _connect(path, threads=min(int(threads), 4), mem=mem if _gb(mem) <= 8 else "8GB", read_only=read_only)
    c.execute("SET max_temp_directory_size='15GB'")
    return _Conn(c)


def guard(step, floor=100):
    return _guard(step, floor=PREFLIGHT_GB)


common.connect, common.guard = connect, guard


def main():
    chain, step, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
    if chain not in evm_cfg.ANCHORS:
        raise SystemExit(f"{chain} is not a compact-served chain")
    common.ANCHORS.setdefault(chain, evm_cfg.ANCHORS[chain])
    if step == "bars":
        import build_bars
        build_bars.run(chain, {int(x) for x in rest})
    elif step == "recent":
        import build_bars
        build_bars.recent_trades(chain)
    elif step == "tail":
        import tail_bars
        tail_bars.main(chain, "realtime")
    elif step == "db":
        import build_db
        build_db.main(chain, evm_cfg.MIN_USD[chain])
    else:
        raise SystemExit(f"unknown step {step}")


if __name__ == "__main__":
    main()
