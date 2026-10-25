"""em-charts shared config: anchors (USD-pricing quotes), DuckDB connection settings, paths."""
import atexit
import itertools
import os
import pwd
import shutil
import weakref

import duckdb

ROOT = "/srv/skew/em-charts"
EM = "/srv/skew/em-data"
VENV_PY = "/srv/skew/em-galaxy/.venv/bin/python"
CHAINS = ("robinhood", "arbitrum", "arc")
NATIVE = "0x0000000000000000000000000000000000000000"

# Quote anchors. kind 'usd' is valued at $1; eth/btc/arb get a minute USD series from their own
# stable-quoted pools. Lower priority = preferred quote when both sides of a swap are anchors.
ANCHORS = {
    "arbitrum": [
        ("0xaf88d065e77c8cc2239327c5edb3a432268e5831", "usd", 1, "USDC"),
        ("0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "usd", 2, "USD₮0"),
        ("0xff970a61a04b1ca14834a43f5de4533ebddb5cc8", "usd", 3, "USDC.e"),
        ("0xda10009cbd5d07dd0cecc66161fc93d7c9000da1", "usd", 4, "DAI"),
        ("0x004b506865409877c9fa29bfb1eba929984b9bbc", "usd", 5, "USDG"),
        ("0x46850ad61c2b7d64d08c9c754f45254596696984", "usd", 6, "PYUSD"),
        ("0x82af49447d8a07e3bd95bd0d56f35241523fbab1", "eth", 10, "WETH"),
        (NATIVE, "eth", 11, "ETH"),
        ("0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f", "btc", 20, "WBTC"),
        ("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", "btc", 21, "cbBTC"),
        ("0x912ce59144191c1204e64559fe8253a0e49e6548", "arb", 30, "ARB"),
    ],
    "robinhood": [
        ("0x5fc5360d0400a0fd4f2af552add042d716f1d168", "usd", 1, "USDG"),
        ("0x0bd7d308f8e1639fab988df18a8011f41eacad73", "eth", 10, "WETH"),
        (NATIVE, "eth", 11, "ETH"),
        ("0xcec185eb182c47d1ba1efc84e6959e18cd620be4", "btc", 20, "cbBTC"),
    ],
    "arc": [   # Arc: USDC is the native gas token (v4 native currency = USDC); WUSDC wraps it
        ("0x3600000000000000000000000000000000000000", "usd", 1, "USDC"),
        ("0xe6b0a06c87a1422f966e8a31b5d1f3b3fa348c5a", "usd", 2, "WUSDC"),
        (NATIVE, "usd", 3, "USDC"),
    ],
}
# smallest quote amount for a trade to count (dust / rounding prints excluded), per anchor kind
MIN_QUOTE = {"usd": 1.0, "eth": 0.0005, "btc": 0.00002, "arb": 2.0}
# composite admission: a pool's minute close must sit within TOL of the cross-pool median
TOL = 0.05
# print sanity: a pool-minute whose average swap is larger than this (USD) is treated as a bogus print
MAX_AVG_SWAP_USD = 20e6
# temporal sanity: anchor minute rates must sit within ANCHOR_JUMP of their +-60-minute rolling median,
# token minute closes within a factor TOKEN_JUMP of their +-30-bar rolling median
ANCHOR_JUMP = 0.10
TOKEN_JUMP = 3.0
STABLE_KIND_JUMP = 1.25  # stocks and anchors: a minute close more than 25% off its rolling median is not plausible
# a composite minute needs at least this much USD turnover to set a price (dust prints are noise)
DUST_USD = {"stock": 10.0, "anchor": 10.0, "token": 1.0}
# stock tokens never trade at more than GLOBAL_BAND x away from their own whole-history median
GLOBAL_BAND = 5.0
# temporal sanity window (ms either side, by time rather than by bar count so sparse markets are judged fairly)
JUMP_WINDOW_MS = 6 * 3600 * 1000
# composite reference/wick admission (USD turnover per pool-minute)
REF_MIN_USD = 50
WICK_MIN_USD = 100
# chart-cache/tails 2026-10-23: serve+live wick bands (fraction of close). Prior 2*TOL=10% let
# single-pool prints draw ±10% minute tails on WETH/stocks and blow TickChart y-scale (Sol path is tighter).
WICK_BAND = {"usd": 0.01, "anchor": 0.02, "stock": 0.03, "token": 0.05}
# trade tape window kept in the DB (days)
TAPE_DAYS = 7
# markets with known-bad price sources: (chain, token) -> note. Flagged markets stay listed with quality='flagged'.
FLAGS = {
    ("arbitrum", "0x17fc002b466eec40dae837fc4be5c67993ddbd6f"):
        "Legacy FRAX: after the Frax migration its remaining Arbitrum pools are thin and inconsistent; daily closes "
        "diverge from the Chainlink FRAX/USD feed by >50% on 10% of days. Not a usable price.",
}
# stored resolutions and how far back each is kept (seconds; None = full history)
RETENTION = {60: 60 * 86400, 300: 180 * 86400, 900: 730 * 86400, 3600: None, 14400: None, 86400: None}


_SEQ = itertools.count()
atexit.register(shutil.rmtree, f"{ROOT}/tmp/{os.getpid()}", True)


def connect(path=":memory:", threads=4, mem="6GB", read_only=False):
    c = duckdb.connect(path, read_only=read_only)
    threads = int(os.environ.get("EM_THREADS", threads))  # lets a job run lighter next to another build
    c.execute(f"SET threads={threads}")
    c.execute(f"SET memory_limit='{mem}'")
    # DuckDB temp file names are fixed per database, so two connections must never share a directory
    # (the server runs live tails and analytics side by side in one process)
    tmp = f"{ROOT}/tmp/{os.getpid()}/{next(_SEQ)}"
    os.makedirs(tmp, exist_ok=True)
    weakref.finalize(c, shutil.rmtree, tmp, True)
    c.execute(f"SET temp_directory='{tmp}'")
    # spill cap (DuckDB default = 90% of the free disk; skew shares its disk with production Ticking, 2026-10-20)
    c.execute(f"SET max_temp_directory_size='{os.environ.get('EM_TMP_MAX', '15GB')}'")
    c.execute("SET preserve_insertion_order=false")
    c.execute("SET enable_progress_bar=false")
    if not os.environ.get("HOME"):  # systemd gives no HOME; extensions (sqlite) live under ~/.duckdb
        c.execute(f"SET home_directory='{pwd.getpwuid(os.getuid()).pw_dir}'")
    return c


def free_gb():
    s = os.statvfs("/")
    return s.f_bavail * s.f_frsize / 1e9


def guard(step, floor=100):
    g = free_gb()
    if g < floor:
        raise SystemExit(f"abort before {step}: only {g:.0f} GB free (< {floor} GB)")
    return g


# --- compact-served EVM chains (Option A, em-evm-rt-bringup): anchors + native flags for every new chain; a chain joins
# CHAINS only once its compact charts are built (join/enabled.json "charts"). COMPACT lists those: no analytics for them.
COMPACT = ()
try:
    import sys as _sys
    if "/srv/skew/em-realtime" not in _sys.path:
        _sys.path.append("/srv/skew/em-realtime")
    import evm_cfg as _evm_cfg
    for _c, _a in _evm_cfg.ANCHORS.items():
        ANCHORS.setdefault(_c, _a)
    for _c, _n in _evm_cfg.NATIVE_NOT_ETH.items():
        FLAGS.setdefault((_c, NATIVE), "native %s: em-charts names the native token ETH on this chain, so it is not an ETH price" % _n)
    COMPACT = tuple(_c for _c in _evm_cfg.charts_enabled() if _c not in CHAINS)
    CHAINS = CHAINS + COMPACT
except Exception:  # never break em-charts over the extension
    COMPACT = ()
