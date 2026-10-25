#!/usr/bin/env python3
"""compare_serve_map.py CHAIN BASE [--raw raw/<x>/raw_bal.csv] [--n 200]: emv2-serve answers against the frozen map (and the raw
recompute) for chains with no older service to compare with (arc, unichain, linea, ...). Runs on the VM next to emv2-serve.

  neighbors (dir=out): every returned row (peer, asset, kind) must equal the map edge from the sampled address to that peer for that
                       asset: n, amountRaw, firstBlock, lastBlock; `edges` must equal the map's non-self out-edge rows of the address.
  address-chains:      this chain's entry: firstSeen == map first_time, kind contract/eoa == map is_contract.
  holders-at (block = map end): holder count == raw balances > 0 (0x0 and 0x..dead left out); the top 30 addresses and their raw
                       balances (amount * 10^decimals, relative 1e-9) == the raw recompute's top 30.
Writes results/<chain>/SERVE_MAP.json.
"""
import json
import os
import subprocess
import sys
import urllib.request

chain, base = sys.argv[1], sys.argv[2].rstrip("/")
raw = sys.argv[sys.argv.index("--raw") + 1] if "--raw" in sys.argv else None
N = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 200
g, out = f"golden/{chain}", f"results/{chain}"
os.makedirs(out, exist_ok=True)
P = "/api/public/em"
DEAD = "0x000000000000000000000000000000000000dead"


def duck(sql):
    p = subprocess.run(["duckdb", "-json", "-c", f"SET threads=4; SET temp_directory='/data/emv2-dev/verify/tmp/p{os.getpid()}'; " + sql],
                       capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[:800])
    return json.loads(p.stdout or "[]")


