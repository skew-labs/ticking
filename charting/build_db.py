"""Stage 2: pool-minute bars (quote units) -> USD token composites, per-pool USD series, rollups -> candles DB.

USD conversion: 'usd' anchors at $1; eth/btc/arb anchors use a per-minute composite from their own
USD-quoted pools (as-of join, at most 6 h old), so every quote leg is priced with a time-matched rate.
Token composite per minute (policy em_pool_reference_v2):
  ref   = USD close of the most established pool printing in that minute (most active minutes over its life;
          robust when a token has many thin side pools)
  admit = pools whose close is within TOL of ref (all pools when only one traded)
  open  = earliest admitted print, close = latest admitted print,
  high/low = admitted wicks inside 2*TOL of ref (otherwise that pool's open/close),
  volume = admitted USD turnover, baseVolume = admitted base amount, count = admitted swaps.
usage: build_db.py <chain> [--min-usd N]
"""
import csv
import glob
import json
import os
import sys
import time

from common import (ANCHOR_JUMP, DUST_USD, EM, FLAGS, GLOBAL_BAND, JUMP_WINDOW_MS, MAX_AVG_SWAP_USD, REF_MIN_USD, RETENTION, ROOT, STABLE_KIND_JUMP, TAPE_DAYS,
                    TOKEN_JUMP, TOL, WICK_MIN_USD, connect, guard)

MIN_USD = {"robinhood": 1_000_000, "arbitrum": 4_000_000, "arc": 500_000}  # 2026-10-23: Arb Markets ≥500 non-anchor tokens (was 5_000_000 → 490 token+10 anchor)
POOLS_PER_TOKEN = 3
POOL_SERIES_MIN_X = 10  # plain tokens get per-pool series only above 10x the market threshold


