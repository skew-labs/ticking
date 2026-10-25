import shutil, sys, time
p = "/srv/skew/em-charts/server.py"
s = open(p).read()
if "traders_api" in s:
    sys.exit("already patched")
shutil.copy2(p, p + ".bak-20261019-traders")
a = '''def em_market(qs):'''
b = '''def traders_response(kind, qs):
    """/api/public/em/top-traders?market=<chain>:<token>&window=7d|30d|all, /api/public/em/wallet-pnl?chain=&address= (traders_api)."""
    import traders_api
    if kind == "top-traders":
        ch, m = resolve_market(qs.get("market") or qs.get("mint"))
        if not m:
            return 404, {"detail": "Unknown market. Use <chain>:<token address> (see /api/public/markets)."}
        return traders_api.top_traders(ch.name, m, qs.get("window", "30d"), int(qs.get("limit", "20")))
    chain = qs.get("chain")
    if chain not in CH:
        return 400, {"detail": "chain must be one of " + ", ".join(CH)}
    return traders_api.wallet_pnl(chain, qs.get("address", ""), int(qs.get("limit", "20")))


def em_market(qs):'''
assert s.count(a) == 1; s = s.replace(a, b)
a = '''        if p in ("/api/health", "/health"):'''
b = '''        if p in ("/api/public/em/top-traders", "/api/public/em/wallet-pnl"):
            return self.send_json(*traders_response(p.rsplit("/", 1)[1], qs), cache=60)
        if p in ("/api/health", "/health"):'''
assert s.count(a) == 1; s = s.replace(a, b)
import ast; ast.parse(s)
open(p + ".tmp", "w").write(s); shutil.copymode(p, p + ".tmp")
import os; os.replace(p + ".tmp", p)
print("patched")