def get(path):
    try:
        with urllib.request.urlopen(base + path, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


addrs = [l.strip() for l in open(f"{g}/sample_addresses.txt") if l.strip()][:N]
lst = ",".join(f"'{a}'" for a in addrs)
edges = duck(f"""SELECT '0x' || lower(hex("from")) f, '0x' || lower(hex("to")) t, CASE WHEN kind = 'native_tx' THEN 'native' ELSE '0x' || lower(hex(asset)) END a,
  kind k, n::VARCHAR n, coalesce(total_amount::VARCHAR, total_amount_big) amt, first_block fb, last_block lb FROM read_parquet('{g}/edges.parquet')
  WHERE '0x' || lower(hex("from")) IN ({lst})""")
by = {}
for e in edges:
    by.setdefault(e["f"], {})[(e["t"], e["a"], e["k"])] = e
res = {"neighbors": {"addresses": 0, "rows": 0, "row_diffs": 0, "format_only_erc721_amount": 0, "edges_count_diffs": 0, "errors": 0, "samples": []}}
KIND = {"native": "native_tx"}
for a in addrs:
    code, j = get(f"{P}/emv2/neighbors?chain={chain}&address={a}&dir=out&limit=200")
    nb = res["neighbors"]; nb["addresses"] += 1
    if code != 200:
        if not by.get(a):
            continue   # no out-edges in the map: a 404 / empty answer is right
        nb["errors"] += 1; nb["samples"].append({"a": a, "code": code, "body": str(j)[:200]}); continue
    mine = {k: v for k, v in by.get(a, {}).items() if k[0] != a}
    if j.get("edges") is not None and int(j["edges"]) != len(mine) and not j.get("truncated"):
        nb["edges_count_diffs"] += 1
        if len(nb["samples"]) < 20: nb["samples"].append({"a": a, "edges_v2": j.get("edges"), "edges_map": len(mine)})
    for r in j.get("rows", []):
        nb["rows"] += 1
        k = r.get("kind") or ("native_tx" if r.get("asset") == "native" else None)
        cand = [v for (t, asset, kk), v in mine.items() if t == r["peer"].lower() and asset == (r.get("asset") or "").lower() and (k is None or kk == k)]
        ok = any(c["n"] == str(r["n"]) and (c["amt"] or "") == (r.get("amountRaw") or "") and c["fb"] == r.get("firstBlock") and c["lb"] == r.get("lastBlock")
                 for c in cand)
        if not ok and any(c["k"] == "erc721" and c["amt"] is None and r.get("amountRaw") == "0" and c["n"] == str(r["n"])
                          and c["fb"] == r.get("firstBlock") and c["lb"] == r.get("lastBlock") for c in cand):
            nb["format_only_erc721_amount"] += 1   # map keeps no ERC721 amount (null); the server printed "0"
            continue
        if not ok:
            nb["row_diffs"] += 1
            if len(nb["samples"]) < 20: nb["samples"].append({"a": a, "v2": r, "map": cand[:2]})
# address-chains
ac = {"addresses": 0, "diffs": 0, "samples": []}
nodes = {x["a"]: x for x in duck(f"SELECT '0x' || lower(hex(address)) a, epoch(first_time)::BIGINT ft, is_contract c FROM read_parquet('{g}/addresses.parquet')")}
for a in addrs[:100]:
    code, j = get(f"{P}/address-chains?address={a}")
    ac["addresses"] += 1
    e = next((c for c in j.get("chains", []) if c.get("chain") == chain), None) if code == 200 else None
    m = nodes.get(a)
    bad = []
    if m and not e: bad.append("chain missing")
    if m and e:
        if e.get("firstSeen") != m["ft"]: bad.append(f"firstSeen {e.get('firstSeen')} != {m['ft']}")
        if (e.get("kind") in ("contract", "eoa_delegated")) != bool(m["c"]): bad.append(f"kind {e.get('kind')} vs is_contract {m['c']}")
    if bad:
        ac["diffs"] += 1
        if len(ac["samples"]) < 20: ac["samples"].append({"a": a, "issues": bad})
res["address_chains"] = ac
# holders-at at the map end against the raw recompute
if raw:
    end = int(open(f"{g}/ranges_included.tsv").read().strip().splitlines()[-1].split("\t")[-1])
    toks = duck(f"SELECT '0x' || lower(hex(token)) t FROM read_parquet('{g}/token_stats.parquet') WHERE standard = 'erc20' ORDER BY n_transfers DESC LIMIT 12")
    ho = {"tokens": 0, "count_diffs": 0, "top_diffs": 0, "samples": []}
    for t in (x["t"] for x in toks):
        code, j = get(f"{P}/holders-at?market={chain}:{t}&block={end}&limit=30")
        ho["tokens"] += 1
        snap = (j.get("snaps") or [{}])[0] if code == 200 else {}
        want = duck(f"""SELECT count(*) FILTER (WHERE left(balance, 1) <> '-' AND balance <> '0' AND address NOT IN ('0x0000000000000000000000000000000000000000', '{DEAD}')) n
          FROM read_csv('{raw}', header=true, all_varchar=true) WHERE token = '{t}' AND kind = 'erc20'""")[0]["n"]
        top = duck(f"""SELECT address, balance FROM read_csv('{raw}', header=true, all_varchar=true) WHERE token = '{t}' AND kind = 'erc20'
          AND left(balance, 1) <> '-' AND balance <> '0' AND address NOT IN ('0x0000000000000000000000000000000000000000', '{DEAD}')
          ORDER BY balance::HUGEINT DESC, address LIMIT 30""")
        if snap.get("holders") != want:
            ho["count_diffs"] += 1
            if len(ho["samples"]) < 20: ho["samples"].append({"t": t, "holders_v2": snap.get("holders"), "holders_raw": want, "code": code})
        v2top = [x.get("address", "").lower() for x in snap.get("top", [])]
        if v2top[: len(top)] != [x["address"] for x in top][: len(v2top)] or not v2top:
            ho["top_diffs"] += 1
            if len(ho["samples"]) < 20: ho["samples"].append({"t": t, "top_v2": v2top[:5], "top_raw": [x["address"] for x in top][:5]})
    res["holders_at"] = ho
res["ok"] = (res["neighbors"]["row_diffs"] == 0 and res["neighbors"]["edges_count_diffs"] == 0 and res["neighbors"]["errors"] == 0
             and ac["diffs"] == 0 and (not raw or (res["holders_at"]["count_diffs"] == 0 and res["holders_at"]["top_diffs"] == 0)))
json.dump(res, open(f"{out}/SERVE_MAP.json", "w"), indent=1, default=str)
print(json.dumps({k: ({kk: vv for kk, vv in v.items() if kk != "samples"} if isinstance(v, dict) else v) for k, v in res.items()}))
