"""em-charts: Ticking-compatible candle API for Arbitrum One and Robinhood Chain assets.

Speaks the shapes the Ticking chart SDK reads (sdk/data-feed.js): /api/public/engine/candles,
/api/public/stream/candles (SSE snapshots), /api/public/markets[/<id>]. Candles are USD on-chain DEX
references built by build_db.py and kept current by live.py. Static test page under /, copied SDK under /ticking/.
usage: server.py [--port 8895] [--no-live]
"""
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import duckdb

from common import CHAINS, RETENTION, ROOT, TAPE_DAYS
from emread import Reader
import emapi
import sec_bars_read
import sec_dump_tags

STORED = sorted(RETENTION)
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
WEEK_ORIGIN = -259200000
POLICY = "em_pool_reference_v2"
STARTED = time.time()
MAX_INFLIGHT = 32          # concurrent non-stream requests
MAX_STREAMS = 64           # concurrent SSE connections
RATE = (20.0, 120.0)       # per-client token bucket: refill per second, burst
RATE_LOOPBACK = (200.0, 600.0)  # a local proxy without X-Forwarded-For carries many users
INFLIGHT = threading.BoundedSemaphore(MAX_INFLIGHT)
STREAMS = threading.BoundedSemaphore(MAX_STREAMS)
BUCKETS = {}
BUCKET_LOCK = threading.Lock()


def allow(client):
    now = time.time()
    rate = RATE_LOOPBACK if client in ("127.0.0.1", "::1") else RATE
    with BUCKET_LOCK:
        tokens, at = BUCKETS.get(client, (rate[1], now))
        tokens = min(rate[1], tokens + (now - at) * rate[0])
        if tokens < 1:
            BUCKETS[client] = (tokens, now)
            return False
        BUCKETS[client] = (tokens - 1, now)
        if len(BUCKETS) > 5000:
            for k in [k for k, (_, t) in BUCKETS.items() if now - t > 600]:
                BUCKETS.pop(k, None)
        return True
MAX_CARRY = 5
WEB = os.path.join(ROOT, "web")
LOG = open(os.path.join(ROOT, "logs", "server.log"), "a", buffering=1)


def clean(x):
    """JSON-safe: non-finite floats become null."""
    if isinstance(x, float):
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


def log(msg):
    LOG.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg + "\n")


def parse_interval(s):
    m = re.fullmatch(r"([1-9]\d*)(s|m|h|d|w)", (s or "1m").strip().lower())
    if not m:
        raise ValueError("Use a whole number and s, m, h, d or w, such as 3m or 1w.")
    sec = int(m.group(1)) * UNITS[m.group(2)]
    if sec > 604800:
        raise ValueError("Choose an interval between 1 minute and 1 week.")
    unit = "w" if sec == 604800 else "d" if sec % 86400 == 0 else "h" if sec % 3600 == 0 else "m" if sec % 60 == 0 else "s"
    return sec, f"{sec // UNITS[unit]}{unit}"


def interval_start(t, ms):
    origin = WEEK_ORIGIN if ms == 604800000 else 0
    return (t - origin) // ms * ms + origin


CH = {}
WALLETS = {}
SPARK = {"at": 0, "data": {}}
QA = {"at": 0, "data": None, "lock": threading.Lock()}
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def ts_arg(v):
    if v in (None, ""):
        return None
    x = int(v)
    return x // 1000 if x > 1e11 else x


def wallet_trades_response(qs):
    chain = qs.get("chain")
    if chain not in CH:
        return 400, {"detail": "chain must be one of " + ", ".join(CH)}
    addr = qs.get("address", "")
    if not ADDR_RE.match(addr):
        return 400, {"detail": "address must be a 0x-prefixed 20-byte hex address."}
    tok = qs.get("token", "")
    ch, m = resolve_market(f"{chain}:{tok}" if ":" not in tok else tok)
    if not m or ch.name != chain:
        return 404, {"detail": "token is not a served market on this chain (see /api/public/markets?network=%s)." % chain}
    w = WALLETS.get(chain)
    if w:
        w.load()
    if not w or not w.ready:
        return 503, {"detail": "Wallet index is not built yet for this chain."}
    limit = max(1, min(5000, int(qs.get("limit", "1000"))))
    res = w.query(ch, addr, m, ts_arg(qs.get("from")), ts_arg(qs.get("to")), limit)
    res.update(chain=chain, address=addr.lower(), market=m["id"], symbol=m["symbol"], quote="USD",
               attribution="transaction sender (tx.from); swaps via smart-contract wallets or relayers are attributed to the sender",
               timePrecision="minute", generated_at=int(time.time()))
    return 200, res


def traders_response(kind, qs):
    """/api/public/em/top-traders?market=<chain>:<token>&window=7d|30d|all, /api/public/em/wallet-pnl?chain=&address= (traders_api)."""
    import traders_api
    if kind == "top-traders":
        ch, m = resolve_market(qs.get("market") or qs.get("mint"))
        if not m:
            return 404, {"detail": "Unknown market. Use <chain>:<token address> (see /api/public/markets)."}
        return traders_api.top_traders(ch.name, m, qs.get("window", "30d"), int(qs.get("limit", "20")))
    chain = qs.get("chain")
    if chain not in CH:
        return 400, {"detail": "chain must be one of " + ", ".join(CH)}
    return traders_api.wallet_pnl(chain, qs.get("address", ""), int(qs.get("limit", "20")))


