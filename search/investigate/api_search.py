"""Contract checks for search/1 through a wallet bridge.
usage: python3 api_search.py http://127.0.0.1:8901   (GET only). Exits 1 on any failure."""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/")
TYPES = {"wallet", "token", "protocol", "tx", "market"}
FAILS, N = [], [0]


def get(q, **kw):
    try:
        with urllib.request.urlopen(BASE + "/api/public/em/search?" + urllib.parse.urlencode({"q": q, **kw}), timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, ok, detail=""):
    N[0] += 1
    print(("PASS " if ok else "FAIL ") + name + ((" · " + str(detail)[:220]) if detail else ""), flush=True)
    ok or FAILS.append(name)


def rows(q, **kw):
    s, d = get(q, **kw)
    return s, d, d.get("results") or []


s, d, r = rows("nvda", limit=5)
check("schema search/1 and row contract", s == 200 and d.get("schema") == "search/1" and all(
    x["type"] in TYPES and "title" in x and "subtitle" in x and "route" in x and "id" in x for x in r), d.get("detail"))
check("labels never carry a confidence number", "confidence" not in json.dumps(d))
check("NVDA -> the Robinhood stock market, reviewed", r and r[0]["type"] == "token" and r[0]["title"] == "NVDA"
      and r[0]["route"] == "#/console/workspace/" + r[0]["id"] and r[0]["label"]["verified"] is True, r[:1])
_, _, r = rows("aave v3")
check("protocol by name -> #/protocol/<chain>/<id>, registry-named", any(x["type"] == "protocol" and x["route"] == "#/protocol/arbitrum/aave-v3"
      and x["label"]["source"] == "registry" and x["label"]["verified"] for x in r), [x["title"] for x in r])
_, _, r = rows("treasury")
check("public-registry entity names are verified and keep their source URL", any(x["type"] == "wallet" and x["title"] == "Arbitrum DAO Treasury"
      and x["label"]["source"] == "public_registry" and x["label"].get("url", "").startswith("https://") for x in r), [x["title"] for x in r])
_, _, r = rows("0x9d53d5e3bd5e8d4cbfa6db1ca238aea02e651010")
check("0x address -> labelled wallet on its chain", r and r[0]["type"] == "wallet" and r[0]["chain"] == "robinhood"
      and r[0]["route"] == "#/address/0x9d53d5e3bd5e8d4cbfa6db1ca238aea02e651010?chain=robinhood", r[:1])
_, _, r = rows("0x5fc5360d0400a0fd4f2af552add042d716f1d168")
check("a token's address -> the token first", r and r[0]["type"] == "token" and r[0]["title"] == "USDG", [x["title"] for x in r])
_, _, r = rows("0x22fc0cf9063309efe17a555ddfa3e488679d868325f9ec4724227830243bc24c")
check("tx hash -> chain, from, to, block", r and r[0]["type"] == "tx" and r[0]["chain"] == "robinhood" and r[0]["block"] == 6050124
      and r[0]["from"] == "0xbf994e7bb09bb71f1b5efb17cee07f6dd90ab560" and r[0]["route"].startswith("#/tx/robinhood/0x22fc"), r[:1])
_, _, r = rows("0x" + "ab" * 32)
check("unknown tx -> one row saying where it was looked for, no route", len(r) == 1 and r[0]["route"] is None, r)
_, _, r = rows("nvdaa")
check("typo -> close match marked didYouMean", r and r[0]["title"] == "NVDA" and r[0].get("didYouMean") == "NVDA", r[:1])
_, d, r = rows("qzxwv exchange")
check("no match -> empty with a note (which institutions are named)", r == [] and any("exchange" in n for n in d.get("notes") or []), d.get("notes"))
_, _, r = rows("usdg", chain="robinhood")
check("chain filter", r and all(x["chain"] in ("robinhood", None) for x in r), [(x["title"], x["chain"]) for x in r])
for name, q, kw, code in (("empty q", "", {}, 400), ("q over 100 chars", "x" * 101, {}, 400), ("bad chain", "nvda", {"chain": "solana"}, 400)):
    s, e = get(q, **kw)
    check(f"error: {name} -> {code}", s == code and e.get("detail"), (s, e.get("detail")))
lat = []
for q in ("n", "nv", "a", "aa", "mo", "us", "we", "tr", "ga", "ca"):
    t = time.time()
    get(q, limit=8)
    lat.append(1000 * (time.time() - t))
    time.sleep(0.15)
check("prefix queries well under 300 ms", max(lat) < 300, "max %.1f ms" % max(lat))
print("%d/%d passed" % (N[0] - len(FAILS), N[0]) if not FAILS else "FAILED %d/%d: %s" % (len(FAILS), N[0], FAILS))
sys.exit(1 if FAILS else 0)
