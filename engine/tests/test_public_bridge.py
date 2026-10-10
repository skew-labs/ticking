"""The public export's retained reader boundary uses only isolated inputs."""
import importlib
import json
from pathlib import Path
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

MINT = 'So11111111111111111111111111111111111111112'


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    secret = tmp_path / 'capability.txt'
    secret.write_text('isolated-bridge-test-capability')
    monkeypatch.setenv('TICKING_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('TICKING_BRIDGE_SECRET_FILE', str(secret))
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    sys.modules.pop('ticking_bridge', None)
    module = importlib.import_module('ticking_bridge')
    server = module.Server(('127.0.0.1', 0), module.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def get(path, body=None, authorized=True):
        headers = {'Authorization': 'Bearer isolated-bridge-test-capability'} if authorized else {}
        data = json.dumps(body).encode() if body is not None else None
        request = Request(f'http://127.0.0.1:{server.server_port}{path}', data=data, headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            error.close()
            return error.code, None

    yield get
    server.shutdown()
    server.server_close()
    worker.join(2)
    module.commits.close()
    sys.modules.pop('ticking_bridge', None)


def test_capability_and_empty_retained_catalog(bridge):
    assert bridge('/hyperliquid/catalog', authorized=False)[0] == 401
    assert bridge('/hyperliquid/catalog') == (200, {'markets': []})


def test_watch_admission_does_not_invent_verified_market(bridge):
    status, watch = bridge('/watch/register', {
        'mint': MINT, 'owner': 'offline-test-owner', 'kind': 'preview', 'expires': int(time.time()) + 120,
    })
    assert status == 200 and watch['active'] and not watch['verified']
    assert watch['state'] == 'queued' and watch['pools'] == []
    assert bridge('/watch/cancel', {'mint': MINT, 'owner': 'offline-test-owner'})[0] == 200
    assert not bridge('/watch/status?mint=' + MINT)[1]['active']


def test_change_cursor_and_history_boundary(bridge):
    status, cursor = bridge('/changes?wait=0')
    assert status == 200 and cursor['boot'] and cursor['seq'] >= 0
    assert bridge('/changes?wait=16')[0] == 422
    assert bridge('/token/candles?token=invalid')[0] == 422
    assert bridge('/reference/candles?market=BTC')[0] == 422
