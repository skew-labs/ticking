#!/usr/bin/env python3
from pathlib import Path
path = Path("/srv/skew/em-charts/server.py")
text = path.read_text()

needle = "import emapi\n"
if "import sec_bars_read" not in text:
    if needle not in text:
        raise SystemExit("emapi import missing")
    text = text.replace(needle, needle + "import sec_bars_read\n", 1)

old = """def _candles_response(qs):
    agg = qs.get(\"aggregation\", \"pool\")
    period_s, label = parse_interval(qs.get(\"interval\", \"1m\"))
    limit = max(1, min(600, int(qs.get(\"limit\", \"180\"))))
    before = qs.get(\"before\")
    gap = qs.get(\"gap_policy\", \"carry\")
    if agg == \"token\":
"""

new = """def _candles_response(qs):
    agg = qs.get(\"aggregation\", \"pool\")
    period_s, label = parse_interval(qs.get(\"interval\", \"1m\"))
    limit = max(1, min(600, int(qs.get(\"limit\", \"180\"))))
    before = qs.get(\"before\")
    gap = qs.get(\"gap_policy\", \"carry\")
    # 초봉 2026-10-23: Binance-style 1s store (sec-bars). Not tip/hot SoT.
    if period_s < 60:
        return _candles_response_sec(qs, period_s, label, limit, before, gap, agg)
    if agg == \"token\":
"""

if "_candles_response_sec" not in text:
    if old not in text:
        raise SystemExit("cannot find _candles_response head")
    text = text.replace(old, new, 1)

helper = r'''
def _candles_response_sec(qs, period_s, label, limit, before, gap, agg):
    """Serve 1s..59s from sec-bars (Binance 1s mirror). Sol+Arb+RH mapped majors only."""
    if agg not in ("token", "pool"):
        return 400, {"detail": "aggregation must be token or pool for 1s."}
    net = qs.get("network")
    mid, meta = sec_bars_read.resolve_sec_market(net, qs.get("mint"), qs.get("market"))
    if not mid:
        return 404, {"detail": "No 1s series for this market yet (sec-bars map). Use a CEX-mapped Sol/Arb/RH liquid, e.g. WETH/SOL.",
                     "hint": "Mapped majors only until Fast Feed / broader tick coverage."}
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
    body = {"mode": "sec_bars_1s", "aggregation": "token", "market": mid, "pool": None, "mint": mid,
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
                         "priceSampling": "Binance 1s OHLCV remapped to chain token id; Sol may blend oracle-fast close tip."}}
    return 200, body


'''

if "def _candles_response_sec" not in text:
    marker = "def candles_response(qs):"
    if marker not in text:
        raise SystemExit("candles_response missing")
    text = text.replace(marker, helper + marker, 1)

old_mr = """def market_row(ch, m):
    # chart-cache 2026-10-23: liquid (hot-touched / stock / anchor) ages follow chain tip when recently printed
    last_ms = m[\"last_time\"] or 0
    kind = m.get(\"kind\") or \"token\"
    recently = last_ms and (ch.data_until - last_ms) < 2 * 3600 * 1000
    liquid = kind in (\"stock\", \"anchor\") or ((m.get(\"volume_24h\") or 0) > 0 and recently)
    updated = (ch.data_until // 1000) if liquid and recently else (last_ms // 1000)
    return {\"id\": m[\"id\"], \"symbol\": m[\"symbol\"], \"name\": m[\"name\"], \"mint\": m[\"id\"], \"address\": m[\"token\"], \"network\": ch.name,
            \"kind\": m[\"kind\"], \"price\": m[\"last_price\"], \"change\": None if m[\"change_24h\"] is None else m[\"change_24h\"] * 100,
"""

new_mr = """_MS_QUOTES = {\"at\": 0.0, \"data\": {}}

def _ms_quotes():
    if time.time() - _MS_QUOTES[\"at\"] < 0.5 and _MS_QUOTES[\"data\"]:
        return _MS_QUOTES[\"data\"]
    try:
        with open(\"/srv/skew/em-realtime/sec-bars/out/quotes.json\") as f:
            _MS_QUOTES[\"data\"] = json.load(f)
            _MS_QUOTES[\"at\"] = time.time()
    except (OSError, ValueError):
        pass
    return _MS_QUOTES[\"data\"]

def market_row(ch, m):
    # chart-cache 2026-10-23: liquid (hot-touched / stock / anchor) ages follow chain tip when recently printed
    last_ms = m[\"last_time\"] or 0
    kind = m.get(\"kind\") or \"token\"
    recently = last_ms and (ch.data_until - last_ms) < 2 * 3600 * 1000
    liquid = kind in (\"stock\", \"anchor\") or ((m.get(\"volume_24h\") or 0) > 0 and recently)
    updated = (ch.data_until // 1000) if liquid and recently else (last_ms // 1000)
    price = m[\"last_price\"]
    q = (_ms_quotes().get(\"quotes\") or {}).get(m[\"id\"]) or (_ms_quotes().get(\"quotes\") or {}).get(m[\"id\"].lower())
    if q and q.get(\"price\") and (q.get(\"age_s\") is None or q[\"age_s\"] < 15):
        price = q[\"price\"]
        updated = int(q[\"t_ms\"] // 1000)
    return {\"id\": m[\"id\"], \"symbol\": m[\"symbol\"], \"name\": m[\"name\"], \"mint\": m[\"id\"], \"address\": m[\"token\"], \"network\": ch.name,
            \"kind\": m[\"kind\"], \"price\": price, \"change\": None if m[\"change_24h\"] is None else m[\"change_24h\"] * 100,
"""

if "_ms_quotes" not in text:
    if old_mr not in text:
        raise SystemExit("market_row block missing")
    text = text.replace(old_mr, new_mr, 1)

path.write_text(text)
print("patched", path, "size", path.stat().st_size)
