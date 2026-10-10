"""Fetch the exact externally maintained wire interface used by this snapshot."""
import hashlib,json,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'engine/proto-provenance.json').read_text())
out=root/'engine/vendor/yellowstone';out.mkdir(parents=True,exist_ok=True)
base=manifest['repository'].replace('https://github.com/','https://raw.githubusercontent.com/')+'/'+manifest['commit']
for name,expected in manifest['files'].items():
    path=out/name
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==expected:continue
    with urllib.request.urlopen(base+'/yellowstone-grpc-proto/proto/'+name,timeout=30) as response:raw=response.read(128*1024)
    if hashlib.sha256(raw).hexdigest()!=expected:raise RuntimeError('Pinned interface mismatch: '+name)
    path.write_bytes(raw)
with urllib.request.urlopen(base+'/LICENSE',timeout=30) as response:(out/'UPSTREAM-LICENSE').write_bytes(response.read(128*1024))
print('Pinned interface and upstream license available in ignored dependency directory.')
