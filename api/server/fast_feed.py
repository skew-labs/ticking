"""Public Fast Feed tip read.

Latency paint for Arbitrum and Robinhood. A missing or oversized charts-edge
body is fail_soft so the chart keeps its series. This module never invents
prices and never clamps a binance_1s wick. The read is bounded: 120 bars,
2000 samples, 256 KiB, one short connection, no kept pool.
"""
import json
import os
from urllib.parse import urlencode, urlsplit

WICK_BAND = 0.05
MAX_LIMIT = 120
MAX_SAMPLES = 2000
MAX_REJECTS = 480
MAX_BODY = 256 * 1024
TIMEOUT = 1.2
APPLIED_KEYS = ('bars_1s', 'candles', 'bars', 'prices', 'prints', 'samples', 'ticks')
REJECT_KEYS = ('wickRejects', 'rejected', 'rejects')
CHAIN = {'arb': 'arbitrum', 'arbitrum': 'arbitrum', 'rh': 'robinhood', 'robinhood': 'robinhood', 'sol': 'solana', 'solana': 'solana'}


def chain_key(value):
    text = ''.join(ch for ch in str(value or '').lower() if ch.isalnum())
    return CHAIN.get(text, text)


def is_tip_chain(chain):
    return chain_key(chain) in {'arbitrum', 'robinhood'}


def round_px(number):
    return round(number * 1e8) / 1e8


