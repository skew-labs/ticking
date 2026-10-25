#!/usr/bin/env python3
from pathlib import Path
import time
import subprocess

path = Path("/srv/skew/em-charts/server.py")
text = path.read_text()
bak = path.with_name(path.name + f".bak-secdump-{time.strftime('%Y%m%d-%H%M%S')}")
bak.write_text(text)
print("backup", bak)

if "import sec_dump_tags" not in text:
    if "import sec_bars_read\n" not in text:
        raise SystemExit("sec_bars_read import missing")
    text = text.replace("import sec_bars_read\n", "import sec_bars_read\nimport sec_dump_tags\n", 1)

needle = (
    '            "coverage": {"complete": False, "source": "cex_1s_mirror", "policy": "binance_1s_kline_mirror_v1",\n'
    '                         "commitment": "provisional_paint", "sot": "tip_hot_onchain",\n'
    '                         "priceSampling": "Binance 1s OHLCV remapped to chain token id; Sol may blend oracle-fast close tip."}}\n'
    "    return 200, body"
)

insert = (
    '            "coverage": {"complete": False, "source": "cex_1s_mirror", "policy": "binance_1s_kline_mirror_v1",\n'
    '                         "commitment": "provisional_paint", "sot": "tip_hot_onchain",\n'
    '                         "priceSampling": "Binance 1s OHLCV remapped to chain token id; Sol may blend oracle-fast close tip."}}\n'
    "    # 1s whale dump tags (tip→hot fills aligned to Binance UTC second) — 2026-10-23\n"
    "    try:\n"
    "        sec_dump_tags.attach_to_candles_body(body, mid, symbol=symbol)\n"
    "    except Exception as e:\n"
    '        body.setdefault("dumpTags", [])\n'
    '        body.setdefault("markers", [])\n'
    '        body["dumpTagsError"] = str(e)[:160]\n'
    "    return 200, body"
)

if "sec_dump_tags.attach_to_candles_body" in text:
    print("already patched")
else:
    if needle not in text:
        # show nearby for debug
        idx = text.find('"coverage": {"complete": False, "source": "cex_1s_mirror"')
        print("idx", idx)
        print(repr(text[idx:idx+350] if idx>=0 else "missing"))
        raise SystemExit("needle not found")
    text = text.replace(needle, insert, 1)
    path.write_text(text)
    print("patched ok")

r = subprocess.run(
    [
        "/srv/skew/em-galaxy/.venv/bin/python",
        "-m",
        "py_compile",
        "/srv/skew/em-charts/server.py",
        "/srv/skew/em-charts/sec_dump_tags.py",
    ],
    capture_output=True,
    text=True,
)
print("compile", r.returncode, (r.stderr or "ok")[:400])
if r.returncode != 0:
    raise SystemExit(r.returncode)
