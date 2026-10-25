#!/usr/bin/env python3
from pathlib import Path
import time
import subprocess

path = Path("/srv/skew/em-charts/server.py")
text = path.read_text()
bak = path.with_name(path.name + f".bak-fullarbrh-{time.strftime('%Y%m%d-%H%M%S')}")
bak.write_text(text)
print("backup", bak)

if "no_invented_prices_hot_required" not in text:
    old = (
        '"priceSampling": "Binance 1s OHLCV remapped to chain token id; Sol may blend oracle-fast close tip."}}'
    )
    new = (
        '"priceSampling": "Binance 1s for CEX-mapped majors; tip_hot 1s from on-chain fills for all other Arb/RH tokens.",'
        '"coveragePolicy": "no_invented_prices_hot_required"}}'
    )
    if old not in text:
        # try multiline already patched shape
        idx = text.find('"priceSampling"')
        print("snippet", repr(text[idx:idx + 220]))
        raise SystemExit("coverage needle missing")
    text = text.replace(old, new, 1)
    print("coverage patched")
else:
    print("coverage already")

# Allow empty tip_hot markets: change 404 for unmapped to only when not arb/rh hex
old404 = (
    '    if not mid:\n'
    '        return 404, {"detail": "No 1s series for this market yet (sec-bars map). Use a CEX-mapped Sol/Arb/RH liquid, e.g. WETH/SOL.",\n'
    '                     "hint": "Mapped majors only until Fast Feed / broader tick coverage."}\n'
)
new404 = (
    '    if not mid:\n'
    '        return 404, {"detail": "No 1s series for this market. Arb/RH need tip/hot fills; Sol majors need CEX map.",\n'
    '                     "hint": "All Arb+RH hot tokens get tip_hot 1s bars; CEX-mapped majors also mirror Binance 1s."}\n'
)
if old404 in text:
    text = text.replace(old404, new404, 1)
    print("404 hint patched")

path.write_text(text)
r = subprocess.run(
    ["/srv/skew/em-galaxy/.venv/bin/python", "-m", "py_compile", "/srv/skew/em-charts/server.py",
     "/srv/skew/em-charts/sec_bars_read.py", "/srv/skew/em-realtime/sec-bars/onchain_1s_writer.py"],
    capture_output=True, text=True,
)
print("compile", r.returncode, (r.stderr or "ok")[:400])
if r.returncode:
    raise SystemExit(r.returncode)
