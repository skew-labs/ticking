#!/usr/bin/env python3
"""blob_get.py NAME [OUT] [--range a-b]: download one blob (or a byte range). Never prints the SAS."""
import sys, urllib.parse, urllib.request
SAS = open('/root/.em-blob.sas').read().strip().lstrip('?')
C = 'https://arbitrum.blob.core.windows.net/em-mapping'
name = sys.argv[1]; out = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else None
req = urllib.request.Request(f"{C}/{urllib.parse.quote(name)}?{SAS}")
if '--range' in sys.argv: req.add_header('x-ms-range', 'bytes=' + sys.argv[sys.argv.index('--range') + 1])
data = urllib.request.urlopen(req, timeout=300).read()
if out: open(out, 'wb').write(data); print(len(data), 'bytes ->', out)
else: sys.stdout.buffer.write(data)
