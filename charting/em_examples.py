"""Fetch and summarize the five EM endpoints for given markets (dev helper)."""
import json
import sys
import urllib.request

B = "http://127.0.0.1:8895/api/public/em/"


def get(p):
    return json.load(urllib.request.urlopen(B + p))


for m in sys.argv[1:]:
    s = get(f"token-state?market={m}")
    print(f"=== {m} {s['symbol']} price {s['price']:.4g} supply {s['supply']:.6g} holders {s['holders']} asOf {s['asOf']} attributed {s['coverage']['attributedShare']:.3f}")
    for b in s["buckets"]:
        print(f"   {b['key']:22} {b['share'] * 100:5.1f}%  {b['usd'] or 0:>14,.0f} USD  wallets {b['wallets']}  d24h {b['d24h']}"
              + (f"  [{', '.join(p['name'] + ' ' + format(p['amount'], '.4g') for p in b['protocols'][:3])}]" if b['protocols'] else ""))
    print("   claims:", [(c["name"], round(c["collateral"] or 0, 2), round(c["borrowed"] or 0, 2)) for c in s["lendingClaims"][:3]])
    lq = get(f"liquidations?market={m}")
    t = lq["totals"]
    print(f"   liquidations: {t['positions']} positions, collateral ${t['collateralUsd']:,.0f}, debt ${t['debtUsd']:,.0f}; atRisk {t['atRiskPositions']} "
          f"(${t['atRiskCollateralUsd']:,.0f}); excluded deep-underwater {t['excludedDeepUnderwater']}")
    for b in lq["ladder"][:5]:
        print(f"     bin ${b['priceLow']:.4g}-{b['price']:.4g}: ${b['collateralUsd']:,.0f} ({b['positions']} pos) cum ${b['cumulativeUsd']:,.0f}")
    for x in lq["positions"][:3]:
        print(f"     nearest {x['wallet'][:10]} {x['protocol']} liq ${x['liqPrice']:.4g} ({x['distancePct']:.1f}%) coll {x['collateralAmount']:.4g} debt ${x['debtUsd']:,.0f} {x['debtSymbol']} {x['thresholdKind']}={x['threshold']}")
    f = get(f"flows?market={m}&window=24h")
    print("   flows24:", [(c["class"], round(c["netUsd"]), c["trades"]) for c in f["classes"]], "lending", {k: round(v or 0) for k, v in f["lending"].items() if k.endswith("Usd")},
          "bridges", {k: round(v or 0) for k, v in f["bridges"].items()})
    e = get(f"events?market={m}&limit=3")
    for x in e["events"]:
        print("   event", x["time"], x["kind"], round(x["usd"] or 0), "|", x["text"])
    br = get(f"token-brief?market={m}")
    print("   headline:", br["headline"])
    for x in br["facts"]:
        print("     fact", x["key"], x["value"], x["unit"], "|", x["text"])