def main(chain, min_usd):
    t0 = time.time()
    guard(f"{chain} build_db")
    stage = f"{ROOT}/data/{chain}/stage/pool_1m/*.parquet"
    db = f"{ROOT}/data/{chain}/candles.duckdb"
    tmp = db + ".building"
    for p in (tmp, tmp + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    c = connect(tmp, threads=4, mem="7GB")
    c.execute(f"CREATE TEMP VIEW pb AS SELECT * FROM read_parquet('{stage}')")
    c.execute(f"""CREATE TABLE meta AS SELECT lower(token) AS token, any_value(symbol) AS symbol, any_value(name) AS name,
                  any_value(decimals) AS decimals FROM read_parquet('{EM}/econ/{chain}/token_meta.parquet') GROUP BY 1""")
    from common import ANCHORS, NATIVE
    c.execute("CREATE TABLE anchors(token VARCHAR, kind VARCHAR, priority INTEGER, symbol VARCHAR)")
    c.executemany("INSERT INTO anchors VALUES (?, ?, ?, ?)", ANCHORS[chain])
    c.execute(f"INSERT INTO meta SELECT '{NATIVE}', 'ETH', 'Ether (native)', 18 WHERE NOT EXISTS (SELECT 1 FROM meta WHERE token = '{NATIVE}')")

    # 1. anchor USD minute series (eth / btc / arb) from USD-quoted pools
    c.execute(f"""CREATE TABLE anchor_px AS
      WITH r AS (SELECT a.kind, pb.minute, pb.pool, pb.c, pb.qv FROM pb JOIN anchors a ON a.token = '0x' || lower(hex(pb.base))
                 WHERE pb.qkind = 'usd' AND a.kind <> 'usd' AND pb.qv / pb.n <= {MAX_AVG_SWAP_USD}),
      med AS (SELECT kind, minute, median(c) AS ref FROM r GROUP BY ALL),
      raw AS (SELECT r.kind, r.minute, sum(r.c * r.qv) / sum(r.qv) AS px, count(*)::SMALLINT AS pools
              FROM r JOIN med USING (kind, minute) WHERE abs(r.c / med.ref - 1) <= {TOL} GROUP BY ALL),
      f AS (SELECT *, median(px) OVER (PARTITION BY kind ORDER BY minute ROWS BETWEEN 60 PRECEDING AND 60 FOLLOWING) AS rm FROM raw)
      SELECT kind, minute, px, pools FROM f WHERE abs(px / rm - 1) <= {ANCHOR_JUMP} ORDER BY kind, minute""")
    print("anchor_px", c.execute("SELECT kind, count(*), min(minute), max(minute), arg_max(px, minute) FROM anchor_px GROUP BY 1").fetchall(), flush=True)

    # 2. every pool-minute converted to USD (quote rate as-of the same minute)
    c.execute("""CREATE TEMP TABLE qrate AS SELECT kind, minute, px FROM anchor_px
                 UNION ALL SELECT 'usd', 0, 1.0 ORDER BY kind, minute""")
    c.execute(f"""CREATE TEMP VIEW pbu AS
      SELECT pb.pool, pb.base, pb.quote, pb.qkind, pb.minute, pb.o * q.px AS o, pb.h * q.px AS h, pb.l * q.px AS l,
             pb.c * q.px AS c, pb.bv, pb.qv * q.px AS qv_usd, pb.n, pb.fs, pb.ls
      FROM pb ASOF JOIN qrate q ON q.kind = pb.qkind AND pb.minute >= q.minute
      WHERE (pb.qkind = 'usd' OR pb.minute - q.minute <= 21600) AND pb.qv * q.px / pb.n <= {MAX_AVG_SWAP_USD}""")

    # 3. candidate scope on raw turnover (stocks and anchors always)
    stocks = {}
    sp = f"{EM}/econ/{chain}/focus_stock_tokens.csv"
    if os.path.exists(sp):
        for r in csv.DictReader(open(sp)):
            stocks[r["token"].lower()] = r
    c.execute("CREATE TEMP TABLE stock(token VARCHAR, ui_multiplier DOUBLE)")
    if stocks:   # chains without stock tokens (arc)
      c.executemany("INSERT INTO stock VALUES (?, ?)",
                  [(t, float(r["ui_multiplier"]) if r.get("ui_multiplier") else None) for t, r in stocks.items()])
    c.execute(f"""CREATE TEMP TABLE cand AS
      SELECT '0x' || lower(hex(base)) AS token, base AS token_b FROM pbu GROUP BY base
      HAVING sum(qv_usd) >= {min_usd}
         OR '0x' || lower(hex(base)) IN (SELECT token FROM stock UNION ALL SELECT token FROM anchors)""")
    c.execute("""ALTER TABLE cand ADD COLUMN kind VARCHAR;
                 UPDATE cand SET kind = CASE WHEN token IN (SELECT token FROM stock) THEN 'stock'
                                             WHEN token IN (SELECT token FROM anchors) THEN 'anchor' ELSE 'token' END""")
    # materialize the USD pool-minutes of candidate tokens once, split into token buckets so every heavy step
    # (admission, composite, window filters) works on 1/K of the data and its temp spill stays small
    nrows = c.execute("SELECT count(*) FROM pb").fetchone()[0]
    K = max(1, min(32, nrows // 12_000_000))
    work = f"{ROOT}/data/{chain}/pbu.building"
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    guard(f"{chain} pbu materialize")
    c.execute(f"""COPY (SELECT cand.token, pbu.* EXCLUDE (base), (hash(cand.token) % {K})::INTEGER AS bk
                        FROM pbu JOIN cand ON cand.token_b = pbu.base)
                  TO '{work}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (bk))""")
    print("pbu buckets", K, "rows", nrows, round(time.time() - t0), "s", flush=True)
    c.execute("""CREATE TABLE pools(token VARCHAR, pool VARCHAR, "quote" VARCHAR, qkind VARCHAR, usd_volume DOUBLE, swaps BIGINT,
                 minutes BIGINT, w DOUBLE, first_minute BIGINT, last_minute BIGINT)""")
    c.execute("""CREATE TEMP TABLE tok1m(token VARCHAR, time BIGINT, open DOUBLE, close DOUBLE, high DOUBLE, low DOUBLE, volume DOUBLE,
                 base_volume DOUBLE, count INTEGER, pools SMALLINT)""")
    c.execute("CREATE TEMP TABLE refmed(token VARCHAR, m DOUBLE)")
    dropped = {"dust": 0, "band": 0, "jump": 0}
    for bk in range(K):
        files = glob.glob(f"{work}/bk={bk}/*.parquet")
        if not files:
            continue
        guard(f"{chain} composite bucket {bk}")
        lst = ",".join("'%s'" % f for f in files)
        # admitted pool-minutes. Reference = close of the most established pool printing that minute, where
        # "established" counts the pool's minutes with at least REF_MIN_USD of turnover (bot pools that print dust
        # every minute do not qualify). Pools within TOL are admitted. Wicks only come from prints that carry
        # turnover (>= WICK_MIN_USD), the reference pool, or a sole print.
        c.execute(f"""CREATE OR REPLACE TEMP TABLE ad AS
          WITH r AS (SELECT * FROM read_parquet([{lst}])),
          pw AS (SELECT pool, count(*) FILTER (WHERE qv_usd >= {REF_MIN_USD}) + 0.001 * count(*) AS w FROM r GROUP BY pool),
          rw AS (SELECT r.*, pw.w FROM r JOIN pw USING (pool)),
          med AS (SELECT token, minute, arg_max(c, w) AS ref, arg_max(pool, w) AS ref_pool, count(*) AS np FROM rw GROUP BY ALL)
          SELECT rw.token, rw.pool, rw."quote", rw.qkind, rw.minute, rw.o, rw.h, rw.l, rw.c, rw.bv, rw.qv_usd, rw.n, rw.fs, rw.ls,
                 rw.w, med.ref, (med.np = 1 OR rw.qv_usd >= {WICK_MIN_USD} OR rw.pool = med.ref_pool) AS wick_ok
          FROM rw JOIN med USING (token, minute) WHERE med.np = 1 OR abs(rw.c / med.ref - 1) <= {TOL}""")
        c.execute("""INSERT INTO pools
          SELECT token, '0x' || lower(hex(pool)), '0x' || lower(hex("quote")), any_value(qkind), sum(qv_usd), sum(n)::BIGINT,
                 count(*), max(w), min(minute), max(minute)
          FROM ad GROUP BY token, pool, "quote" """)
        # token composite 1m: close = reference; open/high/low inputs held inside the 2*TOL band around it
        c.execute(f"""CREATE OR REPLACE TEMP TABLE t0 AS
          SELECT ad.token, minute * 1000 AS time,
                 arg_min(CASE WHEN abs(o / ref - 1) <= {2 * TOL} THEN o ELSE c END, fs) AS open0,
                 any_value(ref) AS close,
                 max(CASE WHEN wick_ok THEN greatest(c, CASE WHEN h <= ref * {1 + 2 * TOL} THEN h ELSE c END,
                                                     CASE WHEN abs(o / ref - 1) <= {2 * TOL} THEN o ELSE c END) ELSE ref END) AS high,
                 min(CASE WHEN wick_ok THEN least(c, CASE WHEN l >= ref * {1 - 2 * TOL} THEN l ELSE c END,
                                                  CASE WHEN abs(o / ref - 1) <= {2 * TOL} THEN o ELSE c END) ELSE ref END) AS low,
                 sum(qv_usd) AS volume, sum(bv) AS base_volume, sum(n)::INTEGER AS count, count(*)::SMALLINT AS pools
          FROM ad GROUP BY ad.token, minute""")
        c.execute("DROP TABLE ad")
        n = c.execute("SELECT count(*) FROM t0").fetchone()[0]
        # dust: a minute below the kind's turnover floor does not set a price
        c.execute(f"""DELETE FROM t0 USING cand WHERE cand.token = t0.token AND t0.volume <
                      CASE cand.kind WHEN 'stock' THEN {DUST_USD['stock']} WHEN 'anchor' THEN {DUST_USD['anchor']} ELSE {DUST_USD['token']} END""")
        n1 = c.execute("SELECT count(*) FROM t0").fetchone()[0]
        # global band for stock tokens (launch-day prints from mis-scaled pools sit 100x+ away). Not for anchors:
        # ARB legitimately spans 35x over its history
        c.execute("INSERT INTO refmed SELECT token, median(close) FROM t0 GROUP BY token")
        c.execute(f"""DELETE FROM t0 USING cand, refmed WHERE cand.token = t0.token AND refmed.token = t0.token
                      AND cand.kind = 'stock' AND (t0.close > refmed.m * {GLOBAL_BAND} OR t0.close < refmed.m / {GLOBAL_BAND})""")
        n2 = c.execute("SELECT count(*) FROM t0").fetchone()[0]
        # temporal sanity: drop minutes whose close leaves, by more than the kind's factor, either the median of the
        # surrounding +-30 bars (isolated prints in sparse markets) or of the surrounding +-6 h (runs in dense ones)
        c.execute(f"""DELETE FROM t0 USING (SELECT token, time FROM (
                        SELECT t.token, t.time, t.close, CASE WHEN cand.kind = 'token' THEN {TOKEN_JUMP} ELSE {STABLE_KIND_JUMP} END AS f,
                               median(t.close) OVER (PARTITION BY t.token ORDER BY t.time ROWS BETWEEN 30 PRECEDING AND 30 FOLLOWING) AS rb,
                               median(t.close) OVER (PARTITION BY t.token ORDER BY t.time
                                                     RANGE BETWEEN {JUMP_WINDOW_MS} PRECEDING AND {JUMP_WINDOW_MS} FOLLOWING) AS rt
                        FROM t0 t JOIN cand ON cand.token = t.token)
                      WHERE close > rb * f OR close < rb / f OR close > rt * f OR close < rt / f) j
                      WHERE t0.token = j.token AND t0.time = j.time""")
        n3 = c.execute("SELECT count(*) FROM t0").fetchone()[0]
        dropped["dust"] += n - n1
        dropped["band"] += n1 - n2
        dropped["jump"] += n2 - n3
        # open continues the previous minute's close
        c.execute("""INSERT INTO tok1m
          SELECT token, time, CASE WHEN lag(time) OVER w = time - 60000 THEN lag(close) OVER w ELSE open0 END AS open,
                 close, high, low, volume, base_volume, count, pools
          FROM t0 WINDOW w AS (PARTITION BY token ORDER BY time)""")
        c.execute("DROP TABLE t0")
        print("bucket", bk, "minutes", n3, round(time.time() - t0), "s", flush=True)
    c.execute("UPDATE tok1m SET high = greatest(high, open, close), low = least(low, open, close)")
    print("dropped", dropped, flush=True)

    c.execute(f"""CREATE TABLE markets AS
      WITH tok AS (SELECT token, sum(usd_volume) AS usd_volume, sum(swaps) AS swaps, count(*) AS n_pools,
                          min(first_minute) AS first_minute, max(last_minute) AS last_minute FROM pools GROUP BY token)
      SELECT '{chain}:' || tok.token AS id, '{chain}' AS chain, tok.token, coalesce(m.symbol, left(tok.token, 10)) AS symbol,
             m.name, m.decimals,
             CASE WHEN s.token IS NOT NULL THEN 'stock' WHEN a.token IS NOT NULL THEN 'anchor' ELSE 'token' END AS kind,
             s.ui_multiplier, tok.usd_volume, tok.swaps, tok.n_pools, tok.first_minute, tok.last_minute, rm.m AS ref_median
      FROM tok LEFT JOIN meta m ON m.token = tok.token LEFT JOIN stock s ON s.token = tok.token
      LEFT JOIN anchors a ON a.token = tok.token LEFT JOIN refmed rm ON rm.token = tok.token
      WHERE tok.usd_volume >= {min_usd} OR s.token IS NOT NULL OR a.token IS NOT NULL""")
    print("markets", c.execute("SELECT kind, count(*), round(sum(usd_volume)/1e9, 2) FROM markets GROUP BY 1 ORDER BY 1").fetchall(), flush=True)
    c.execute("DELETE FROM tok1m WHERE token NOT IN (SELECT token FROM markets)")
    c.execute("DELETE FROM pools WHERE token NOT IN (SELECT token FROM markets)")
    print("tok1m", c.execute("SELECT count(*), count(DISTINCT token) FROM tok1m").fetchone(), round(time.time() - t0), "s", flush=True)
    c.execute(f"""CREATE TEMP TABLE top_pools AS
      SELECT * FROM (SELECT p.*, row_number() OVER (PARTITION BY p.token ORDER BY p.usd_volume DESC) AS rk
                     FROM pools p JOIN markets mk ON mk.token = p.token
                     WHERE mk.kind <> 'token' OR mk.usd_volume >= {POOL_SERIES_MIN_X} * {min_usd})
      WHERE rk <= {POOLS_PER_TOKEN}""")

    # 5. per-pool USD 1m for the top pools of each market: the pool's own prints, kept to minutes whose close is
    #    within 2x of the token composite and with o/h/l held to 1.25x around close/composite
    c.execute(f"""CREATE TEMP TABLE pool1m AS
      WITH p AS (SELECT '0x' || lower(hex(r.pool)) AS pool, r.token, r.minute * 1000 AS time, r.o, r.h, r.l, r.c, r.qv_usd, r.bv, r.n
                 FROM read_parquet('{work}/*/*.parquet') r
                 SEMI JOIN top_pools tp ON tp.pool = '0x' || lower(hex(r.pool)) AND tp.token = r.token),
      j AS (SELECT p.*, t.close AS tc FROM p LEFT JOIN tok1m t USING (token, time))
      SELECT pool, time,
             CASE WHEN o BETWEEN least(c, coalesce(tc, c)) / 1.25 AND greatest(c, coalesce(tc, c)) * 1.25 THEN o ELSE c END AS open,
             least(h, greatest(c, coalesce(tc, c)) * 1.25) AS high, greatest(l, least(c, coalesce(tc, c)) / 1.25) AS low,
             c AS close, qv_usd AS volume, bv AS base_volume, n::INTEGER AS count
      FROM j WHERE tc IS NULL OR c BETWEEN tc / 2 AND tc * 2""")
    c.execute("UPDATE pool1m SET high = greatest(high, open, close), low = least(low, open, close)")
    shutil.rmtree(work, ignore_errors=True)

    # 6. rollups + retention -> final ordered tables
    now_ms = int(c.execute("SELECT max(time) FROM tok1m").fetchone()[0])
    c.execute("""CREATE TABLE token_bars(token VARCHAR, period INTEGER, time BIGINT, open DOUBLE, high DOUBLE, low DOUBLE,
                 close DOUBLE, volume DOUBLE, base_volume DOUBLE, count INTEGER, pools SMALLINT)""")
    c.execute("""CREATE TABLE pool_bars(pool VARCHAR, period INTEGER, time BIGINT, open DOUBLE, high DOUBLE, low DOUBLE,
                 close DOUBLE, volume DOUBLE, base_volume DOUBLE, count INTEGER)""")
    for period, keep in sorted(RETENTION.items()):
        since = 0 if keep is None else now_ms - keep * 1000
        ms = period * 1000
        for src, dst, key, extra in (("tok1m", "token_bars", "token", ", max(pools)::SMALLINT"),
                                     ("pool1m", "pool_bars", "pool", "")):
            c.execute(f"""INSERT INTO {dst}
              SELECT {key}, {period}, time // {ms} * {ms} AS t, arg_min(open, time), max(high), min(low), arg_max(close, time),
                     sum(volume), sum(base_volume), sum(count)::INTEGER{extra}
              FROM {src} WHERE time >= {since} // {ms} * {ms} GROUP BY {key}, t ORDER BY {key}, t""")
        print("period", period, c.execute(f"SELECT count(*) FROM token_bars WHERE period = {period}").fetchone()[0],
              c.execute(f"SELECT count(*) FROM pool_bars WHERE period = {period}").fetchone()[0], round(time.time() - t0), "s", flush=True)

    # 7. market summary fields
    c.execute("""CREATE TABLE market_pools AS
      SELECT tp.token, tp.pool, tp."quote", tp.qkind, coalesce(m.symbol, left(tp."quote", 10)) AS quote_symbol, tp.usd_volume, tp.swaps, tp.rk
      FROM top_pools tp LEFT JOIN meta m ON m.token = tp."quote" """)
    c.execute("""ALTER TABLE markets ADD COLUMN last_price DOUBLE; ALTER TABLE markets ADD COLUMN last_time BIGINT;
                 ALTER TABLE markets ADD COLUMN change_24h DOUBLE; ALTER TABLE markets ADD COLUMN volume_24h DOUBLE""")
    c.execute(f"""UPDATE markets SET last_price = s.close, last_time = s.time FROM
                 (SELECT token, arg_max(close, time) AS close, max(time) AS time FROM token_bars WHERE period = 3600 GROUP BY token) s
                 WHERE s.token = markets.token""")
    c.execute(f"""UPDATE markets SET change_24h = s.chg, volume_24h = s.vol FROM
                 (SELECT b.token, arg_max(b.close, b.time) / arg_min(b.open, b.time) - 1 AS chg, sum(b.volume) AS vol
                  FROM token_bars b JOIN markets m ON m.token = b.token
                  WHERE b.period = 3600 AND b.time > m.last_time - 86400000 GROUP BY b.token) s WHERE s.token = markets.token""")
    # quality flags (known-bad price sources stay listed but are marked)
    c.execute("ALTER TABLE markets ADD COLUMN quality VARCHAR DEFAULT 'ok'; ALTER TABLE markets ADD COLUMN quality_note VARCHAR")
    for (fch, ftok), note in FLAGS.items():
        if fch == chain:
            c.execute("UPDATE markets SET quality = 'flagged', quality_note = ? WHERE token = ?", [note, ftok])

    # 8. trade tape: the last TAPE_DAYS of priced swaps for served markets (USD at the same-minute quote rate)
    tglob = f"{ROOT}/data/{chain}/stage/trades/*.parquet"
    c.execute("""CREATE TABLE tape(token VARCHAR, pool VARCHAR, time BIGINT, bn UBIGINT, tx_index UINTEGER, log_index UINTEGER,
                 tx_hash BLOB, trader BLOB, price DOUBLE, base_volume DOUBLE, usd DOUBLE, side VARCHAR, eligible BOOLEAN)""")
    if glob.glob(tglob):
        since = now_ms // 1000 - TAPE_DAYS * 86400
        c.execute(f"""INSERT INTO tape
          WITH t AS (SELECT * FROM read_parquet('{tglob}', union_by_name=true) WHERE minute >= {since}),
          u AS (SELECT t.*, q.px AS rate FROM t ASOF JOIN qrate q ON q.kind = t.qkind AND t.minute >= q.minute
                WHERE t.qkind = 'usd' OR t.minute - q.minute <= 21600)
          SELECT '0x' || lower(hex(u.base)) AS token, '0x' || lower(hex(u.pool)) AS pool, coalesce(u.ts, u.minute), u.bn, u.tx_index, u.log_index,
                 u.tx_hash, u.trader, u.px * u.rate, u.base_amt, u.quote_amt * u.rate, u.side,
                 coalesce(abs(u.px * u.rate / b.close - 1) <= {2 * TOL}, false)
          FROM u JOIN markets mk ON mk.token = '0x' || lower(hex(u.base))
          LEFT JOIN token_bars b ON b.token = mk.token AND b.period = 60 AND b.time = u.minute * 1000
          WHERE u.quote_amt * u.rate <= {MAX_AVG_SWAP_USD}
          ORDER BY 1, u.bn, u.log_index""")
    print("tape", c.execute("SELECT count(*), count(DISTINCT token), min(time), max(time) FROM tape").fetchone(), flush=True)
    c.execute("CREATE TABLE info(key VARCHAR, value VARCHAR)")
    c.executemany("INSERT INTO info VALUES (?, ?)", [
        ("chain", chain), ("built_at", str(int(time.time()))), ("data_until_ms", str(now_ms)),
        ("policy", "em_pool_reference_v2"), ("tol", str(TOL)), ("min_usd", str(min_usd)),
        ("retention", json.dumps({str(k): v for k, v in RETENTION.items()})),
        ("last_block", str(c.execute(f"SELECT max(ls) // 100000 FROM read_parquet('{stage}')").fetchone()[0]))])
    c.execute("CHECKPOINT")
    # side exports for jobs that must not open the DB while the server holds it
    c.execute(f"COPY (SELECT token, symbol, kind FROM markets ORDER BY token) TO '{ROOT}/data/{chain}/markets_export.parquet' (FORMAT parquet)")
    json.dump(dict(c.execute("SELECT key, value FROM info").fetchall()), open(f"{ROOT}/data/{chain}/info_export.json", "w"))
    c.close()
    os.replace(tmp, db)
    print("DONE", chain, f"{os.path.getsize(db) / 1e9:.2f} GB", round(time.time() - t0), "s", flush=True)


if __name__ == "__main__":
    ch = sys.argv[1]
    mu = MIN_USD[ch]
    if "--min-usd" in sys.argv:
        mu = float(sys.argv[sys.argv.index("--min-usd") + 1])
    main(ch, mu)
