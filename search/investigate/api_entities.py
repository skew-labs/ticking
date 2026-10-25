"""Contract checks for entities/1, entity/1 and the research-label rows of search/1, through a wallet bridge.
usage: python3 api_entities.py http://127.0.0.1:8901 [entity id, default okx] [alias, default okex]   (GET only). Exits 1 on any failure."""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/")
EID = sys.argv[2] if len(sys.argv) > 2 else "okx"
ALIAS = sys.argv[3] if len(sys.argv) > 3 else "okex"
FAILS, N = [], [0]


def get(path, **kw):
    try:
        with urllib.request.urlopen(BASE + path + "?" + urllib.parse.urlencode(kw), timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, ok, detail=""):
    N[0] += 1
    print(("PASS " if ok else "FAIL ") + name + ((" · " + str(detail)[:220]) if detail else ""), flush=True)
    ok or FAILS.append(name)


s, d = get("/api/public/em/entities", q=EID)
r = d.get("rows") or []
check("entities/1: the name finds the entity", s == 200 and d.get("schema") == "entities/1" and r and r[0]["entity_id"] == EID, d.get("detail"))
check("entities/1 row contract", r and {"entity_id", "name", "type", "per_chain", "per_grade", "holdings"} <= set(r[0]), r[:1])
s, d = get("/api/public/em/entities", q=ALIAS)
check("an alias finds the entity", s == 200 and any(x["entity_id"] == EID for x in d.get("rows") or []), [x["entity_id"] for x in d.get("rows") or []])
s, d = get("/api/public/em/search", q=EID)
r = d.get("results") or []
check("search: the name -> the entity page first", s == 200 and r and r[0]["type"] == "entity" and r[0]["route"] == "#/entity/" + EID, r[:1])
t0 = time.time()
s, d = get("/api/public/em/entity", id=EID, limit="5")
ms = 1000 * (time.time() - t0)
a = d.get("addresses") or []
check("entity/1 contract", s == 200 and d.get("schema") == "entity/1" and d["entity"]["entity_id"] == EID and d.get("sources")
      and {"per_chain", "per_grade"} <= set(d.get("totals") or {}) and {"perChain", "totalUsd", "partial"} <= set(d.get("holdings") or {}), d.get("detail"))
check("addresses: official first, every row says its grade and source", a and a[0]["grade"] == "official" and all(
    {"chain", "address", "role", "grade", "gradeText", "verified", "source", "source_url", "as_of", "route"} <= set(x) for x in a), a[:1])
check("candidate rows read 'estimated', never verified", all(x["gradeText"] == "estimated" and not x["verified"] for x in a if x["grade"] == "candidate"))
check("signatures stay in the source file", all("signature" not in (x.get("evidence") or {}) for x in a if isinstance(x.get("evidence"), dict)))
got, cur, pages = [(x["chain"], x["address"]) for x in a], d.get("nextCursor"), 0
while cur and pages < 3:
    s, d2 = get("/api/public/em/entity", id=EID, limit="5", cursor=cur)
    got += [(x["chain"], x["address"]) for x in d2.get("addresses") or []]
    cur, pages = d2.get("nextCursor"), pages + 1
check("cursor pages do not repeat rows", pages and len(got) == len(set(got)) == 5 * (pages + 1), (pages, len(got), len(set(got))))
s, d = get("/api/public/em/entity", id=EID, chain="arbitrum", grade="strong", limit="3")
check("filters chain + grade", s == 200 and d.get("addresses") and all(x["chain"] == "arbitrum" and x["grade"] == "strong" for x in d["addresses"]),
      d.get("detail"))
s, d = get("/api/public/em/entity", id=EID, chain="ethereum", limit="1")
off = (d.get("addresses") or [{}])[0].get("address")
s, d = get("/api/public/em/search", q=off or "0x")
r = d.get("results") or []
check("search: an official address is named by the entity first", off and r and r[0].get("entity", {}).get("id") == EID
      and r[0]["label"]["grade"] == "official" and r[0]["label"]["verified"] is True, r[:1])
s, d = get("/api/public/em/entity", id="no-such-entity")
check("unknown id -> 404", s == 404, d)
s, d = get("/api/public/em/entity", id=EID, cursor="bad")
check("bad cursor -> 400", s == 400, d)
s, d = get("/api/public/em/entity", id=EID, grade="maybe")
check("bad grade -> 400", s == 400, d)
s, d = get("/api/public/em/entities", type="exchange", chain="arbitrum", limit="10")
r = d.get("rows") or []
check("list mode without q: type + chain, by holdings there", s == 200 and any(x["entity_id"] == EID for x in r) and all(x["type"] == "exchange" for x in r)
      and [x["holdings"]["perChain"].get("arbitrum", {}).get("usd") or 0 for x in r] == sorted([x["holdings"]["perChain"].get("arbitrum", {}).get("usd") or 0 for x in r], reverse=True), [x["entity_id"] for x in r])
s, d = get("/api/public/em/entities")
check("no q, no type, no chain -> 400", s == 400, d)
s, d = get("/api/public/em/entity", id=EID, chain="arbitrum", sort="networth", limit="5")
a = d.get("addresses") or []
nw = [x["netWorthUsd"] for x in a]
check("sort=networth: addresses with a sheet first, largest first", s == 200 and a and nw[0] is not None
      and [v for v in nw if v is not None] == sorted([v for v in nw if v is not None], reverse=True), nw)
if d.get("nextCursor"):
    s, d2 = get("/api/public/em/entity", id=EID, chain="arbitrum", sort="networth", limit="5", cursor=d["nextCursor"])
    check("sort=networth pages by offset without repeats", s == 200 and not ({x["address"] for x in a} & {x["address"] for x in d2.get("addresses") or []}), d2.get("nextCursor"))
check("entity page well under 1 s", ms < 1000, "%.0f ms" % ms)
print(f"{N[0] - len(FAILS)}/{N[0]} passed")
sys.exit(1 if FAILS else 0)
