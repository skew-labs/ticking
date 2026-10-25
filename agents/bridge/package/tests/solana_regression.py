"""Solana / exchange regression: production console (:19428) vs the staging console (:19438), same engine stores.

usage: python3 solana_regression.py [prod_base] [staging_base]   (run on the Canada host; GET only)
       python3 solana_regression.py --record FILE [base]           before a production change
       python3 solana_regression.py --compare FILE [base]          after it: the same closed windows, byte-identical
Closed windows (an explicit `before` cursor) must be byte-identical after removing per-request clocks.
Catalog rows must keep the same identities and static fields; the one intended difference is the explicit
`network: "solana"` on Solana rows (previously absent). Exits 1 on any difference.
"""
import json
import sys
import time
import urllib.error
import urllib.request

MODE = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ('--record', '--compare') else None
ARGS = sys.argv[3:] if MODE else sys.argv[1:]
PROD = ARGS[0] if ARGS else 'http://127.0.0.1:19428'
STAGE = ARGS[1] if len(ARGS) > 1 else 'http://127.0.0.1:19438'
SAVED = json.load(open(sys.argv[2])) if MODE == '--compare' else {}
RECORD = {}
SOL = 'So11111111111111111111111111111111111111112'
CLOCKS = {'sequence', 'revision', 'cursor', 'stale', 'retainedSnapshot', 'refresh_seconds', 'engineThrough', 'observedThrough', 'updated_at',
          'measuredAtMs', 'generated_at', 'fetched_at', 'checkedAt', 'end', 'recordedThrough', 'sourceThrough', 'liveThrough'}   # moving edges, not window data
results = []


def get(base, path):
    for attempt in range(6):   # both consoles answer 503 + Retry-After while a bounded read lane is busy
        try:
            with urllib.request.urlopen(base + path, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 503 or attempt == 5:
                raise
            time.sleep(5)


def scrub(v):
    if isinstance(v, dict):
        return {k: scrub(x) for k, x in v.items() if k not in CLOCKS}
    if isinstance(v, list):
        return [scrub(x) for x in v]
    return v


def same(name, path, pick=lambda d: d):
    for attempt in range(3):
        a = scrub(pick(get(PROD, path)))
        if MODE == '--record':
            RECORD[path] = a; b = a; break
        b = SAVED.get(path) if MODE == '--compare' else scrub(pick(get(STAGE, path)))
        if a == b:
            break
        time.sleep(2)
    ok = a == b
    results.append({'check': name, 'pass': ok, 'path': path, 'bytes': len(json.dumps(a, sort_keys=True))})
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)
    if not ok:
        keys = sorted(set(a) | set(b)) if isinstance(a, dict) and isinstance(b, dict) else []
        print('   differing keys:', [k for k in keys if a.get(k) != b.get(k)][:12])


before = SAVED.get('before') or (int(time.time()) // 86400 - 2) * 86400 * 1000   # a closed window two UTC days back
RECORD['before'] = before
for agg, interval, extra in (('token', '1h', '&history=full'), ('token', '15m', '&history=executions'), ('token', '1d', '&history=full&gap_policy=carry'),
                             ('pool', '1m', '&limit_hint=')):
    same('SOL %s %s candles (closed window)' % (agg, interval),
         '/api/public/engine/candles?mint=%s&aggregation=%s&interval=%s&limit=%d&before=%d%s' % (SOL, agg, interval, 120 if agg == 'pool' else 300, before, extra.replace('&limit_hint=', '')))
same('BTC exchange reference candles (closed window)', '/api/public/engine/candles?market=BTC&aggregation=exchange&interval=1h&limit=200&before=%d' % before)
same('SOL markets/<id>/candles hour frame', '/api/public/markets/SOL/candles?frame=hour&before=%d' % before)
same('SOL token tape shape', '/api/public/engine/trades?mint=%s&aggregation=token&limit=5' % SOL,
     lambda d: {'identity': d.get('identity'), 'fields': sorted(d['trades'][0]) if d.get('trades') else None, 'source': d.get('source')})

prod = {t['id']: t for t in get(PROD, '/api/public/markets?view=table')['tokens']}
if MODE == '--record':
    RECORD['catalog'] = prod
stage = SAVED['catalog'] if MODE == '--compare' else prod if MODE == '--record' else {t['id']: t for t in get(STAGE, '/api/public/markets?view=table')['tokens'] if t.get('network') not in ('arbitrum', 'robinhood')}
if MODE == '--compare':
    prod, stage = stage, prod   # baseline first: Solana rows may only gain network "solana"
static = ('symbol', 'name', 'mint', 'icon', 'category', 'coingecko', 'verification', 'marketType', 'market', 'source', 'venue', 'quote')
ids_ok = set(prod) == set(stage)
rows_ok = all({k: prod[i].get(k) for k in static} == {k: stage[i].get(k) for k in static} for i in prod if i in stage)
net_ok = all(stage[i].get('network') == (prod[i].get('network') or 'solana') for i in prod if i in stage)
results.append({'check': 'catalog: same Solana/exchange identities and static fields', 'pass': ids_ok and rows_ok, 'rows': len(prod)})
results.append({'check': 'catalog: Solana rows gain only an explicit network "solana"', 'pass': net_ok})
print(('PASS' if ids_ok and rows_ok else 'FAIL'), 'catalog identities/static fields', len(prod), len(stage))
print(('PASS' if net_ok else 'FAIL'), 'catalog explicit network on Solana rows')
failed = sum(not r['pass'] for r in results)
print(json.dumps({'passed': len(results) - failed, 'failed': failed}))
json.dump(results, open('/tmp/solana-regression.json', 'w'), indent=1)
if MODE == '--record':
    json.dump(RECORD, open(sys.argv[2], 'w'))
sys.exit(1 if failed else 0)
