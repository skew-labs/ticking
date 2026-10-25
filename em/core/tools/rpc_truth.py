#!/usr/bin/env python3
"""rpc_truth.py (skew): independent check of EMV2 balances against chain state over public RPC (never QuickNode).

Comparing V2 with the old engine cannot catch an error both share (WETH9, Arc native USDC), so every chain switched to `new`
is also checked against the node itself: the chain's top tokens by holder count, and for each its top holders' balances and
totalSupply(), read with eth_call at the ledger head block. A public RPC without that block's state is asked at `latest` and
the row says so (time skew, not an error by itself).

    python3 rpc_truth.py <chain> --rpc URL [--rpc URL2] [--tokens 20] [--holders 20] [--serve http://127.0.0.1:8740]
                         [--meta /srv/skew/em-data/econ/<chain>/token_meta.parquet] [--out DIR]

Pass: |ledger - rpc| <= 0.1% of rpc for every holder (and the supply). Output: <out>/<chain>-<ts>.json and a TSV summary line
per token on stdout. Requests are paced (min gap 0.15 s, doubled on 429/5xx up to 8 s).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("chain")
ap.add_argument("--rpc", action="append", required=True)
ap.add_argument("--tokens", type=int, default=20)
ap.add_argument("--holders", type=int, default=20)
ap.add_argument("--serve", default="http://127.0.0.1:8740")
ap.add_argument("--meta", default=None)
ap.add_argument("--out", default="/srv/skew/emv2/checks/truth")
ap.add_argument("--tol", type=float, default=0.001)
a = ap.parse_args()
meta_path = a.meta or f"/srv/skew/em-data/econ/{a.chain}/token_meta.parquet"
gap = 0.15
rpc_i = 0


def serve(q):
    with urllib.request.urlopen(a.serve + q, timeout=60) as r:
        return json.loads(r.read())


def rpc(method, params):
    """One JSON-RPC call; rotates endpoints and backs off on rate limits. Returns (result, error message)."""
    global gap, rpc_i
    err = ""
    for attempt in range(10):
        time.sleep(gap)
        url = a.rpc[rpc_i % len(a.rpc)]
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        try:
            req = urllib.request.Request(url, body, {"content-type": "application/json", "user-agent": "emv2-truth/1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            gap = max(0.15, gap * 0.9)
            if "error" in d:
                return None, str(d["error"].get("message", d["error"]))[:160]
            return d.get("result"), None
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                gap = min(8.0, gap * 2); rpc_i += 1; continue
            return None, f"http {e.code}"
        except Exception as e:  # timeouts, resets: next endpoint
            gap = min(8.0, gap * 2); rpc_i += 1
            err = str(e)[:120]
    return None, "rpc gave up " + err


def call_uint(to, data, block):
    """eth_call returning a uint256 at `block` (hex) or, if the node lacks that state, at latest. (value, at, error)."""
    res, err = rpc("eth_call", [{"to": to, "data": data}, block])
    at = block
    if err and any(s in err.lower() for s in ("missing trie", "header not found", "pruned", "state", "not available", "historical", "unknown block")):
        res, err = rpc("eth_call", [{"to": to, "data": data}, "latest"]); at = "latest"
    if err or not res or res == "0x":
        return None, at, err or "empty"
    try:
        return int(res[:66], 16), at, None
    except ValueError:
        return None, at, "bad result"


def load_meta():
    import pyarrow.parquet as pq
    t = pq.read_table(meta_path, columns=["token", "symbol", "decimals"]).to_pylist()
    return {r["token"].lower(): r for r in t if r.get("token")}


def main():
    meta = load_meta()
    health = serve("/health")
    st = health["chains"][a.chain]
    head = int(st["head"]); bhex = hex(head)
    print(f"# {a.chain} build {st['build']} head {head}; {len(meta)} candidate tokens", file=sys.stderr)
    # rank candidates by holder count at the head (one cheap holders-at per token)
    ranked = []
    for i, tok in enumerate(meta):
        try:
            d = serve(f"/holders-at?market={a.chain}:{tok}&block={head}&limit=1")
        except Exception:
            continue
        s = (d.get("snaps") or [{}])[0]
        if s.get("available"):
            ranked.append((int(s.get("holders") or 0), tok))
    ranked.sort(reverse=True)
    rows = []
    for holders, tok in ranked[: a.tokens]:
        d = serve(f"/holders-at?market={a.chain}:{tok}&block={head}&limit={a.holders}")
        s = d["snaps"][0]
        m = meta.get(tok, {})
        dec = m.get("decimals")
        dec = 18 if dec is None or dec == 255 else int(dec)
        sym = d.get("symbol") or m.get("symbol") or ""
        out = {"token": tok, "symbol": sym, "decimals": dec, "holders": holders, "checked": 0, "pass": 0, "fail": [], "at": set(),
               "supply_v2": s.get("supply"), "supply_basis": s.get("supplyBasis")}
        sup, at, err = call_uint(tok, "0x18160ddd", bhex)
        out["at"].add(at)
        out["supply_rpc"] = None if sup is None else sup / 10 ** dec
        out["supply_err"] = err
        if sup is not None and s.get("supplyBasis") == "totalSupply()" and s.get("supply") is not None:
            rs = out["supply_rpc"]; v = float(s["supply"])
            out["supply_rel"] = abs(v - rs) / rs if rs else (0.0 if v == 0 else 1.0)
        for h in s.get("top", []):
            addr = h["address"]; v2 = float(h["amount"])
            bal, at, err = call_uint(tok, "0x70a08231" + "0" * 24 + addr[2:], bhex)
            out["at"].add(at)
            out["checked"] += 1
            if bal is None:
                out["fail"].append({"address": addr, "v2": v2, "rpc": None, "err": err}); continue
            rv = bal / 10 ** dec
            rel = abs(v2 - rv) / rv if rv else (0.0 if v2 == 0 else 1.0)
            if rel <= a.tol:
                out["pass"] += 1
            else:
                out["fail"].append({"address": addr, "v2": v2, "rpc": rv, "rel": round(rel, 6)})
        out["at"] = sorted(out["at"])
        ok = out["checked"] > 0 and out["pass"] == out["checked"] and (out.get("supply_rel") is None or out["supply_rel"] <= a.tol)
        out["verdict"] = "PASS" if ok else "FAIL"
        rows.append(out)
        worst = max([f.get("rel") or 1.0 for f in out["fail"]], default=0.0)
        print("\t".join(str(x) for x in (a.chain, sym, tok, holders, f"{out['pass']}/{out['checked']}",
                                         "-" if out.get("supply_rel") is None else f"{out['supply_rel']:.2e}", f"{worst:.3g}",
                                         ",".join(out["at"]), out["verdict"])), flush=True)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"{a.chain}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    with open(path, "w") as f:
        json.dump({"chain": a.chain, "build": st["build"], "head": head, "tol": a.tol, "tokens": rows}, f, indent=1)
    print(f"# wrote {path}", file=sys.stderr)


main()
