"""Live tail: follower chunks -> hot store (SQLite, Ticking fills/bars DDL). The deep DuckDB stays read-only.

Each cycle (15 s):
  1. extend block times and decode swaps from new follower log chunks (tail_bars.decode_to -> stage parts);
  2. fold the new pool-minutes into an in-memory working set (seeded from the deep DB at start: anchors,
     markets, pool weights, the last 26 h of token/pool 1m bars and anchor rates);
  3. recompute the touched minutes with the deep build's policy (em_pool_reference_v2): anchor USD rates,
     token composites (dust floor, stock global band, temporal sanity, open = previous close), per-pool series,
     and the 5m / 1h buckets that contain them;
  4. write fills (+ fill_meta), bars (60 / 300 / 3600) and the cursor to hot/<chain>/history.sqlite3 in one
     transaction that bumps state.data_version.
On start, stage parts decoded after the deep build (and not yet in the hot store) are replayed first.
"""
import glob
import json
import os
import re
import threading
import time
import traceback

from block_minutes_live import extend
from common import (ANCHOR_JUMP, DUST_USD, GLOBAL_BAND, JUMP_WINDOW_MS, MAX_AVG_SWAP_USD, ROOT, STABLE_KIND_JUMP, TOKEN_JUMP, TOL,
                    WICK_BAND, WICK_MIN_USD, connect)
from hotstore import HotWriter
from tail_bars import chunk_range, decode_to, prepare, save_new_pools

RT = "/srv/skew/em-realtime/out"
KEEP_RECENT_S = 6 * 3600
KEEP_LIVE_MS = 26 * 3600 * 1000
PAUSE = f"{ROOT}/data/LIVE_PAUSED"
PART_RE = re.compile(r"part=([rt])(\d+)\.parquet$")


def part_from(p):
    m = PART_RE.search(p)
    return int(m.group(2)) if m else -1


