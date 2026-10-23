import json
from fastapi.testclient import TestClient
from server import app as api
from server import store
from server.fast_feed import MAX_BODY, decode_body, edge_base, load

T0 = 1_700_000_000_000


def tip(time=T0, **fields):
    row = {'time': time, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 1}
    row.update(fields)
    return row


def test_edge_base_accepts_loopback_and_https(monkeypatch):
    monkeypatch.setenv('TICKING_CHARTS_EDGE', 'http://127.0.0.1:8895')
    assert edge_base() == 'http://127.0.0.1:8895'
    monkeypatch.setenv('TICKING_CHARTS_EDGE', 'https://charts.example/feed/')
    assert edge_base() == 'https://charts.example/feed'
    monkeypatch.setenv('TICKING_CHARTS_EDGE', 'http://charts.example')
    assert edge_base() == ''
    monkeypatch.setenv('TICKING_CHARTS_EDGE', 'https://' + 'user' + ':' + 'pw' + '@charts.example')
    assert edge_base() == ''
    monkeypatch.delenv('TICKING_CHARTS_EDGE', raising=False)
    assert edge_base() == ''


def test_missing_edge_is_fail_soft(monkeypatch):
    monkeypatch.delenv('TICKING_CHARTS_EDGE', raising=False)
    body, status = load(chain='arbitrum', symbol='WETH')
    assert status == 503
    assert body['coverage'] == 'fail_soft'
    assert body['reason'] == 'charts-edge-unavailable'
    assert body['bars_1s'] == [] and body['wickRejects'] == []


def test_solana_does_not_call_the_edge(monkeypatch):
    monkeypatch.setenv('TICKING_CHARTS_EDGE', 'http://127.0.0.1:8895')
    called = []
    body, status = load(chain='solana', transport=lambda url, query: called.append(url) or (200, {'bars_1s': [tip()]}))
    assert status == 200 and body['reason'] == 'solana-not-tip' and called == []


def test_upstream_failure_and_empty_stay_fail_soft(monkeypatch):
    monkeypatch.setenv('TICKING_CHARTS_EDGE', 'http://127.0.0.1:8895')
    body, status = load(chain='rh', transport=lambda url, query: (500, None))
    assert status == 503 and body['coverage'] == 'fail_soft'
    body, status = load(chain='arbitrum', transport=lambda url, query: (200, {'bars_1s': [], 'wickRejects': []}))
    assert status == 503 and body['reason'] == 'tip-empty'
    body, status = load(chain='arbitrum', interval='1m', transport=lambda url, query: (_ for _ in ()).throw(AssertionError('fetched')))
    assert status == 422


def test_wick_clamp_binance_and_rejects(monkeypatch):
    monkeypatch.setenv('TICKING_CHARTS_EDGE', 'https://charts.example')
    seen = {}

    def transport(url, query):
        seen['url'] = url
        seen['query'] = query
        return 200, {
            'bars_1s': [
                tip(high=200),
                {'time': T0 + 7000, 'open': 10, 'high': 10.1, 'low': 9.9, 'close': 10.2},
                {'time': T0 + 1000, 'px': 100},
                {'time': T0 + 2000, 'open': 100 * 1e8, 'high': 400 * 1e8, 'low': 1 * 1e8, 'close': 100 * 1e8, 'scale': 1e8, 'volume': 2},
                {'time': T0 + 3000, 'open': 2, 'high': 20, 'low': 1, 'close': 2, 'volume': 4, 'source': 'binance_1s'},
                {'time': T0 + 4000, 'px': 1, 'symbol': 'PEPE'},
            ],
            'wickRejects': [
                {'time': T0 + 5000, 'open': 100, 'high': 200, 'low': 50, 'close': 100, 'symbol': 'WETHUSD'},
                {'time': T0 + 6000, 'px': 3, 'symbol': 'PEPE'},
            ],
        }

    body, status = load(chain='arb', symbol='WETH', mint='0xabc', limit=120, transport=transport)
    assert status == 200
    assert body['coverage'] == 'tip_hot' and body['latencyPaintOnly'] is True and body['failSoft'] is True
    assert 'chain=arbitrum' in seen['url'] and 'coverage=tip_hot' in seen['url'] and 'interval=1s' in seen['url']
    assert seen['query']['symbol'] == 'WETH' and seen['query']['mint'] == '0xabc'
    bars = {row['time']: row for row in body['bars_1s']}
    assert bars[T0]['high'] == 105 and bars[T0]['low'] == 100 and bars[T0]['source'] == 'tip_hot'
    assert bars[T0 + 7000]['high'] == 10.2 and bars[T0 + 7000]['low'] == 9.9
    assert bars[T0 + 1000]['open'] == 100 and bars[T0 + 1000]['close'] == 100 and bars[T0 + 1000]['source'] == 'tip_hot'
    assert bars[T0 + 2000]['high'] == 105 and bars[T0 + 2000]['low'] == 95 and bars[T0 + 2000]['volume'] == 2
    assert bars[T0 + 3000]['source'] == 'binance_1s' and bars[T0 + 3000]['high'] == 20 and bars[T0 + 3000]['latencyPaintOnly'] is False
    assert T0 + 4000 not in bars
    assert body['wickRejects'][0]['high'] == 105 and body['wickRejects'][0]['low'] == 95
    assert all(row.get('symbol') != 'PEPE' for row in body['wickRejects'])
    binance = bars[T0 + 3000]
    assert binance['open'] == 2 and binance['close'] == 2 and binance['low'] == 1


