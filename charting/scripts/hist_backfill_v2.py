"""Backfill hot 1m/5m/1h bars + fills for the gap between deep build end and hot bar start (2026-10-24).
Replays existing stage parts (already decoded from follower chunks) through LiveTail's exact policy.
Does NOT touch state cursor/data_until_ms. Rollback: DELETE FROM bars/fills WHERE time in [lo,hi)."""
import glob, json, os, sys, time, sqlite3, shutil
sys.path.insert(0, "/srv/skew/em-charts"); os.chdir("/srv/skew/em-charts")
import live
from live import LiveTail, part_from
from hotstore import path

ch = sys.argv[1]; dry = "--dry" in sys.argv
class W:
    def __init__(s, ch, lo, hi):
        s.db = sqlite3.connect(path(ch), timeout=30, isolation_level=None)
        s.db.execute("PRAGMA busy_timeout=10000"); s.lo, s.hi = lo, hi; s.n = [0, 0]
    def write(s, fills, bars, pools, cursor, until):
        bars = [b for b in bars if s.lo < b[2] + (0) and b[2] < s.hi and (b[1] == 60 or (b[2] >= s.lo and b[2] + b[1] <= s.hi))]
        fills = [f for f in fills if s.lo < f[2] < s.hi]
        if dry: s.n[0] += len(bars); s.n[1] += len(fills); return
        c = s.db; c.execute("BEGIN IMMEDIATE")
        try:
            c.executemany("INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?,?,?,?)", [f[:9] for f in fills])
            c.executemany("INSERT OR IGNORE INTO fill_meta VALUES(?,?,?,?,?,?,?)", [(f[0],) + tuple(f[9:15]) for f in fills])
            c.executemany("INSERT OR IGNORE INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", bars)
            c.executemany("INSERT OR IGNORE INTO pools VALUES(?,?,?)", pools)
            c.execute("UPDATE state SET value = CAST(value AS INTEGER) + 1 WHERE key='data_version'")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK"); raise
        s.n[0] += len(bars); s.n[1] += len(fills)

def tip_age():
    c = sqlite3.connect(f"file:{path('arbitrum')}?mode=ro", uri=True, timeout=5)
    return time.time() - c.execute("SELECT max(time) FROM fills WHERE time > ?", (int(time.time()) - 3600,)).fetchone()[0]

t = LiveTail.__new__(LiveTail); t.chain = ch; t.log = print; t.dec = None
t.mem = None; t.deep_last = 0
t.hot = sqlite3.connect(f"file:{path(ch)}?mode=ro", uri=True)
hb = int(os.environ.get("HB") or t.hot.execute("SELECT min(time) FROM bars WHERE interval=60").fetchone()[0])
t.load(); t.mem.execute("DELETE FROM recent_pool_1m")
dm = int(t.mem.execute("SELECT max(time) FROM tok_live").fetchone()[0]) // 1000
print(ch, "deep_last", t.deep_last, "deep_until", dm, "hot_bar_start", hb, flush=True)
t.hot = W(ch, dm, hb)
endb = t.mem.execute(f"SELECT max(last_block) FROM read_parquet('data/block_minutes_{ch}.parquet') WHERE minute < {hb}").fetchone()[0]
parts = sorted([p for p in glob.glob(f"data/{ch}/stage/pool_1m/part=r*.parquet") if part_from(p) <= endb], key=part_from)
low = t.deep_last; done = 0; t0 = time.time()
for p in parts:
    if t.part_last_block(p) <= low: continue
    if shutil.disk_usage("/").free < 40e9: print("ABORT disk<40GB", flush=True); break
    while not dry and tip_age() > 60: print("pause tip_age", round(tip_age()), flush=True); time.sleep(10)
    t.ingest(p, low); low = max(low, t.part_last_block(p)); done += 1
    if done % 50 == 0: print(ch, done, "parts", p, "bars/fills", t.hot.n, f"{time.time()-t0:.0f}s", flush=True)
    if not dry: time.sleep(0.15)
print(ch, "DONE parts", done, "bars/fills", t.hot.n, f"{time.time()-t0:.0f}s", flush=True)