class LiveTail(threading.Thread):
    def __init__(self, chain, every=15, log=print):  # was 60; markets-faster 2026-10-23
        super().__init__(daemon=True, name=f"live-{chain}")
        self.chain, self.every, self.log = chain, every, log
        self.dec = None
        self.state_p = f"{ROOT}/data/{chain}/tail_state.json"
        self.last_cycle = None
        self.errors = 0
        self.hot = HotWriter(chain)
        self.mem = None
        self.deep_last = 0

    # ---------- working set
    def load(self):
        m = connect(threads=2, mem="2GB")
        db = f"{ROOT}/data/{self.chain}/candles.duckdb"
        m.execute(f"ATTACH '{db}' AS d (READ_ONLY)")
        info = dict(m.execute("SELECT key, value FROM d.info").fetchall())
        self.deep_last = int(info.get("last_block", 0))
        until = int(info["data_until_ms"])
        m.execute("CREATE TABLE anchors AS SELECT * FROM d.anchors")
        m.execute("CREATE TABLE markets AS SELECT token, kind, ref_median FROM d.markets")
        m.execute("CREATE TABLE pools_w AS SELECT token, pool, w FROM d.pools")
        m.execute("CREATE TABLE market_pools AS SELECT token, pool FROM d.market_pools")
        m.execute(f"CREATE TABLE apx AS SELECT kind, minute, px, pools FROM d.anchor_px WHERE minute >= {until // 1000 - KEEP_RECENT_S}")
        m.execute(f"""CREATE TABLE tok_live AS SELECT token, time, open, high, low, close, volume, base_volume, count, pools
                      FROM d.token_bars WHERE period = 60 AND time >= {until - KEEP_LIVE_MS}""")
        m.execute(f"""CREATE TABLE pool_live AS SELECT pool, time, open, high, low, close, volume, base_volume, count
                      FROM d.pool_bars WHERE period = 60 AND time >= {until - KEEP_LIVE_MS}""")
        m.execute("DETACH d")
        m.execute("""CREATE TABLE recent_pool_1m(pool BLOB, base BLOB, "quote" BLOB, qkind VARCHAR, minute BIGINT,
                     o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE, bv DOUBLE, qv DOUBLE, n UINTEGER, fs BIGINT, ls BIGINT)""")
        parts = sorted(glob.glob(f"{ROOT}/data/{self.chain}/stage/pool_1m/part=[rt]*.parquet"), key=part_from)[-4:]
        if parts:
            lst = ",".join("'%s'" % p for p in parts)
            m.execute(f"""INSERT INTO recent_pool_1m SELECT pool, base, "quote", qkind, minute, o, h, l, c, bv, qv, n, fs, ls
                          FROM read_parquet([{lst}], union_by_name=true)
                          WHERE minute >= (SELECT max(minute) FROM read_parquet([{lst}], union_by_name=true)) - {KEEP_RECENT_S}""")
        self.mem = m

    def run(self):
        try:
            self.load()
            self.replay()
        except Exception:
            self.errors += 1
            self.log(f"live {self.chain} start failed:\n" + traceback.format_exc())
        while True:
            t = time.time()
            try:
                n = self.cycle()
                self.last_cycle = {"at": int(t), "parts": n, "seconds": round(time.time() - t, 2)}
            except Exception:
                self.errors += 1
                self.log(f"live {self.chain} cycle failed:\n" + traceback.format_exc())
            time.sleep(max(5, self.every - (time.time() - t)))

    def replay(self):
        """Stage parts decoded after the deep build that the hot store has not seen yet."""
        cursor = int(self.hot.state("cursor", "0") or 0)
        start = max(cursor, self.deep_last)
        parts = [p for p in glob.glob(f"{ROOT}/data/{self.chain}/stage/pool_1m/part=r*.parquet") if part_from(p) > start]
        for p in sorted(parts, key=part_from):
            self.ingest(p, start)
            start = max(start, self.part_last_block(p))
        if parts:
            self.log(f"live {self.chain}: replayed {len(parts)} stage parts into the hot store")

    def part_last_block(self, part):
        tp = part.replace("/pool_1m/", "/trades/")
        if os.path.exists(tp):
            r = self.mem.execute(f"SELECT max(bn) FROM read_parquet('{tp}')").fetchone()[0]
            if r is not None:
                return int(r)
        return part_from(part)

    def decoder(self):
        if self.dec is None:
            self.dec = connect(threads=2, mem="2GB")
            prepare(self.dec, self.chain)
        return self.dec

    def cycle(self):
        if os.path.exists(PAUSE):  # set while a full rebuild reads the stage (keeps stage and tail_state consistent)
            return 0
        if self.mem is None:
            self.load()
            self.replay()
        state = json.load(open(self.state_p))
        low = int(state["last_block"])
        known = extend(self.chain)
        files = []
        for f in glob.glob(f"{RT}/{self.chain}/logs/*.parquet"):
            a, b = chunk_range(f)
            mf = f"{RT}/{self.chain}/manifests/{a:010d}-{b:010d}.json"
            if b > low and b <= known and os.path.exists(mf) and json.load(open(mf)).get("status") == "OK":
                files.append(f)
        if not files:
            return 0
        files.sort(key=chunk_range)
        c = self.decoder()
        c.execute(f"CREATE OR REPLACE TABLE bm AS SELECT minute::BIGINT AS minute, first_block::UBIGINT AS first_block, "
                  f"last_block::UBIGINT AS last_block FROM read_parquet('{ROOT}/data/block_minutes_{self.chain}.parquet') "
                  f"WHERE minute > 0 ORDER BY first_block")
        dst = f"{ROOT}/data/{self.chain}/stage/pool_1m/part=r{max(chunk_range(files[0])[0], low + 1):010d}.parquet"
        s = decode_to(c, files, low, dst)
        save_new_pools(c, self.chain)
        state["last_block"] = max(low, chunk_range(files[-1])[1])
        json.dump(state, open(self.state_p + ".tmp", "w"))
        os.replace(self.state_p + ".tmp", self.state_p)
        self.ingest(dst, low)
        self.log(f"live {self.chain}: {len(files)} chunks through block {state['last_block']} {s}")
        return len(files)

    # ---------- one stage part -> working set -> hot store
    def ingest(self, part, min_block):
        m = self.mem
        m.execute(f"CREATE OR REPLACE TEMP TABLE newp AS SELECT pool, base, \"quote\", qkind, minute, o, h, l, c, bv, qv, n, fs, ls "
                  f"FROM read_parquet('{part}') WHERE fs // 100000 > {min_block}")
        if not m.execute("SELECT count(*) FROM newp").fetchone()[0]:
            return
        m.execute("INSERT INTO recent_pool_1m SELECT * FROM newp")
        m.execute("CREATE OR REPLACE TEMP TABLE touched AS SELECT DISTINCT minute FROM newp")
        # a pool-minute split across two decode batches: merge both halves in print order
        m.execute("""CREATE OR REPLACE TEMP TABLE merged AS
          SELECT pool, base, "quote", any_value(qkind) AS qkind, minute, arg_min(o, fs) AS o, max(h) AS h, min(l) AS l,
                 arg_max(c, ls) AS c, sum(bv) AS bv, sum(qv) AS qv, sum(n)::UINTEGER AS n, min(fs) AS fs, max(ls) AS ls
          FROM recent_pool_1m WHERE minute IN (SELECT minute FROM touched) GROUP BY pool, base, "quote", minute""")
        m.execute("DELETE FROM recent_pool_1m WHERE minute IN (SELECT minute FROM touched)")
        m.execute("INSERT INTO recent_pool_1m SELECT * FROM merged")
        self._recompute(m)
        fills, bars, pools = self._hot_rows(m, part, min_block)
        until = m.execute("SELECT max(minute) * 1000 FROM touched").fetchone()[0]
        self.hot.write(fills, bars, pools, self.part_last_block(part), int(until or 0))
        # bounded working set
        lo = m.execute("SELECT max(minute) FROM recent_pool_1m").fetchone()[0]
        m.execute(f"DELETE FROM recent_pool_1m WHERE minute < {lo} - {KEEP_RECENT_S}")
        m.execute(f"DELETE FROM apx WHERE minute < {lo} - {KEEP_RECENT_S}")
        m.execute(f"DELETE FROM tok_live WHERE time < {lo * 1000} - {KEEP_LIVE_MS}")
        m.execute(f"DELETE FROM pool_live WHERE time < {lo * 1000} - {KEEP_LIVE_MS}")

    def _recompute(self, m):
        m.execute(f"""CREATE OR REPLACE TEMP TABLE a_new AS
          WITH r AS (SELECT a.kind, p.minute, p.c, p.qv FROM recent_pool_1m p JOIN anchors a ON a.token = '0x' || lower(hex(p.base))
                     WHERE p.qkind = 'usd' AND a.kind <> 'usd' AND p.minute IN (SELECT minute FROM touched)
                       AND p.qv / p.n <= {MAX_AVG_SWAP_USD}),
          med AS (SELECT kind, minute, median(c) AS ref FROM r GROUP BY ALL),
          raw AS (SELECT r.kind, r.minute, sum(r.c * r.qv) / sum(r.qv) AS px, count(*)::SMALLINT AS pools
                  FROM r JOIN med USING (kind, minute) WHERE abs(r.c / med.ref - 1) <= {TOL} GROUP BY ALL),
          prior AS (SELECT kind, median(px) AS rm FROM apx
                    WHERE minute >= (SELECT min(minute) FROM touched) - 3600 AND minute < (SELECT min(minute) FROM touched) GROUP BY kind)
          SELECT raw.kind, raw.minute, raw.px, raw.pools FROM raw LEFT JOIN prior USING (kind)
          WHERE prior.rm IS NULL OR abs(raw.px / prior.rm - 1) <= {ANCHOR_JUMP}""")
        m.execute("DELETE FROM apx USING a_new WHERE apx.kind = a_new.kind AND apx.minute = a_new.minute")
        m.execute("INSERT INTO apx SELECT * FROM a_new")
        m.execute("""CREATE OR REPLACE TEMP TABLE qrate AS SELECT kind, minute, px FROM apx UNION ALL SELECT 'usd', 0, 1.0
                     ORDER BY kind, minute""")
        m.execute(f"""CREATE OR REPLACE TEMP TABLE pbu AS
          SELECT '0x' || lower(hex(p.base)) AS token, '0x' || lower(hex(p.pool)) AS pool, p.minute,
                 p.o * q.px AS o, p.h * q.px AS h, p.l * q.px AS l, p.c * q.px AS c, p.bv, p.qv * q.px AS qv_usd, p.n, p.fs, p.ls
          FROM recent_pool_1m p ASOF JOIN qrate q ON q.kind = p.qkind AND p.minute >= q.minute
          WHERE p.minute IN (SELECT minute FROM touched) AND (p.qkind = 'usd' OR p.minute - q.minute <= 21600)
            AND p.qv * q.px / p.n <= {MAX_AVG_SWAP_USD}""")
        m.execute(f"""CREATE OR REPLACE TEMP TABLE t0 AS
          WITH r AS (SELECT pbu.* FROM pbu SEMI JOIN markets mk ON mk.token = pbu.token),
          rw AS (SELECT r.*, coalesce(p.w, 0.001) AS w FROM r LEFT JOIN pools_w p ON p.pool = r.pool AND p.token = r.token),
          med AS (SELECT token, minute, arg_max(c, w) AS ref, arg_max(pool, w) AS ref_pool, count(*) AS np FROM rw GROUP BY ALL),
          ad AS (SELECT rw.*, med.ref, (med.np = 1 OR rw.qv_usd >= {WICK_MIN_USD} OR rw.pool = med.ref_pool) AS wick_ok
                 FROM rw JOIN med USING (token, minute) WHERE med.np = 1 OR abs(rw.c / med.ref - 1) <= {TOL})
          SELECT token, minute * 1000 AS time,
                 arg_min(CASE WHEN abs(o / ref - 1) <= {TOL} THEN o ELSE c END, fs) AS open0,  -- tails 2026-10-23: was 2*TOL
                 any_value(ref) AS close,
                 max(CASE WHEN wick_ok THEN greatest(c, CASE WHEN h <= ref * {1 + TOL} THEN h ELSE c END,
                                                     CASE WHEN abs(o / ref - 1) <= {TOL} THEN o ELSE c END) ELSE ref END) AS high,
                 min(CASE WHEN wick_ok THEN least(c, CASE WHEN l >= ref * {1 - TOL} THEN l ELSE c END,
                                                  CASE WHEN abs(o / ref - 1) <= {TOL} THEN o ELSE c END) ELSE ref END) AS low,
                 sum(qv_usd) AS volume, sum(bv) AS base_volume, sum(n)::INTEGER AS count, count(*)::SMALLINT AS pools,
                 min(fs) AS fs, max(ls) AS ls
          FROM ad GROUP BY token, minute""")
        m.execute(f"""DELETE FROM t0 USING markets mk WHERE mk.token = t0.token AND t0.volume <
                      CASE mk.kind WHEN 'stock' THEN {DUST_USD['stock']} WHEN 'anchor' THEN {DUST_USD['anchor']} ELSE {DUST_USD['token']} END""")
        m.execute(f"""DELETE FROM t0 USING markets mk WHERE mk.token = t0.token AND mk.kind = 'stock' AND mk.ref_median IS NOT NULL
                      AND (t0.close > mk.ref_median * {GLOBAL_BAND} OR t0.close < mk.ref_median / {GLOBAL_BAND})""")
        m.execute(f"""DELETE FROM t0 USING (
                        SELECT t0.token, t0.time FROM t0 JOIN (
                          SELECT token, median(close) AS rm FROM (
                            SELECT b.token, b.close, b.time, row_number() OVER (PARTITION BY b.token ORDER BY b.time DESC) AS k
                            FROM tok_live b WHERE b.token IN (SELECT token FROM t0) AND b.time < (SELECT min(time) FROM t0))
                          WHERE k <= 30 OR time >= (SELECT min(time) FROM t0) - {JUMP_WINDOW_MS}
                          GROUP BY token) h USING (token)
                        JOIN (SELECT token, CASE WHEN kind = 'token' THEN {TOKEN_JUMP} ELSE {STABLE_KIND_JUMP} END AS f FROM markets) ff
                          ON ff.token = t0.token
                        WHERE t0.close > h.rm * ff.f OR t0.close < h.rm / ff.f) j
                      WHERE t0.token = j.token AND t0.time = j.time""")
        # tails 2026-10-23: kind wick band + no prev-close open stitch (Sol-like; killed gap tails)
        m.execute(f"""UPDATE t0 SET
            high = least(high, close * (1 + CASE mk.kind
                WHEN 'stock' THEN {WICK_BAND['stock']} WHEN 'anchor' THEN
                    CASE WHEN close BETWEEN 0.5 AND 1.5 THEN {WICK_BAND['usd']} ELSE {WICK_BAND['anchor']} END
                ELSE {WICK_BAND['token']} END)),
            low = greatest(low, close * (1 - CASE mk.kind
                WHEN 'stock' THEN {WICK_BAND['stock']} WHEN 'anchor' THEN
                    CASE WHEN close BETWEEN 0.5 AND 1.5 THEN {WICK_BAND['usd']} ELSE {WICK_BAND['anchor']} END
                ELSE {WICK_BAND['token']} END)),
            open0 = least(greatest(open0, close * (1 - CASE mk.kind
                WHEN 'stock' THEN {WICK_BAND['stock']} WHEN 'anchor' THEN
                    CASE WHEN close BETWEEN 0.5 AND 1.5 THEN {WICK_BAND['usd']} ELSE {WICK_BAND['anchor']} END
                ELSE {WICK_BAND['token']} END)),
                close * (1 + CASE mk.kind
                WHEN 'stock' THEN {WICK_BAND['stock']} WHEN 'anchor' THEN
                    CASE WHEN close BETWEEN 0.5 AND 1.5 THEN {WICK_BAND['usd']} ELSE {WICK_BAND['anchor']} END
                ELSE {WICK_BAND['token']} END))
          FROM markets mk WHERE mk.token = t0.token""")
        m.execute("""CREATE OR REPLACE TEMP TABLE t1 AS
          SELECT token, time, open0 AS open,
                 greatest(high, open0, close) AS high, least(low, open0, close) AS low, close,
                 volume, base_volume, count, pools, fs, ls FROM t0""")
        m.execute("""CREATE OR REPLACE TEMP TABLE p1 AS
          WITH p AS (SELECT pbu.* FROM pbu SEMI JOIN market_pools mp ON mp.pool = pbu.pool AND mp.token = pbu.token),
          j AS (SELECT p.*, t1.close AS tc FROM p LEFT JOIN t1 ON t1.token = p.token AND t1.time = p.minute * 1000)
          SELECT pool, minute * 1000 AS time,
                 CASE WHEN o BETWEEN least(c, coalesce(tc, c)) / 1.25 AND greatest(c, coalesce(tc, c)) * 1.25 THEN o ELSE c END AS open,
                 least(h, greatest(c, coalesce(tc, c)) * 1.05) AS high, greatest(l, least(c, coalesce(tc, c)) / 1.05) AS low,  -- tails 2026-10-23 was 1.25
                 c AS close, qv_usd AS volume, bv AS base_volume, n::INTEGER AS count, fs, ls
          FROM j WHERE tc IS NULL OR c BETWEEN tc / 2 AND tc * 2""")
        m.execute("UPDATE p1 SET high = greatest(high, open, close), low = least(low, open, close)")
        m.execute("DELETE FROM tok_live USING t1 WHERE tok_live.token = t1.token AND tok_live.time = t1.time")
        m.execute("INSERT INTO tok_live SELECT token, time, open, high, low, close, volume, base_volume, count, pools FROM t1")
        m.execute("DELETE FROM pool_live USING p1 WHERE pool_live.pool = p1.pool AND pool_live.time = p1.time")
        m.execute("INSERT INTO pool_live SELECT pool, time, open, high, low, close, volume, base_volume, count FROM p1")

    def _hot_rows(self, m, part, min_block):
        bars = []
        for key, src, live, prefix in (("token", "t1", "tok_live", "token:"), ("pool", "p1", "pool_live", "")):
            for k, t, o, h, l, cl, v, bv, n, fs, ls in m.execute(
                    f"SELECT {key}, time, open, high, low, close, volume, base_volume, count, fs, ls FROM {src}").fetchall():
                bars.append((prefix + k, 60, t // 1000, o, h, l, cl, bv, v, n, "%020d:%010d:%06d:" % (fs // 100000, 0, fs % 100000),
                             "%020d:%010d:%06d:" % (ls // 100000, 0, ls % 100000)))
            for period in (300, 3600):
                ms = period * 1000
                for k, t, o, h, l, cl, v, bv, n in m.execute(f"""
                    WITH bk AS (SELECT DISTINCT {key}, time // {ms} * {ms} AS b FROM {src})
                    SELECT x.{key}, bk.b, arg_min(x.open, x.time), max(x.high), min(x.low), arg_max(x.close, x.time), sum(x.volume),
                           sum(x.base_volume), sum(x.count)
                    FROM {live} x JOIN bk ON bk.{key} = x.{key} AND x.time >= bk.b AND x.time < bk.b + {ms}
                    GROUP BY x.{key}, bk.b""").fetchall():
                    bars.append((prefix + k, period, t // 1000, o, h, l, cl, bv, v, n, "", ""))
        fills, pools = [], []
        tp = part.replace("/pool_1m/", "/trades/")
        if os.path.exists(tp):
            cols = {r[0] for r in m.execute(f"DESCRIBE SELECT * FROM read_parquet('{tp}')").fetchall()}
            tsx = "u.ts" if "ts" in cols else "NULL::BIGINT"  # parts decoded before block times were carried
            rows = m.execute(f"""
              WITH u AS (SELECT t.*, q.px AS rate FROM read_parquet('{tp}', union_by_name=true) t
                         ASOF JOIN qrate q ON q.kind = t.qkind AND t.minute >= q.minute
                         WHERE t.bn > {min_block} AND (t.qkind = 'usd' OR t.minute - q.minute <= 21600))
              SELECT lower(hex(u.tx_hash)), u.log_index, '0x' || lower(hex(u.pool)), coalesce({tsx}, u.minute), u.bn, u.tx_index,
                     u.px * u.rate, u.base_amt, u.quote_amt * u.rate, '0x' || lower(hex(u.base)), u.side,
                     '0x' || lower(hex(u.trader)), coalesce(abs(u.px * u.rate / b.close - 1) <= {2 * TOL}, false),
                     '0x' || lower(hex(u."quote"))
              FROM u JOIN markets mk ON mk.token = '0x' || lower(hex(u.base))
              LEFT JOIN tok_live b ON b.token = mk.token AND b.time = u.minute * 1000
              WHERE u.quote_amt * u.rate <= {MAX_AVG_SWAP_USD} AND u.tx_hash IS NOT NULL""").fetchall()
            seen = set()
            for txh, li, pool, ts, bn, txi, price, bv, qv, token, side, trader, el, quote in rows:
                ident = f"0x{txh}:{li}"
                fills.append((ident, pool, int(ts), int(bn), int(txi), int(li), price, bv, qv, token, side, trader, "0x" + txh, qv,
                              1 if el else 0))
                if pool not in seen:
                    seen.add(pool)
                    pools.append((pool, token, quote))
        return fills, bars, pools