def _num(value):
    if isinstance(value, bool) or value is None or value == '':
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def bar_time(raw):
    if isinstance(raw, dict):
        value = None
        for key in ('time', 't', 'ts', 'timestamp'):
            if raw.get(key) not in (None, ''):
                value = raw.get(key)
                break
    else:
        value = raw
    if value in (None, ''):
        return None
    number = _num(value)
    if number is None or number <= 0:
        if not isinstance(value, str):
            return None
        from datetime import datetime
        try:
            number = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000
        except ValueError:
            return None
    if number >= 1e18:
        number /= 1e6
    elif number >= 1e15:
        number /= 1e3
    elif number < 1e12:
        number *= 1000
    return int(number // 1000) * 1000


def read_num(raw, keys):
    if not isinstance(raw, dict):
        return None
    for key in keys:
        number = _num(raw.get(key))
        if number is not None:
            return number
    return None


def is_binance(bar):
    if not isinstance(bar, dict):
        return False
    if bar.get('source') == 'binance_1s' or bar.get('venue') == 'binance_1s':
        return True
    sources = bar.get('sources')
    return isinstance(sources, list) and 'binance_1s' in sources


def ohlc_ok(bar):
    open_ = _num(bar.get('open'))
    high = _num(bar.get('high'))
    low = _num(bar.get('low'))
    close = _num(bar.get('close'))
    if not (open_ and high and low and close and open_ > 0 and high > 0 and low > 0 and close > 0):
        return None
    if not (low <= min(open_, close) and max(open_, close) <= high):
        return None
    volume = _num(bar.get('volume'))
    if volume is None or volume < 0:
        volume = 0
    return {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume}


def decode_tip_sample(raw):
    if isinstance(raw, list):
        time = bar_time(raw[0] if raw else None)
        if time is None:
            return None
        nums = []
        for value in raw[1:6]:
            number = _num(value)
            if number is None:
                break
            nums.append(number)
        if len(nums) >= 4 and nums[0] > 0 and nums[3] > 0:
            volume = nums[4] if len(nums) >= 5 and nums[4] >= 0 else 0
            return {'time': time, 'open': nums[0], 'high': nums[1], 'low': nums[2], 'close': nums[3], 'volume': volume}
        if len(nums) >= 1 and nums[0] > 0:
            return {'time': time, 'open': nums[0], 'high': nums[0], 'low': nums[0], 'close': nums[0], 'volume': 0}
        return None
    if not isinstance(raw, dict):
        return None
    time = bar_time(raw)
    if time is None:
        return None
    scale = read_num(raw, ('scale', 'pxScale'))
    div = scale if scale and scale > 1 else 1

    def scaled(keys):
        number = read_num(raw, keys)
        return None if number is None else number / div

    open_ = scaled(('open', 'o'))
    close = scaled(('close', 'c'))
    px = scaled(('px', 'price', 'p', 'mid', 'last', 'mark'))
    bid = scaled(('bid', 'b'))
    ask = scaled(('ask', 'a'))
    if open_ is not None and not open_ > 0:
        return None
    if close is not None and not close > 0:
        return None
    body_open = open_
    body_close = close if close is not None else (px if px and px > 0 else None)
    if not (body_close and body_close > 0) and bid and ask and bid > 0 and ask > 0:
        body_close = (bid + ask) / 2
    if not (body_open and body_open > 0):
        body_open = body_close if body_close and body_close > 0 else (px if px and px > 0 else None)
    if not (body_close and body_close > 0):
        body_close = body_open
    if not (body_open and body_close and body_open > 0 and body_close > 0):
        return None
    high = scaled(('high', 'h'))
    low = scaled(('low', 'l'))
    if not (high and high > 0) and not (low and low > 0) and bid and ask and bid > 0 and ask > 0:
        high = max(bid, ask, body_open, body_close)
        low = min(bid, ask, body_open, body_close)
    if not (high and high > 0):
        high = max(body_open, body_close)
    if not (low and low > 0):
        low = min(body_open, body_close)
    volume = read_num(raw, ('volume', 'v', 'vol', 'qty', 'size'))
    if volume is None or volume < 0:
        volume = 0
    return {'time': time, 'open': body_open, 'high': high, 'low': low, 'close': body_close, 'volume': volume}


def finish_tip_bar(sample, band=WICK_BAND):
    open_ = sample['open']
    close = sample['close']
    top = max(open_, close)
    bot = min(open_, close)
    high = sample['high']
    low = sample['low']
    if not (high and high > 0):
        high = top
    if not (low and low > 0):
        low = bot
    if high < low:
        high, low = low, high
    if high < top:
        high = top
    if low > bot:
        low = bot
    high = min(high, round_px(top * (1 + band)))
    low = max(low, round_px(bot * (1 - band)))
    if not (low > 0 and high > 0 and low <= bot and top <= high):
        return None
    volume = sample['volume'] if sample['volume'] >= 0 else 0
    return {
        'time': sample['time'], 'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume,
        'source': 'tip_hot', 'coverage': 'tip_hot', 'latencyPaintOnly': True, 'synthetic': False,
    }


def sanitize_tip_bar(raw, band=WICK_BAND):
    decoded = decode_tip_sample(raw)
    if not decoded:
        return None
    return finish_tip_bar(decoded, band)


def norm_symbol(value):
    return ''.join(ch for ch in str(value or '').upper() if ch.isalnum())[:16]


def same_market(raw, symbol, mint):
    if not isinstance(raw, dict):
        return True
    sym = raw.get('symbol', raw.get('ticker', raw.get('pair')))
    ident = raw.get('mint', raw.get('address', raw.get('token')))
    if symbol and sym:
        want, got = norm_symbol(symbol), norm_symbol(sym)
        if want and got and want != got and not got.startswith(want):
            return False
    if mint and ident and str(ident).lower() != str(mint).lower():
        return False
    return True


def edge_base(environ=None):
    raw = (environ if environ is not None else os.environ).get('TICKING_CHARTS_EDGE', '').strip()
    if not raw:
        return ''
    parts = urlsplit(raw)
    if parts.username or parts.password or parts.query or parts.fragment or parts.scheme not in {'http', 'https'} or not parts.hostname:
        return ''
    if parts.scheme == 'http' and parts.hostname not in {'127.0.0.1', 'localhost'}:
        return ''
    return raw.rstrip('/')


def _take(obj, keys, symbol, mint, dest):
    if not isinstance(obj, dict):
        return
    for key in keys:
        rows = obj.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        for row in rows[max(0, len(rows) - MAX_SAMPLES):]:
            if same_market(row, symbol, mint):
                dest.append(row)


def collect(body, symbol, mint):
    applied, rejects = [], []
    if isinstance(body, list):
        for row in body[-MAX_SAMPLES:]:
            if same_market(row, symbol, mint):
                applied.append(row)
        return applied, rejects
    _take(body, APPLIED_KEYS, symbol, mint, applied)
    _take(body, REJECT_KEYS, symbol, mint, rejects)
    data = body.get('data') if isinstance(body, dict) else None
    _take(data, APPLIED_KEYS, symbol, mint, applied)
    _take(data, REJECT_KEYS, symbol, mint, rejects)
    return applied[-MAX_SAMPLES:], rejects[-MAX_SAMPLES:]


def shape(body, symbol, mint, limit):
    applied, rejects = collect(body, symbol, mint)
    tips, venue, kept = [], [], []
    for raw in applied:
        if is_binance(raw):
            px = ohlc_ok(raw)
            time = bar_time(raw)
            if not px or time is None:
                continue
            venue.append({
                'time': time, 'open': px['open'], 'high': px['high'], 'low': px['low'], 'close': px['close'],
                'volume': px['volume'], 'source': 'binance_1s', 'synthetic': False, 'latencyPaintOnly': False,
            })
        else:
            tip = sanitize_tip_bar(raw)
            if tip:
                tips.append(tip)
    for raw in rejects:
        if is_binance(raw):
            continue
        tip = sanitize_tip_bar(raw)
        if tip:
            kept.append(tip)
    venue = venue[-limit:]
    tips = tips[-max(0, limit - len(venue)):]
    return {
        'ok': True,
        'coverage': 'tip_hot',
        'latencyPaintOnly': True,
        'failSoft': True,
        'interval': '1s',
        'bars_1s': tips + venue,
        'wickRejects': kept[-MAX_REJECTS:],
    }


def fail(reason, status=503):
    return {
        'ok': False, 'coverage': 'fail_soft', 'reason': reason, 'latencyPaintOnly': True, 'failSoft': True,
        'bars_1s': [], 'wickRejects': [],
    }, status


def idle(reason):
    return {
        'ok': True, 'coverage': 'idle', 'reason': reason, 'latencyPaintOnly': True, 'failSoft': True,
        'bars_1s': [], 'wickRejects': [],
    }, 200


def decode_body(blob):
    if blob is None or len(blob) > MAX_BODY:
        return 503, None
    try:
        return 200, json.loads(blob)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 503, None


def http_get(url):
    import httpx
    chunks = []
    size = 0
    try:
        timeout = httpx.Timeout(TIMEOUT)
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=0)
        with httpx.Client(timeout=timeout, follow_redirects=False, limits=limits) as client:
            with client.stream('GET', url, headers={'Accept': 'application/json'}) as res:
                if res.status_code != 200:
                    return res.status_code, None
                for chunk in res.iter_bytes(chunk_size=8192):
                    size += len(chunk)
                    if size > MAX_BODY:
                        return 503, None
                    chunks.append(chunk)
    except httpx.HTTPError:
        return 503, None
    return decode_body(b''.join(chunks))