def test_same_second_binance_is_not_overwritten(monkeypatch):
    monkeypatch.setenv('TICKING_CHARTS_EDGE', 'http://127.0.0.1:8895')
    body, status = load(chain='robinhood', transport=lambda url, query: (200, {'bars_1s': [
        tip(high=200),
        {'time': T0, 'open': 2, 'high': 20, 'low': 1, 'close': 2, 'source': 'binance_1s'},
    ]}))
    assert status == 200
    assert [row['source'] for row in body['bars_1s']] == ['tip_hot', 'binance_1s']
    assert body['bars_1s'][0]['high'] == 105
    assert body['bars_1s'][1]['high'] == 20


def test_output_is_capped_and_oversize_is_fail_soft(monkeypatch):
    monkeypatch.setenv('TICKING_CHARTS_EDGE', 'http://127.0.0.1:8895')
    rows = [{'time': T0 + i * 1000, 'px': 100} for i in range(5000)]
    body, status = load(chain='arbitrum', limit=120, transport=lambda url, query: (200, {'bars_1s': rows}))
    assert status == 200 and len(body['bars_1s']) == 120
    assert body['bars_1s'][0]['time'] == T0 + (5000 - 120) * 1000
    assert decode_body(b'{' + b'x' * (MAX_BODY + 1)) == (503, None)
    assert decode_body(json.dumps({'bars_1s': [tip()]}).encode())[0] == 200


def test_public_route_keeps_series_until_a_real_tip(tmp_path, monkeypatch):
    monkeypatch.setenv('TICKING_MARKETS_ENABLED', 'false')
    monkeypatch.setenv('TICKING_DB', str(tmp_path / 'console.sqlite3'))
    monkeypatch.delenv('TICKING_CHARTS_EDGE', raising=False)
    monkeypatch.setattr(store, 'DB_PATH', tmp_path / 'console.sqlite3')
    with TestClient(api.app) as client:
        missing = client.get('/api/public/engine/fast-feed', params={'chain': 'arbitrum', 'symbol': 'WETH', 'interval': '1s', 'coverage': 'tip_hot'})
        assert missing.status_code == 503
        assert missing.json()['bars_1s'] == [] and missing.json()['wickRejects'] == []
        assert missing.headers['cache-control'] == 'no-store'
        assert missing.headers['access-control-allow-origin'] == '*'
        monkeypatch.setenv('TICKING_CHARTS_EDGE', 'http://127.0.0.1:8895')
        monkeypatch.setattr('server.fast_feed.http_get', lambda url: (200, {
            'bars_1s': [{'time': T0, 'px': 100, 'symbol': 'WETH'}],
            'wickRejects': [{'time': T0 + 1000, 'open': 100, 'high': 200, 'low': 100, 'close': 100}],
        }))
        live = client.get('/api/public/engine/fast-feed', params={'chain': 'arbitrum', 'symbol': 'WETH', 'interval': '1s', 'coverage': 'tip_hot', 'limit': 2})
        assert live.status_code == 200
        assert live.headers['cache-control'] == 'no-store'
        payload = live.json()
        assert payload['coverage'] == 'tip_hot'
        assert payload['bars_1s'][0]['close'] == 100
        assert payload['wickRejects'][0]['high'] == 105
        sol = client.get('/api/public/engine/fast-feed', params={'chain': 'solana'})
        assert sol.status_code == 200 and sol.json()['reason'] == 'solana-not-tip'
