"""Network-keyed artwork in qa/refresh-token-icons.py (Arbitrum One / Robinhood Chain).

usage: apply_qa_patch.py <ticking_root> [--check]
`--networks <bundle>` adds manifest.networkIcons['<network>:<0x address>'] with content-addressed
<network>-<address>-<sha16>.webp files, normalised by the same encode_square() as every other icon,
from the reviewed sources in <bundle>/sources.json (Simple Icons tiles, exact-address token-list logos,
Ticking monograms). Solana and exchange icons are unchanged; without --networks the run is identical.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from patchlib import apply  # noqa: E402

NETWORKS = r'''

NETWORK_HOSTS={'assets-cdn.trustwallet.com':'/blockchains/','raw.githubusercontent.com':'/trustwallet/assets/','assets.coingecko.com':'/coins/images/','coin-images.coingecko.com':'/coins/images/'}


def network_sources(entry,bundle):
    """Reviewed artwork for one exact network/address identity; artwork never establishes identity."""
    keep={k:entry[k] for k in ('sourceType','license','simpleIcons','slug','brand','hex','svgUrl','svgSha256','note','style','renderedSha256') if k in entry}
    if entry.get('file'):
        path=(bundle/entry['file']).resolve()
        if path.parent!=bundle.resolve() or not re.fullmatch(r'(?:arbitrum|robinhood)-0x[0-9a-f]{40}\.png',path.name) or not path.is_file():return []
        return [{'source':str(path),**keep}]
    out=[]
    for c in entry.get('candidates',[]):
        u=urlsplit(c.get('url',''))
        if u.scheme=='https' and u.hostname in NETWORK_HOSTS and u.path.startswith(NETWORK_HOSTS[u.hostname]) and allowed_url(c['url']):
            out.append({'source':c['url'],**keep,'list':c.get('list'),'listUrl':c.get('listUrl'),**({'canonicalFor':c['canonicalFor']} if c.get('canonicalFor') else {})})
    return out


def refresh_networks(root,live,output,old,force=False,bundle=None,reader=read_source):
    """Reviewed EVM rows only, pinned by network + address in reviewed_markets.json."""
    bundle=Path(bundle).resolve();sources=json.loads((bundle/'sources.json').read_text())['sources']
    catalog=json.loads((root/'server/evm_catalog.json').read_text())['markets']
    reviewed={t['id']:t for t in json.loads((root/'server/reviewed_markets.json').read_text())['markets']}
    icons=dict(old.get('networkIcons',{}));failures={}
    def fetch(t):
        key=t['network']+':'+t['mint'];review=reviewed.get(t['id'])
        if t['id']!=key or not review or review.get('network')!=t['network'] or review.get('mint')!=t['mint'] or key not in sources:return key,None,'unreviewed'
        def read(source):return reader(source) if source.startswith('https:') else Path(source).read_bytes()
        _,row,error=refresh_one({'mint':t['network']+'-'+t['mint']},network_sources(sources[key],bundle),icons.get(key),output,live,force,read)
        if row:
            row={k:v for k,v in row.items() if k!='identityMint'};row['identity']={'network':t['network'],'address':t['mint']}
            if not row['source'].startswith('https:'):row['source']='bundle:'+Path(row['source']).name
        return key,row,error
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for key,row,error in pool.map(fetch,catalog):
            if row:icons[key]=row
            if error:failures['network_'+error]=failures.get('network_'+error,0)+1
    return icons,failures
'''

PATCHES = {
    'qa/refresh-token-icons.py': [
        ("    return u.hostname=='cdn.dexscreener.com' and u.path.startswith('/')\n",
         "    if u.hostname in NETWORK_HOSTS:return u.path.startswith(NETWORK_HOSTS[u.hostname])\n"
         "    return u.hostname=='cdn.dexscreener.com' and u.path.startswith('/')\n"),
        ("IMAGE_NAME=re.compile(r'(?:[1-9A-HJ-NP-Za-km-z]{32,44}|exchange-binance-(?:spot|usdm)-[a-z0-9]{2,30})-[a-f0-9]{16}\\.webp')",
         "IMAGE_NAME=re.compile(r'(?:[1-9A-HJ-NP-Za-km-z]{32,44}|exchange-binance-(?:spot|usdm)-[a-z0-9]{2,30}|(?:arbitrum|robinhood)-0x[0-9a-f]{40})-[a-f0-9]{16}\\.webp')"),
        ("\n\ndef run(reviewed_only=False,force=False,output=None,metadata_limit=300):",
         NETWORKS + "\n\ndef run(reviewed_only=False,force=False,output=None,metadata_limit=300,networks=None):"),
        ("                'failures':failures,'icons':icons,'exchangeIcons':exchange_icons}\n",
         "                'failures':failures,'icons':icons,'exchangeIcons':exchange_icons}\n"
         "        if networks:\n"
         "            network_icons,network_failures=refresh_networks(ROOT,live,output,old,force,networks)\n"
         "            failures.update(network_failures);report.update(networkCount=len(network_icons),networkIcons=network_icons)\n"
         "        elif 'networkIcons' in old:report['networkIcons']=old['networkIcons']\n"),
        ("        print(json.dumps({k:v for k,v in report.items() if k not in {'icons','exchangeIcons'}}),flush=True)",
         "        print(json.dumps({k:v for k,v in report.items() if k not in {'icons','exchangeIcons','networkIcons'}}),flush=True)"),
        ("p.add_argument('--metadata-limit',type=int,choices=range(0,2001),default=300)\n    args=p.parse_args();run(args.reviewed_only,args.force,args.output,args.metadata_limit)",
         "p.add_argument('--metadata-limit',type=int,choices=range(0,2001),default=300);p.add_argument('--networks')\n"
         "    args=p.parse_args();run(args.reviewed_only,args.force,args.output,args.metadata_limit,args.networks)"),
    ],
}

if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    apply(sys.argv[1], PATCHES, check='--check' in sys.argv)