def _clip(value, length):
    text = '' if value is None else str(value).strip()
    return text[:length]


def load(*, chain='', symbol=None, mint=None, interval='1s', coverage='tip_hot', limit=120, transport=None, environ=None):
    del coverage
    key = chain_key(chain)
    if key == 'solana':
        return idle('solana-not-tip')
    if not is_tip_chain(key):
        return idle('not-tip')
    if interval not in (None, '', '1s'):
        return fail('interval-not-1s', 422)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = MAX_LIMIT
    limit = min(MAX_LIMIT, max(1, limit))
    symbol = _clip(symbol, 32) or None
    mint = _clip(mint, 64) or None
    base = edge_base(environ)
    if transport is None and not base:
        return fail('charts-edge-unavailable')
    query = {'chain': key, 'interval': '1s', 'coverage': 'tip_hot', 'limit': str(limit)}
    if symbol:
        query['symbol'] = symbol
    if mint:
        query['mint'] = mint
    url = f'{base}/api/public/engine/fast-feed?{urlencode(query)}' if base else ''
    try:
        status, body = transport(url, query) if transport is not None else http_get(url)
    except Exception:
        return fail('charts-edge-unavailable')
    if status != 200 or not isinstance(body, (dict, list)):
        return fail('charts-edge-unavailable')
    shaped = shape(body, symbol, mint, limit)
    if not shaped['bars_1s'] and not shaped['wickRejects']:
        return fail('tip-empty')
    return shaped, 200