def em_market(qs):
    ch, m = resolve_market(qs.get("market") or qs.get("mint"))
    if not m:
        return None, None, (404, {"detail": "Unknown market. Use <chain>:<token address> (see /api/public/markets)."})
    return ch, m, None


def em_response(kind, qs):
    ch, m, err = em_market(qs)
    if err:
        return err
    mid, chain = m["id"], ch.name
    meta = emapi.meta(chain)
    if not meta:
        return 503, {"detail": "Token analytics are not built yet for this chain; retry in a few minutes."}
    as_of, upd = int(meta.get("data_time", 0)), int(meta.get("refreshed_at", 0))
    if kind == "token-state":
        r = emapi.token_state(chain, mid)
    elif kind == "token-brief":
        r = emapi.brief(chain, mid)
    elif kind == "flows":
        w = qs.get("window", "24h")
        if w not in ("1h", "24h", "7d"):
            return 400, {"detail": "window must be 1h, 24h or 7d"}
        r = emapi.flows(chain, mid, w)
    elif kind == "liquidations":
        r = emapi.liquidations(chain, mid, m.get("last_price"), m["symbol"])
        if r is not None:
            r.update(asOf=as_of, updatedAt=upd, priceAsOf=(m.get("last_time") or 0) // 1000)
    else:  # events
        to = ts_arg(qs.get("to")) or int(time.time())
        frm = ts_arg(qs.get("from")) or (to - 7 * 86400)
        limit = max(1, min(500, int(qs.get("limit", "200"))))
        kinds = [k for k in (qs.get("kinds") or "").split(",") if k]
        ev = emapi.events(chain, mid, frm, to, limit, kinds or None)
        r = None if ev is None else {"market": mid, "symbol": m["symbol"], "asOf": as_of, "updatedAt": upd, "from": frm, "to": to,
                                     "events": ev, "coverage": {"notes": [
                                         "trades: per transaction net, size >= max($25K, 0.5% of the token's DEX liquidity)",
                                         "lending (collateral in/out, borrow, repay): >= max($10K, 0.25% of DEX liquidity); liquidations >= $1K",
                                         "lending events since the econ snapshot (unix %s); trades: last 7 days; bridges/LP: live.db window (from unix %s)"
                                         % (meta.get("snap_time"), meta.get("live_from"))], "unpricedShare": 0.0 if m.get("last_price") else 1.0}}
    if r is None:
        return 404, {"detail": f"No analytics for {mid} (not in the latest refresh)."}
    return 200, r


def flowmap_response(qs):
    import flowapi
    chain, w = qs.get("chain"), qs.get("window", "24h")
    if chain not in CH:
        return 400, {"detail": "chain must be one of " + ", ".join(CH)}
    if w not in flowapi.WIN:
        return 400, {"detail": "window must be 1h, 24h or 7d"}
    r = flowapi.flowmap(chain, w)
    if r is None:
        return 503, {"detail": "The money-flow map is not built yet for this chain; retry in a few minutes."}
    return 200, r


def flow_wallet_response(qs):
    import flowapi
    chain, w, addr = qs.get("chain"), qs.get("window", "24h"), (qs.get("address") or "").lower()
    if chain not in CH:
        return 400, {"detail": "chain must be one of " + ", ".join(CH)}
    if w not in flowapi.WIN:
        return 400, {"detail": "window must be 1h, 24h or 7d"}
    if not ADDR_RE.match(addr):
        return 400, {"detail": "address must be a 0x-prefixed 20-byte hex address."}
    return 200, flowapi.wallet(CH[chain], addr, w)


def quality_response(qs):
    from qa import report
    if QA["data"] is None or time.time() - QA["at"] > 600 or qs.get("refresh") == "1":
        if not QA["lock"].acquire(blocking=False):
            return (200, QA["data"]) if QA["data"] else (503, {"detail": "Quality report is being computed; retry shortly."})
        try:
            data = {}
            for name, ch in CH.items():
                c = ch.con.cursor()
                data[name] = report(c)
            QA.update(at=time.time(), data={"generated_at": int(time.time()), "chains": data})
        finally:
            QA["lock"].release()
    return 200, QA["data"]



def fast_feed_response(qs):
    """Same exact-contract composite used by the chart, at one-second resolution."""
    from evm_composite import compose,POLICY
    chain=(qs.get('network') or qs.get('chain') or '').lower()
    chain={'arb':'arbitrum','rh':'robinhood'}.get(chain,chain)
    mint=(qs.get('mint') or qs.get('market') or '').lower()
    if ':' in mint:
        selected,_,mint=mint.partition(':')
        if chain and chain!=selected:return 422,{'detail':'Network and contract identity disagree.'}
        chain=selected
    if chain not in ('arbitrum','robinhood') or not re.fullmatch(r'0x[0-9a-f]{40}',mint):return 422,{'detail':'Use an exact network and token contract.'}
    try:limit=max(1,min(120,int(qs.get('limit') or 60)))
    except (TypeError,ValueError):return 422,{'detail':'Use a page size of 1–120.'}
    raw=compose({'candles':[],'coverage':{}},chain,mint,1,limit,gap='recorded',history='full')
    bars=[{**b,'time':b['time']*1000,'sourceTime':b.get('sourceTime',b['time'])*1000,'wickClamped':False} for b in raw['candles']]
    return 200,{'ok':True,'chain':chain,'market':chain+':'+mint,'currency':'USD','interval':'1s','bars_1s':bars,'wickRejects':[],'wickBand':None,'latencyPaintOnly':True,'identityPolicy':POLICY,'coverage':raw['coverage'],'sot':'exact_contract_executions'}


def health_response():
    chains = {}
    ok = True
    for name, ch in CH.items():
        lag = round(time.time() - ch.data_until / 1000)
        lt = LIVE.get(name)
        st = {"markets": len(ch.markets_by_id), "dataUntil": ch.data_until, "lagSeconds": lag,
              "live": getattr(lt, "last_cycle", None), "liveErrors": getattr(lt, "errors", None),
              "livePaused": os.path.exists(f"{ROOT}/data/LIVE_PAUSED"), "tape": "tape" in ch.tables,
              "walletIndex": bool(WALLETS.get(name) and WALLETS[name].ready), "policy": ch._info.get("policy"), "deepUntil": ch.deep_until_ms(),
              "analytics": getattr(ANALYTICS.get(name), "last", None), "analyticsErrors": getattr(ANALYTICS.get(name), "errors", None),
              "dataVersion": ch.data_version()}
        st["status"] = "ok" if lag < 900 or st["livePaused"] else "lagging"
        ok = ok and st["status"] == "ok"
        chains[name] = st
    return (200 if ok and CH else 503), {"status": "ok" if ok and CH else "degraded", "uptimeSeconds": round(time.time() - STARTED),
                                         "chains": chains, "limits": {"inflight": MAX_INFLIGHT, "streams": MAX_STREAMS,
                                                                       "ratePerSecond": RATE[0], "burst": RATE[1]}}


def resolve_market(q):
    """mint/market forms: '<chain>:0x..', '<chain>:SYMBOL', '0x..', 'SYMBOL' (stocks first, then volume)."""
    key = (q or "").strip()
    chain = None
    if ":" in key:
        chain, key = key.split(":", 1)
    cands = []
    for name, ch in CH.items():
        if chain and name != chain:
            continue
        if key.lower().startswith("0x"):
            m = ch.markets_by_id.get(f"{name}:{key.lower()}")
            if m:
                cands.append((ch, m))
        else:
            cands += [(ch, m) for m in ch.markets_by_id.values() if (m["symbol"] or "").lower() == key.lower()]
    if not cands:
        return None, None
    cands.sort(key=lambda x: (x[1]["kind"] != "stock", x[1]["kind"] != "anchor", -(x[1]["usd_volume"] or 0)))
    return cands[0]


def resolve_pool(q):
    chain, addr = (q.split(":", 1) if ":" in q else (None, q))
    for name, ch in CH.items():
        if chain and name != chain:
            continue
        hit = ch.pool_index.get(addr.lower())
        if hit:
            return ch, hit[0], hit[1]
    return None, None, None


def fetch_bars(ch, series, key, period_s, before, limit):
    bars, more = ch.bars(series, key, period_s, before_ms=int(before) if before else None, limit=limit)
    return bars, more, ch.resolution_starts()


def carry(bars, ms, limit):
    if not bars:
        return bars, 0
    out, carried = [bars[0]], 0
    for b in bars[1:]:
        prev = out[-1]
        gap = (b["time"] - prev["time"]) // ms - 1
        if 0 < gap <= MAX_CARRY:
            for i in range(1, gap + 1):
                p = prev["close"]
                out.append({"time": prev["time"] + i * ms, "open": p, "high": p, "low": p, "close": p, "volume": 0, "baseVolume": 0,
                            "count": 0, "synthetic": True, "gapKind": "unverified_no_record", "sourceTime": prev["time"]})
                carried += 1
        out.append(b)
    return out[-limit:], sum(1 for x in out[-limit:] if x.get("synthetic"))


from response_cache import ResponseCache, generation
CANDLE_CACHE = ResponseCache(max_bytes=64*1024*1024, max_entries=512, max_inflight=48)


def sanitize_candle_wicks(bars, kind="token"):
    """Clamp OHLC tails for TickChart scale (data-side; Sol sparse-wick intent).
    Display/API only — tip/hot SoT unchanged. Band from common.WICK_BAND.
    Gap-open (open=prev close) is pulled into the band so it cannot force a wick.
    """
    from common import WICK_BAND
    band = WICK_BAND.get(kind) or WICK_BAND.get("token", 0.05)
    out = []
    for b in bars:
        if b.get("synthetic"):
            out.append(b); continue
        o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        if not (c > 0 and o > 0):
            out.append(b); continue
        hi_cap = c * (1.0 + band)
        lo_floor = c * (1.0 - band)
        no = min(max(o, lo_floor), hi_cap)
        nh = min(max(h, no, c), hi_cap)
        nl = max(min(l, no, c), lo_floor)
        if no != o or nh != h or nl != l:
            b = dict(b)
            b["open"], b["high"], b["low"] = no, nh, nl
            b["wickClamped"] = True
            if no != o:
                b["openClamped"] = True
        out.append(b)
    return out


def _candles_response_sec(qs, period_s, label, limit, before, gap, agg):
    """Serve 1s..59s from sec-bars (Binance 1s mirror). Sol+Arb+RH mapped majors only."""
    if agg not in ("token", "pool"):
        return 400, {"detail": "aggregation must be token or pool for 1s."}
    net = qs.get("network")
    mid, meta = sec_bars_read.resolve_sec_market(net, qs.get("mint"), qs.get("market"))
    if not mid:
        return 404, {"detail": "No 1s series for this market. Arb/RH need tip/hot fills; Sol majors need CEX map.",
                     "hint": "All Arb+RH hot tokens get tip_hot 1s bars; CEX-mapped majors also mirror Binance 1s."}
    try:
        bars, more = sec_bars_read.fetch_1s(mid, period_s, before_ms=int(before) if before else None, limit=limit)
    except ValueError as e:
        return 400, {"detail": str(e)}
    carried = 0
    m = None
    try:
        if mid.startswith(("arbitrum:", "robinhood:")):
            ch, m = resolve_market(mid)
    except Exception:
        m = None
    symbol = (m or {}).get("symbol") or (meta or {}).get("symbol") or mid.split(":")[-1][:8]
    name = (m or {}).get("name") or symbol
    kind = (m or {}).get("kind") or ("anchor" if symbol in ("WETH", "WBTC", "ARB", "SOL") else "token")
    if kind == "anchor":
        px = float((bars[-1]["close"] if bars else 0) or 0)
        kind = "usd" if 0.5 < px < 1.5 else "anchor"
    bars = sanitize_candle_wicks(bars, kind)
    for b in bars:
        b.setdefault("synthetic", False)
        b.setdefault("sourceTime", b["time"])
        b["quoteCurrency"] = "USD"
    now = int(time.time() * 1000)
    last_t = bars[-1]["time"] if bars else 0
    ident = {"network": mid.split(":")[0], "market": mid, "baseMint": mid, "quoteMint": "USD",
             "series": "reference", "aggregation": "sec_bars_binance_1s_v1", "venue": "cex_mirror",
             "instrumentType": "spot"}
    body = {"mode": "sec_bars", "aggregation": "token", "market": mid, "pool": None, "mint": mid,
            "live": True, "observationAvailable": True, "refresh_seconds": 1, "symbol": symbol, "name": name,
            "quoteMint": "USD", "quote": "USD", "identity": ident, "interval": label, "candles": bars,
            "fillPolicy": "binance_1s_contiguous_flat_empty", "carriedBars": carried,
            "revision": last_t, "cursor": last_t, "hasMore": more,
            "nextBefore": bars[0]["time"] if bars else None,
            "updated_at": (last_t // 1000) if last_t else int(time.time()),
            "stale": (now - last_t) > 15_000 if last_t else True,
            "msPaint": True, "secBars": True,
            "gapDisclosure": "1s bars mirror Binance kline_1s: UTC second floor; empty seconds emitted with O=H=L=C=prior close, volume=0.",
            "history": {"source": "sec-bars", "sourceLabel": "Binance 1s kline mirror (+ oracle-fast tip for Sol)",
                        "market": mid, "policy": "binance_1s_kline_mirror_v1",
                        "emptySecond": "emit_flat_carry", "openTime": "utc_second_floor_ms"},
            "coverage": {"complete": False, "source": "cex_1s_mirror", "policy": "binance_1s_kline_mirror_v1",
                         "commitment": "provisional_paint", "sot": "tip_hot_onchain",
                         "priceSampling": "Binance 1s for CEX-mapped majors; tip_hot 1s from on-chain fills for all other Arb/RH tokens.","coveragePolicy": "no_invented_prices_hot_required"}}
    # 1s whale dump tags (tip→hot fills aligned to Binance UTC second) — 2026-10-23
    try:
        sec_dump_tags.attach_to_candles_body(body, mid, symbol=symbol, period_s=period_s)
    except Exception as e:
        body.setdefault("dumpTags", [])
        body.setdefault("markers", [])
        body["dumpTagsError"] = str(e)[:160]
    return 200, body


def candles_response(qs):
    key=(generation(CH,qs),tuple(sorted(qs.items())))
    return CANDLE_CACHE.read(key,lambda:_candles_response(qs),ttl=1.0)

def _candles_response(qs):
    agg = qs.get("aggregation", "pool")
    period_s, label = parse_interval(qs.get("interval", "1m"))
    limit = max(1, min(600, int(qs.get("limit", "180"))))
    before = qs.get("before")
    gap = qs.get("gap_policy", "carry")
    # 초봉 2026-10-23: Binance-style 1s store (sec-bars). Not tip/hot SoT.
    if period_s < 60:
        return _candles_response_sec(qs, period_s, label, limit, before, gap, agg)
    if agg == "token":
        ch, m = resolve_market(qs.get("mint") or qs.get("market"))
        if not m:
            return 404, {"detail": "Unknown market. Use <chain>:<token address> from /api/public/markets."}
        bars, more, starts = fetch_bars(ch, "token", m["token"], period_s, before, limit)
        ident = {"network": ch.name, "market": m["id"], "baseMint": m["id"], "quoteMint": "USD", "series": "reference",
                 "aggregation": POLICY, "venue": "em", "instrumentType": "spot"}
        head = {"mode": "asset_history", "aggregation": "token", "market": m["id"], "pool": None, "mint": m["id"]}
    elif agg == "pool":
        if qs.get("pool"):
            ch, token, p = resolve_pool(qs["pool"])
            if not p:
                return 404, {"detail": "Unknown pool."}
            m = ch.markets_by_id.get(f"{ch.name}:{token}")
        else:
            ch, m = resolve_market(qs.get("mint") or qs.get("market"))
            if not m or not ch.pools.get(m["token"]):
                return 404, {"detail": "No pool series for this market; use aggregation=token."}
            p = ch.pools[m["token"]][0]
        bars, more, starts = fetch_bars(ch, "pool", p["address"], period_s, before, limit)
        ident = {"network": ch.name, "market": p["pool"], "pool": p["pool"], "baseMint": m["id"], "quoteMint": "USD",
                 "series": "trade", "aggregation": "pool", "venue": "em", "instrumentType": "spot"}
        head = {"mode": "executed_history", "aggregation": "pool", "market": p["pool"], "pool": p["pool"], "mint": m["id"],
                "poolQuote": p["quote"]}
    else:
        return 400, {"detail": "aggregation must be token or pool (exchange markets are not served here)."}
    ms = period_s * 1000
    carried = 0
    if gap == "carry":
        bars, carried = carry(bars, ms, limit)
    # tails 2026-10-23: clamp pathological wicks (deep memes + prior ±10% live band leftovers)
    kind = (m.get("kind") or "token")
    if kind == "anchor":
        px = float(m.get("last_price") or (bars[-1]["close"] if bars else 0) or 0)
        kind = "usd" if 0.5 < px < 1.5 else "anchor"
    bars = sanitize_candle_wicks(bars, kind)
    for b in bars:
        b.setdefault("synthetic", False)
        b.setdefault("sourceTime", b["time"])
        b["quoteCurrency"] = "USD"
    now = int(time.time() * 1000)
    body = dict(head, live=False, observationAvailable=False, refresh_seconds=30, symbol=m["symbol"], name=m["name"],
                quoteMint="USD", quote="USD", identity=ident, interval=label, candles=bars, fillPolicy="recorded",
                carriedBars=carried, revision=ch.revision, cursor=ch.revision, hasMore=more,
                nextBefore=bars[0]["time"] if bars else None, updated_at=ch.data_until // 1000,
                stale=now - ch.data_until > 15 * 60 * 1000,
                gapDisclosure=f"At most {MAX_CARRY} short intervals carry the prior close; longer gaps stay unfilled. "
                              "Completeness of the on-chain swap archive is not attested.",
                history={"source": "em-mapping", "sourceLabel": "Economic Machine on-chain DEX swaps", "market": m["id"],
                         "earliest": ch.firsts.get(m["token"]), "start": ch.firsts.get(m["token"]), "resolutionStarts": starts,
                         "archiveQuote": "USD", "currentQuote": "USD", "policy": POLICY, "priceConversion": "none"},
                coverage={"complete": False, "archiveCompleteness": "not_attested", "source": "onchain_dex_swaps",
                          "policy": POLICY, "commitment": "follower_verified_chunks",
                          "priceSampling": "Per minute: the most established pool printing sets the reference; pools within 5% of it are "
                                           "admitted; open/close are the earliest/latest admitted prints; wicks beyond 10% are dropped.",
                          "quoteConversion": "USD stables at $1; WETH/ETH, BTC and ARB quotes converted with a same-minute composite "
                                             "of their own USD-stable pools (as-of, at most 6 h old).",
                          "volumeDefinition": "USD turnover of admitted pool prints (quote leg).",
                          "observedPoolCount": m.get("n_pools")})
    return 200, body


def tape_ready(ch):
    return "tape" in ch.tables or ch.hot() is not None


def trades_response(qs):
    """Ticking trade-tape shape (sdk normalizeTape): newest first, at most `limit` (default 100)."""
    agg = qs.get("aggregation", "token")
    limit = max(1, min(500, int(qs.get("limit", "100"))))
    pool = None
    if qs.get("pool"):
        ch, token, p = resolve_pool(qs["pool"])
        if not p:
            return 404, {"detail": "Unknown pool."}
        m = ch.markets_by_id.get(f"{ch.name}:{token}")
        pool = p
    else:
        ch, m = resolve_market(qs.get("mint") or qs.get("market"))
        if not m:
            return 404, {"detail": "Unknown market."}
        if agg == "pool" and ch.pools.get(m["token"]):
            pool = ch.pools[m["token"]][0]
    if not tape_ready(ch):
        return 503, {"detail": "Trade tape is not built yet for this chain."}
    b = qs.get("before")
    before_s = (int(b) // 1000 if int(b) > 1e11 else int(b)) if b else None
    trades = []
    for r in ch.tape(m["token"], pool=pool["address"] if pool else None, before_s=before_s, limit=limit):
        txs = r["tx"]
        trades.append({"identity": f"{ch.name}:{txs or r['block']}:{r['log_index']}", "time": r["time"], "price": r["price"],
                       "reference_price": r["price"], "reference_eligible": bool(r["eligible"]), "base_volume": r["base_volume"],
                       "quote_mint": "USD", "usd": r["usd"], "side": r["side"], "slot": r["block"], "tx_order": r["tx_index"],
                       "event_order": r["log_index"], "tradeId": r["block"] * 100000 + r["log_index"],
                       "pool": f"{ch.name}:{r['pool']}", "tx": txs, "trader": r["trader"]})
    ident = {"network": ch.name, "market": pool["pool"] if pool else m["id"], "baseMint": m["id"], "quoteMint": "USD",
             "series": "trade" if pool else "reference", "aggregation": "pool" if pool else POLICY, "venue": "em"}
    if pool:
        ident["pool"] = pool["pool"]
    return 200, {"identity": ident, "market": ident["market"], "mint": m["id"], "symbol": m["symbol"], "trades": trades,
                 "updated_at": ch.data_until // 1000, "revision": ch.revision,
                 "coverage": {"window_days": TAPE_DAYS, "timePrecision": "second for follower blocks (hot store), minute for older rows",
                              "side": "B = the transaction sender received the market token, A = sent it",
                              "reference_eligible": "price within 10% of the token reference for that minute"}}


_MS_QUOTES = {"at": 0.0, "data": {}}

def _ms_quotes():
    if time.time() - _MS_QUOTES["at"] < 0.5 and _MS_QUOTES["data"]:
        return _MS_QUOTES["data"]
    try:
        with open("/srv/skew/em-realtime/sec-bars/out/quotes.json") as f:
            _MS_QUOTES["data"] = json.load(f)
            _MS_QUOTES["at"] = time.time()
    except (OSError, ValueError):
        pass
    return _MS_QUOTES["data"]


# --- category overlay (2026-10-24) ---
_CATEGORY_OVERLAY = {"by_id": {}, "by_network_symbol": {}, "at": 0}
_CAT_PRIO = {
    "stock": 0, "bond": 0, "rwa": 0, "programmable": 0,
    "stable": 1, "defi": 1,
    "other": 2, "anchor": 2,
    "meme": 3,
}


def _load_category_overlay(force=False):
    if not force and _CATEGORY_OVERLAY["at"] and (time.time() - _CATEGORY_OVERLAY["at"] < 60):
        return
    path = "/srv/skew/em-charts/data/category_overlay.json"
    try:
        with open(path) as f:
            data = json.load(f)
        _CATEGORY_OVERLAY["by_id"] = {str(k).lower(): v for k, v in (data.get("by_id") or {}).items()}
        _CATEGORY_OVERLAY["by_network_symbol"] = {str(k): v for k, v in (data.get("by_network_symbol") or {}).items()}
        _CATEGORY_OVERLAY["at"] = time.time()
    except (OSError, ValueError):
        _CATEGORY_OVERLAY["at"] = time.time()


def resolve_category(ch, m):
    _load_category_overlay()
    mid = str(m.get("id") or "").lower()
    sym = (m.get("symbol") or "").upper()
    tok = (m.get("token") or "").lower()
    net = ch.name
    hit = (_CATEGORY_OVERLAY["by_id"].get(mid)
           or _CATEGORY_OVERLAY["by_network_symbol"].get(f"{net}:{sym}")
           or _CATEGORY_OVERLAY["by_network_symbol"].get(f"{net}:{tok}"))
    if hit:
        return hit.get("category") or "other", list(hit.get("subtags") or [])
    kind = (m.get("kind") or "token")
    if kind == "anchor":
        return "anchor", []
    if kind == "stock":
        return "stock", ["equity"]
    if net == "robinhood":
        return "meme", ["launchpad"]
    return "other", []


def market_row(ch, m):
    # chart-cache 2026-10-23: liquid (hot-touched / stock / anchor) ages follow chain tip when recently printed
    last_ms = m["last_time"] or 0
    kind = m.get("kind") or "token"
    recently = last_ms and (ch.data_until - last_ms) < 2 * 3600 * 1000
    liquid = kind in ("stock", "anchor") or ((m.get("volume_24h") or 0) > 0 and recently)
    updated = (ch.data_until // 1000) if liquid and recently else (last_ms // 1000)
    price = m["last_price"]
    q = (_ms_quotes().get("quotes") or {}).get(m["id"]) or (_ms_quotes().get("quotes") or {}).get(m["id"].lower())
    if q and q.get("price") and (q.get("age_s") is None or q["age_s"] < 15):
        price = q["price"]
        updated = int(q["t_ms"] // 1000)
    cat, subtags = resolve_category(ch, m)
    return {"id": m["id"], "symbol": m["symbol"], "name": m["name"], "mint": m["id"], "address": m["token"], "network": ch.name,
            "kind": m["kind"], "category": cat, "subtags": subtags,
            "price": price, "change": None if m["change_24h"] is None else m["change_24h"] * 100,
            "volume": m["volume_24h"], "usdVolumeAllTime": m["usd_volume"], "swaps": m["swaps"], "pools": m["n_pools"],
            "updated_at": updated, "stale": (ch.data_until - last_ms) > 86400000,
            "uiMultiplier": m.get("ui_multiplier"), "sparkline": SPARK["data"].get((ch.name, m["token"])),
            "quality": m.get("quality") or "ok", "qualityNote": m.get("quality_note")}


def refresh_spark():
    if time.time() - SPARK["at"] < 300:
        return
    data = {}
    for name, ch in CH.items():
        c = ch.con.cursor()
        for token, closes in c.execute("""SELECT token, list(close ORDER BY time) FROM token_bars
                                          WHERE period = 3600 AND time > ? GROUP BY token""", [ch.data_until - 7 * 86400000]).fetchall():
            data[(name, token)] = closes
    SPARK.update(at=time.time(), data=data)


def markets_response(qs):
    refresh_spark()
    rows = [market_row(ch, m) for ch in CH.values() for m in ch.markets_by_id.values()]
    net = qs.get("network")
    if net:
        rows = [r for r in rows if r["network"] == net]
    if qs.get("kind"):
        rows = [r for r in rows if r["kind"] == qs["kind"]]
    # 2026-10-23: honor q/search/query so workspace search (e.g. BTC) resolves on backend
    q = (qs.get("q") or qs.get("search") or qs.get("query") or "").strip().lower()
    if q:
        def _hit(r):
            blob = " ".join([
                str(r.get("symbol") or ""),
                str(r.get("name") or ""),
                str(r.get("id") or ""),
                str(r.get("address") or ""),
                str(r.get("mint") or ""),
            ]).lower()
            if q in blob:
                return True
            # BTC ↔ WBTC/cbBTC/tBTC convenience
            if q == "btc" and any(x in blob for x in ("wbtc", "cbbtc", "tbtc", "btc")):
                return True
            if q == "eth" and any(x in blob for x in ("weth", "eth")):
                return True
            return False
        rows = [r for r in rows if _hit(r)]
    if qs.get("category"):
        want = {c.strip().lower() for c in str(qs.get("category")).split(",") if c.strip()}
        rows = [r for r in rows if (r.get("category") or "").lower() in want]
    # priority: stock|bond|rwa|programmable ≫ stable|defi ≫ other ≫ meme, then volume
    rows.sort(key=lambda r: (_CAT_PRIO.get(r.get("category") or "other", 2), -(r["volume"] or 0)))
    # No implicit catalog ceiling; UI page limits are explicit.
    net = qs.get("network")
    limit = qs.get("limit")
    try:
        limit_n = int(limit) if limit not in (None, "") else None
    except ValueError:
        limit_n = None
    if limit_n is not None:
        rows = rows[:max(0, limit_n)]
    return 200, {"tokens": rows, "generated_at": int(time.time()), "refresh_seconds": 60, "universe": "em_dex_markets",
                 "scope": "Arbitrum One + Robinhood Chain tokens with on-chain DEX USD turnover above the build threshold, "
                          "every Robinhood stock token, and the quote anchors. Ranked by category priority then volume.",
                 "q": q or None, "categoryEnum": list(_CAT_PRIO.keys())}


def market_detail(mid):
    ch, m = resolve_market(unquote(mid))
    if not m:
        return 404, {"detail": "Unknown market."}
    refresh_spark()
    r = market_row(ch, m)
    r.update(pools={"items": ch.pools.get(m["token"], [])}, firstTime=ch.firsts.get(m["token"]), decimals=m["decimals"],
             policy=POLICY, dataUntil=ch.data_until)
    return 200, r


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, code, body, cache=0):
        b = json.dumps(clean(body), separators=(",", ":"), allow_nan=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", f"public, max-age={cache}" if code == 200 and cache else "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        if code in (429, 503):
            self.send_header("Retry-After", "2")
        self.end_headers()
        self.wfile.write(b)

    def client(self):
        peer = self.client_address[0]
        fwd = self.headers.get("X-Forwarded-For")
        return fwd.split(",")[0].strip() if fwd and peer in ("127.0.0.1", "::1") else peer

    def not_allowed(self):
        self.send_json(405, {"detail": "Only GET is supported."})

    do_POST = do_PUT = do_DELETE = do_PATCH = not_allowed

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if len(self.path) > 4096:
            return self.send_json(414, {"detail": "Request URI too long."})
        if not allow(self.client()):
            return self.send_json(429, {"detail": "Too many requests; slow down."})
        qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
        streaming = u.path.startswith("/api/public/stream/")
        gate = STREAMS if streaming else INFLIGHT
        if not gate.acquire(timeout=0 if streaming else 3):
            return self.send_json(503, {"detail": "Server busy; retry shortly."})
        try:
            return self.route(u, qs)
        except ValueError as e:
            return self.send_json(400, {"detail": str(e)[:300]})
        except duckdb.Error as e:
            log("db error " + self.path + " " + str(e)[:300])
            return self.send_json(503, {"detail": "Data temporarily unavailable."})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            log("error " + self.path + "\n" + traceback.format_exc())
            try:
                self.send_json(500, {"detail": "Internal error."})
            except Exception:
                pass
        finally:
            gate.release()

    def route(self, u, qs):
        p = u.path
        if p == "/api/public/engine/fast-feed":
            code, body = fast_feed_response(qs)
            return self.send_json(code, body, cache=0)
        if p == "/api/public/engine/candles":
            code, body = candles_response(qs)
            fresh = not qs.get("before")
            return self.send_json(code, body, cache=5 if fresh else 120)
        if p == "/api/public/engine/trades":
            return self.send_json(*trades_response(qs), cache=5)
        if p == "/api/public/stream/candles":
            return self.stream(qs, candles_response, dict(limit="180"))
        if p == "/api/public/stream/trades":
            return self.stream(qs, trades_response, dict(limit="100"))
        if p == "/api/public/markets":
            return self.send_json(*markets_response(qs), cache=8)
        if p.startswith("/api/public/markets/"):
            return self.send_json(*market_detail(p.rsplit("/", 1)[1]), cache=8)
        if p == "/api/public/em/wallet-trades":
            return self.send_json(*wallet_trades_response(qs), cache=30)
        if p in ("/api/public/em/top-traders", "/api/public/em/wallet-pnl"):
            return self.send_json(*traders_response(p.rsplit("/", 1)[1], qs), cache=60)
        if p in ("/api/health", "/health"):
            return self.send_json(*health_response())
        if p.startswith("/api/public/em/") and p.rsplit("/", 1)[1] in ("token-state", "token-brief", "flows", "liquidations", "events"):
            kind = p.rsplit("/", 1)[1]
            return self.send_json(*em_response(kind, qs), cache=30 if kind in ("liquidations", "events") else 60)
        if p == "/api/public/em/flowmap":
            return self.send_json(*flowmap_response(qs), cache=60)
        if p == "/api/public/em/flowmap/wallet":
            return self.send_json(*flow_wallet_response(qs), cache=30)
        if p == "/api/health/quality":
            return self.send_json(*quality_response(qs))
        if p == "/api/status":
            return self.send_json(200, status())
        if p.startswith("/api/"):
            return self.send_json(404, {"detail": "Not served by em-charts."})
        return self.static(p)

    def static(self, path):
        rel = os.path.normpath(unquote(path).lstrip("/")) or "index.html"
        if path.endswith("/"):
            rel = os.path.join(rel, "index.html") if rel != "." else "index.html"
        full = os.path.realpath(os.path.join(WEB, rel))
        if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
            return self.send_json(404, {"detail": "Not found."})
        data = open(full, "rb").read()
        ctype = "text/javascript" if full.endswith((".js", ".mjs")) else mimetypes.guess_type(full)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=300" if "/ticking/" in full else "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def stream(self, qs, fn, fixed):
        qs = dict(qs, **fixed)
        qs.pop("before", None)
        code, first = fn(qs)
        if code != 200:
            return self.send_json(code, first)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seq, last_rev, last_ping, started = 0, None, 0, time.time()
        body = first
        while time.time() - started < 900:
            if body.get("revision") != last_rev:
                seq += 1
                msg = clean({"type": "snapshot", "seq": seq, "data": body, "publishedAt": int(time.time() * 1000)})
                self.wfile.write(b"data: " + json.dumps(msg, separators=(",", ":"), allow_nan=False).encode() + b"\n\n")
                self.wfile.flush()
                last_rev, last_ping = body.get("revision"), time.time()
            elif time.time() - last_ping > 10:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                last_ping = time.time()
            time.sleep(2)
            code, nb = fn(qs)
            if code == 200:
                body = nb
        self.wfile.write(b'data: {"type":"reconnect","retryMs":500}\n\n')


LIVE = {}
ANALYTICS = {}


def status():
    return {name: {"markets": len(ch.markets_by_id), "dataUntil": ch.data_until, "lagSeconds": round(time.time() - ch.data_until / 1000),
                   "revision": ch.revision, "live": getattr(LIVE.get(name), "last_cycle", None),
                   "liveErrors": getattr(LIVE.get(name), "errors", None)} for name, ch in CH.items()}


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8895
    for name in CHAINS:
        if os.path.exists(f"{ROOT}/data/{name}/candles.duckdb"):
            CH[name] = Reader(name)
    from wallet import WalletIndex
    for name in CH:
        WALLETS[name] = WalletIndex(name)
    if "--no-analytics" not in sys.argv:
        for name, ch in CH.items():
            ANALYTICS[name] = emapi.AnalyticsJob(name, ch, log=log)
            ANALYTICS[name].start()
    if "--no-live" not in sys.argv:
        from live import LiveTail
        for name, ch in CH.items():
            LIVE[name] = LiveTail(name, log=log)
            LIVE[name].start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    log(f"em-charts listening on 127.0.0.1:{port} chains={list(CH)}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
