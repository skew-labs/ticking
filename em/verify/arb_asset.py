import json, csv, urllib.request, duckdb, unicodedata
from collections import Counter, defaultdict
G = "golden/arbitrum"; B = "http://127.0.0.1:8741/api/public/em/graph"
rows = list(csv.DictReader(open(f"{G}/sample.csv")))
qa = [r["address"] for r in rows if r["why"] == "random"][200:235] + [r["address"] for r in rows if "hub_out" in r["why"]][:6]
db = duckdb.connect(); lst = ",".join(f"'{a}'" for a in qa)
mm = defaultdict(list)
for f, t, a, k, n, amt, fb, lb in db.execute(f"""SELECT '0x'||lower(hex("from")), '0x'||lower(hex("to")), CASE WHEN kind='native_tx' THEN NULL ELSE '0x'||lower(hex(asset)) END, kind,
    n::BIGINT, coalesce(total_amount::VARCHAR, total_amount_big), first_block, last_block FROM read_parquet('{G}/edges.parquet') WHERE '0x'||lower(hex("from")) IN ({lst})""").fetchall():
    mm[(f, t)].append(dict(a=a, k=k, n=n, amt=amt, fb=fb, lb=lb))
K = {"native": ["native_tx"], "erc20": ["erc20"], "nft": ["erc721", "erc1155"]}
STABLE = {"USDC", "USDT", "DAI", "USDC.E", "USD₮0", "USDT0", "AUSD", "USDE", "FRAX", "LUSD", "USDS", "PYUSD", "TUSD", "FDUSD", "USDB", "GHO", "CRVUSD", "MIM"}
def suspicious(sym):
    if not sym: return False
    if any(unicodedata.category(ch) in ("Cf", "Zs") for ch in sym): return True          # invisible / odd spaces
    if any(ord(ch) > 127 and unicodedata.category(ch).startswith("L") and ch not in "₮" for ch in sym): return True  # non-ASCII letters (homoglyphs)
    low = sym.lower(); return any(s in low for s in ("http", ".com", ".io", "visit", "claim", "airdrop", "t.me", "www", "reward", "voucher"))
c = Counter(); ex = defaultdict(list); hid = 0; ehid = 0
for q in qa:
    j = json.loads(urllib.request.urlopen(f"{B}?address={q}&chain=arbitrum&hops=1&window=all&limit=25", timeout=120).read())
    hid += int(j.get("spamEdgesHidden") or 0); ehid += int(j.get("edgesHidden") or 0)
    for e in j["edges"]:
        if e.get("hop") != 1: continue
        s, t = e["source"].split(":")[1], e["target"].split(":")[1]
        A = e.get("asset") or {}; sym = A.get("symbol"); addr = (A.get("address") or "").lower() or None
        c["edges"] += 1
        if suspicious(sym): c["suspicious_symbol_shown"] += 1; ex["spam_shown"].append((sym, addr)) if len(ex["spam_shown"]) < 5 else None
        if e.get("usd") is not None:
            c["usd_filled"] += 1
            if not (sym and sym.upper() in STABLE): c["usd_on_nonstable"] += 1; ex["usd_nonstable"].append((sym, addr, e.get("usd"))) if len(ex["usd_nonstable"]) < 5 else None
            elif e.get("amount") is not None and abs(e["usd"] - e["amount"]) > 1e-6 * max(1, abs(e["amount"])): c["usd_ne_amount"] += 1
        elif sym and sym.upper() in STABLE and e.get("amount"): c["stable_without_usd"] += 1; ex["stable_no_usd"].append((sym, addr)) if len(ex["stable_no_usd"]) < 5 else None
        if e["kind"] != "native" and A.get("decimals") is None and e.get("amount") is not None: c["amount_without_decimals"] += 1
        if s != q or not e.get("exact"): continue
        c["exact_out"] += 1
        if e["kind"] == "native":
            ok = any(m["k"] == "native_tx" and m["n"] == e["n"] and m["amt"] == e.get("amountRaw") for m in mm[(s, t)]); c["native_ok" if ok else "native_bad"] += 1; continue
        if not addr: c["asset_addr_null"] += 1; continue
        cand = [m for m in mm[(s, t)] if m["k"] in K.get(e["kind"], [e["kind"]]) and m["a"] == addr]
        ok = any(m["n"] == e["n"] and (m["amt"] or None) == (e.get("amountRaw") or None) and m["fb"] == e.get("firstBlock") and m["lb"] == e.get("lastBlock") for m in cand)
        c["asset_row_match" if ok else "asset_row_mismatch"] += 1
        if not ok and len(ex["mismatch"]) < 3: ex["mismatch"].append({"tgt": t, "asset": addr, "sym": sym, "n": e["n"], "amt": e.get("amountRaw"), "map": [m for m in mm[(s, t)] if m["k"] in K.get(e["kind"], [e["kind"]])][:3]})
c["spamEdgesHidden_total"] = hid; c["edgesHidden_total"] = ehid
out = {"counts": dict(c), "examples": dict(ex)}
json.dump(out, open("results/arbitrum/GRAPH_STAGED_ASSET.json", "w"), indent=1, default=str)
print(json.dumps(out, default=str, ensure_ascii=False)[:2500])
