"""Run only the patched refresh_networks() (network-keyed EVM icons) against a Ticking tree and its icon store.
usage: refresh_network_icons.py <ticking_root> <icon_store> <artwork_bundle>   (as the icon job user; Solana/exchange icons untouched)"""
import json, runpy, sys
from pathlib import Path
root, live, bundle = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(root / 'qa'))
ns = runpy.run_path(str(root / 'qa/refresh-token-icons.py'), run_name='staging_icons')
old = json.loads((live / 'manifest.json').read_text())
icons, failures = ns['refresh_networks'](root, live, live, old, False, bundle)
old.update(networkIcons=icons, networkCount=len(icons))
tmp = live / 'manifest.tmp'; tmp.write_text(json.dumps(old, separators=(',', ':'))); tmp.replace(live / 'manifest.json')
kinds = {}
for r in icons.values(): kinds[r['sourceType']] = kinds.get(r['sourceType'], 0) + 1
print(json.dumps({'networkIcons': len(icons), 'failures': failures, 'kinds': kinds}))
