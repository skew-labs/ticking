#!/usr/bin/env python3
"""Wire multi-second TF paint + dump tag bucketing on em-charts server."""
from pathlib import Path
import time
import subprocess

path = Path("/srv/skew/em-charts/server.py")
text = path.read_text()
bak = path.with_name(path.name + f".bak-sectf-{time.strftime('%Y%m%d-%H%M%S')}")
bak.write_text(text)
print("backup", bak)

# 1) pass period_s into attach
old_attach = "        sec_dump_tags.attach_to_candles_body(body, mid, symbol=symbol)\n"
new_attach = "        sec_dump_tags.attach_to_candles_body(body, mid, symbol=symbol, period_s=period_s)\n"
if old_attach in text:
    text = text.replace(old_attach, new_attach, 1)
    print("attach period_s ok")
elif "period_s=period_s" in text:
    print("attach already has period_s")
else:
    raise SystemExit("attach call not found")

# 2) richer mode / interval metadata in _candles_response_sec body
old_mode = '"mode": "sec_bars_1s", "aggregation": "token"'
new_mode = '"mode": "sec_bars", "aggregation": "token"'
if old_mode in text:
    text = text.replace(old_mode, new_mode, 1)
    print("mode ok")

# 3) parse_interval: allow sub-minute explicitly (already works); fix error text for clarity
old_err = '        if sec > 604800:\n            raise ValueError("Choose an interval between 1 minute and 1 week.")\n'
new_err = (
    '        if sec < 1 or sec > 604800:\n'
    '            raise ValueError("Choose an interval from 1s to 1w (e.g. 1s, 3s, 5s, 15s, 30s, 1m).")\n'
)
if old_err in text:
    text = text.replace(old_err, new_err, 1)
    print("parse_interval err ok")
else:
    print("parse_interval err skip (already changed?)")

path.write_text(text)
r = subprocess.run(
    ["/srv/skew/em-galaxy/.venv/bin/python", "-m", "py_compile",
     "/srv/skew/em-charts/server.py",
     "/srv/skew/em-charts/sec_bars_read.py",
     "/srv/skew/em-charts/sec_dump_tags.py",
     "/srv/skew/em-realtime/sec-bars/writer.py"],
    capture_output=True, text=True,
)
print("compile", r.returncode, (r.stderr or "ok")[:400])
if r.returncode:
    raise SystemExit(r.returncode)
