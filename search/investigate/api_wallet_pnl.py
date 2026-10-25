"""Contract checks for wallet-pnl/1 (/api/public/em/wallet-pnl-xc) through a wallet bridge, or em-investigate directly.
usage: python3 api_wallet_pnl.py http://127.0.0.1:8901 <wallet with PnL> <wallet holding an unpriced token | ->
       (em-investigate directly: http://127.0.0.1:8899 --direct). GET only. Exits 1 on any failure. The trader analytics of
       2026-10-19 have no row without a price (served markets only), so the unpriced checks take "-" and are skipped then."""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE, W, WU = sys.argv[1].rstrip("/"), sys.argv[2].lower(), sys.argv[3].lower()
PATH = "/wallet-pnl-xc" if "--direct" in sys.argv else "/api/public/em/wallet-pnl-xc"
ROW = {"chain", "token", "symbol", "market", "balance", "valueUsd", "price", "priceBasis", "firstBuyTs", "lastTradeTs", "buys", "sells",
       "avgCostUsd", "costBasis", "realizedUsd", "unrealizedUsd", "transferAdjusted", "transfersIn", "transfersOut", "verified"}
FAILS, N = [], [0]


def get(**kw):
    try:
        with urllib.request.urlopen(BASE + PATH + "?" + urllib.parse.urlencode(kw), timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, ok, detail=""):
    N[0] += 1
    print(("PASS " if ok else "FAIL ") + name + ((" · " + str(detail)[:220]) if detail else ""), flush=True)
    ok or FAILS.append(name)


s, d = get(address=W)
r = d.get("rows") or []
check("schema wallet-pnl/1 with rows", s == 200 and d.get("schema") == "wallet-pnl/1" and r, d.get("detail"))
check("every row carries the wallet-pnl/1 fields", all(ROW <= set(x) for x in r), [sorted(ROW - set(x)) for x in r][:1])
check("market id = <chain>:<token>", all(x["market"] == x["chain"] + ":" + x["token"] for x in r))
check("verified false always says why", all(x["verified"] or x["unverifiedReason"] for x in r))
check("totals and per-chain summaries", "totals" in d and {"realizedUsd", "unrealizedUsd", "unpricedRows", "partial"} <= set(d["totals"])
      and all({"chain", "asOf", "summary"} <= set(c) for c in d.get("chains", [])), d.get("totals"))
s, d1 = get(address=W, chain="arbitrum", limit="2")
check("chain filter and limit", s == 200 and {x["chain"] for x in d1["rows"]} <= {"arbitrum"} and len(d1["rows"]) <= 2)
if WU != "-":
    s, d = get(address=WU)
    u = [x for x in d.get("rows") or [] if x["price"] is None and (x["balance"] or 0) > 0]
    check("unpriced holding: value and unrealized are null, never 0", u and all(x["valueUsd"] is None and x["unrealizedUsd"] is None for x in u),
          u[:1])
    check("unpriced holdings make the totals partial", d.get("totals", {}).get("partial") is True and d["totals"]["unpricedRows"] >= len(u))
else:
    print("SKIP unpriced checks (no unpriced wallet given)")
check("priced rows never carry a null value", all(x["valueUsd"] is not None for x in r if x["price"] is not None))
s, d = get(address="0x123")
check("bad address -> 400", s == 400, d)
s, d = get(address=W, chain="base")
check("chain without trader analytics -> 400 that names the chains", s == 400 and "arbitrum" in d.get("detail", ""), d)
s, d = get(address="0x" + "00" * 19 + "01")
check("unknown wallet -> 200 with no rows", s == 200 and d.get("rows") == [] and d["totals"]["partial"] is False)
print(f"{N[0] - len(FAILS)}/{N[0]} passed")
sys.exit(1 if FAILS else 0)
