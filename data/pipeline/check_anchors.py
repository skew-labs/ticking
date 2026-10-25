"""check_anchors.py: symbol() / decimals() of every evm_cfg anchor over the chain's public RPC (exit 1 on any miss)."""
import json
import sys
import urllib.request

sys.path.insert(0, "/srv/skew/em-realtime")
import evm_cfg as E  # noqa: E402


def call(u, to, data):
    req = urllib.request.Request(u, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "curl/8.5.0"})
    return json.load(urllib.request.urlopen(req, timeout=20)).get("result")


def sym(h):
    b = bytes.fromhex(h[2:])
    if len(b) == 32:
        return b.rstrip(b"\0").decode(errors="replace")
    n = int.from_bytes(b[32:64], "big")
    return b[64:64 + n].decode(errors="replace")


bad = 0
for ch, rows in E.ANCHORS.items():
    out = []
    for a, k, p, s in rows:
        if a == E.NATIVE:
            out.append(f"{s}:native")
            continue
        try:
            r, d = call(E.RPC[ch], a, "0x95d89b41"), call(E.RPC[ch], a, "0x313ce567")
            got, dec = sym(r), int(d, 16)
        except Exception as e:
            got, dec = None, str(e)[:40]
        ok = bool(got) and isinstance(dec, int)
        bad += not ok
        out.append(f"{s}={got}/{dec}" + ("" if ok else " !!"))
    print(ch, " ".join(out))
print("problems:", bad)
sys.exit(1 if bad else 0)
