"""Money-flow map entry point for analytics.refresh. The implementation lives in flowmap_core.py and is re-imported whenever that file
changes, so map fixes ship without restarting em-charts (the refresh calls build() every ~10 minutes)."""
import importlib
import os

_core, _mtime = None, None
CORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flowmap_core.py")


def build(*args, **kwargs):
    global _core, _mtime
    m = os.path.getmtime(CORE)
    if _core is None:
        import flowmap_core
        _core, _mtime = flowmap_core, m
    elif m != _mtime:
        _core, _mtime = importlib.reload(_core), m
    return _core.build(*args, **kwargs)
