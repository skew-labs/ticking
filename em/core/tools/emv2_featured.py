#!/usr/bin/env python3
"""emv2_featured.py [--serve 127.0.0.1:8740] [--root /srv/skew/emv2] [--out /srv/skew/emv2/featured] [--per 12]
Home snapshot: for every chain emv2-serve serves, pick the most important labelled entities (exchanges, market makers, funds,
whales, bridges, ...) from the chain's label table and store each one's 1-hop graph (graph/1 from emv2-serve) as static JSON:
<out>/<chain>.json and <out>/index.json. The bridge serves these files as /api/public/em/featured (no upstream call, zero wait).
Run daily (emv2-featured.timer). Writes tmp + rename; a chain that fails keeps its previous file. Python with pyarrow."""
import json, os, sys, time, urllib.parse, urllib.request
import pyarrow.parquet as pq


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


SERVE, ROOT, OUT, PER = arg("--serve", "127.0.0.1:8740"), arg("--root", "/srv/skew/emv2"), arg("--out", "/srv/skew/emv2/featured"), int(arg("--per", "12"))
LABELS = ["/srv/skew/em-graphloop/export/{c}/labels.parquet", "/srv/skew/em-data/compact/{c}/labels.parquet"]
# group -> (categories, entity keywords); keywords pick market makers / funds out of the label entity names
GROUPS = [("exchange", {"cex_hot_wallet", "cex_deposit"}, ()),
          ("market_maker", set(), ("wintermute", "jump", "gsr", "amber", "cumberland", "flowdesk", "dwf", "galaxy digital", "b2c2", "keyrock", "auros")),
          ("fund", set(), ("a16z", "paradigm", "polychain", "multicoin", "dragonfly", "pantera", "three arrows", "alameda", "fund", "capital", "ventures")),
          ("whale", {"eoa_whale"}, ()),
          ("bridge", {"bridge"}, ()),
          ("protocol", {"lending", "dex_router"}, ())]
QUOTA = {"exchange": 4, "market_maker": 2, "fund": 2, "whale": 3, "bridge": 2, "protocol": 2}


def get(path):
    with urllib.request.urlopen("http://%s%s" % (SERVE, path), timeout=20) as r:
        return json.loads(r.read())


def candidates(chain):
    src = next((p.format(c=chain) for p in LABELS if os.path.exists(p.format(c=chain))), None)
    if not src:
        return []
    t = pq.read_table(src, columns=["address", "category", "entity_name", "importance", "confidence"])
    wanted = set().union(*(g[1] for g in GROUPS))
    # named entities, plus unnamed exchanges / whales / bridges (shown by category) when the label table names few
    rows = [r for r in zip(*(t.column(c).to_pylist() for c in ("address", "category", "entity_name", "importance", "confidence")))
            if r[2] or r[1] in wanted]
    rows.sort(key=lambda r: -(r[3] or 0))
    picked, ents = [], set()
    for g, cats, kws in GROUPS:
        n = 0
        for ad, cat, ent, imp, conf in rows:
            if n >= QUOTA[g]:
                break
            name = ent or ""
            key = name.split(":")[0].lower() or ("addr:" + (ad.hex() if isinstance(ad, (bytes, bytearray)) else str(ad)))
            if key in ents:
                continue
            if (cats and cat in cats) or (kws and name and any(k in name.lower() for k in kws)):
                a = ad.hex() if isinstance(ad, (bytes, bytearray)) else str(ad).lower().removeprefix("0x")
                if len(a) != 40:
                    continue
                picked.append({"group": g, "entity": ent or None, "category": cat, "address": "0x" + a, "importance": imp, "confidence": conf})
                ents.add(key); n += 1
    return picked


def main():
    os.makedirs(OUT, exist_ok=True)
    health = get("/health")
    index = {"schema": "featured/1", "generatedAt": int(time.time()), "chains": []}
    for chain in sorted((health.get("chains") or {}).keys()):
        try:
            out = {"schema": "featured/1", "chain": chain, "generatedAt": int(time.time()), "build": health["chains"][chain].get("build"), "entities": []}
            for c in candidates(chain):
                if len(out["entities"]) >= PER:
                    break
                q = urllib.parse.urlencode({"chain": chain, "address": c["address"], "hops": 1, "limit": 12})
                g = get("/api/public/em/graph?" + q)
                if not g.get("available"):
                    continue
                out["entities"].append({**c, "graph": g})
            tmp = os.path.join(OUT, ".%s.json.tmp" % chain)
            with open(tmp, "w") as f:
                json.dump(out, f, separators=(",", ":"))
            os.replace(tmp, os.path.join(OUT, chain + ".json"))
            index["chains"].append({"chain": chain, "entities": len(out["entities"]), "build": out["build"],
                                    "groups": sorted({e["group"] for e in out["entities"]})})
            print(json.dumps({"chain": chain, "entities": len(out["entities"])}), flush=True)
        except Exception as e:   # keep the previous file for this chain
            print(json.dumps({"chain": chain, "error": str(e)[:200]}), flush=True)
    tmp = os.path.join(OUT, ".index.json.tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(OUT, "index.json"))


if __name__ == "__main__":
    main()
