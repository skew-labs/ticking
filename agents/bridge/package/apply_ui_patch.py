"""First-class Arbitrum / Robinhood networks in the console API (tick-market-ui server/).

usage: apply_ui_patch.py <server_dir> [--check]
Adds evm_data.py and evm_catalog.json; `network` on the public/v1 candle, trade, resolve and
stream routes; explicit `network` on every catalog row (default solana); reviewed rows pinned by
network; network-keyed icon names; a `network` column on display grants. Requests without an
EVM network or an exact '<network>:<0x address>' market keep the existing Solana/exchange paths.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from patchlib import apply  # noqa: E402

EVM = 'evm_data.NETWORKS'
PATCHES = {
    'engine_data.py': [
        ('from . import global_data\n', 'from . import global_data, evm_data\n'),
        ("def resolve(mint=None,pool=None,market=None,quote=None,aggregation='pool'):\n",
         "def resolve(mint=None,pool=None,market=None,quote=None,aggregation='pool',network=None):\n"
         "    if network in evm_data.NETWORKS or evm_data.chosen(market):return evm_data.resolve(evm_data.selection(market,network,mint),aggregation,pool)\n"),
        ('    """Resolve once, before topic sharing; ranking changes cannot replace a tape."""\n',
         '    """Resolve once, before topic sharing; ranking changes cannot replace a tape."""\n'
         "    if query.get('network') in evm_data.NETWORKS:return evm_data.pin_stream(query,kind)\n"),
        ("def candles(market=None,interval='1m',limit=300,mint=None,pool=None,before=None,quote=None,pool_pin=None,gap_policy='recorded',aggregation='pool',history='executions'):\n",
         "def candles(market=None,interval='1m',limit=300,mint=None,pool=None,before=None,quote=None,pool_pin=None,gap_policy='recorded',aggregation='pool',history='executions',network=None):\n"
         "    if network in evm_data.NETWORKS or evm_data.chosen(market):return evm_data.candles(evm_data.selection(market,network,mint),interval,limit,before,gap_policy,aggregation,pool_pin or pool,history)\n"),
        ("def trades(mint=None,pool=None,market=None,limit=50,base_mint=None,aggregation='pool'):\n",
         "def trades(mint=None,pool=None,market=None,limit=50,base_mint=None,aggregation='pool',network=None):\n"
         "    if network in evm_data.NETWORKS or evm_data.chosen(market):return evm_data.trades(evm_data.selection(market,network,mint or base_mint),pool,limit,aggregation)\n"),
    ],
    'market_data.py': [
        ('from . import global_data\n', 'from . import global_data\nfrom . import evm_data\n'),
        ("    if record and record['mint']==t.get('mint') and record['coingecko']==t.get('coingecko'):\n",
         "    if record and record['mint']==t.get('mint') and record['coingecko']==t.get('coingecko') and record.get('network','solana')==(t.get('network') or 'solana'):\n"),
        ("'included':'Reviewed Solana mint identities, issuer-bound RWA tokens and explicit global exchange instruments.",
         "'included':'Reviewed Solana mint identities, issuer-bound RWA tokens, explicit global exchange instruments and reviewed Arbitrum One / Robinhood Chain token addresses."),
        ("            row={**t,'icon':token_icons.icon_url(t['mint']) or t.get('icon'),",
         "            row={**t,'network':t.get('network') or 'solana','icon':token_icons.icon_url(t['mint']) or t.get('icon'),"),
        # EVM rows are opt-in (networks=all): already-open tabs and a rolled-back frontend never receive rows they reject
        ("    async def snapshot(self,ident=None,compact=False,reviewed_only=False):\n", "    async def snapshot(self,ident=None,compact=False,reviewed_only=False,include_evm=False):\n"),
        ("            key=('reviewed',compact)\n", "            key=('reviewed',compact,include_evm)\n"),
        ("                result=await self._snapshot(None,compact,reviewed_only=True)\n", "                result=await self._snapshot(None,compact,reviewed_only=True,include_evm=include_evm)\n"),
        ("    async def _snapshot(self,ident=None,compact=False,reviewed_only=False):\n", "    async def _snapshot(self,ident=None,compact=False,reviewed_only=False,include_evm=False):\n"),
        ("    async def snapshot(view:str='full'):\n        data=await markets.snapshot(compact=view in ('table','home'),reviewed_only=True)\n",
         "    async def snapshot(view:str='full',networks:str='solana'):\n        data=await markets.snapshot(compact=view in ('table','home'),reviewed_only=True,include_evm=networks=='all' and view!='home')\n"),
        ("            rows.extend(table_row(r) if compact else r for r in extra)\n",
         "            rows.extend(table_row(r) if compact else r for r in extra)\n"
         "        if reviewed_only and ident is None and include_evm and evm_data.CATALOG:\n"
         "            rows.extend(table_row(r) if compact else r for r in await asyncio.to_thread(evm_data.reference_rows))\n"),
        ("    def engine_candles(response:Response,mint:str|None=None,pool:str|None=None,market:str|None=None,interval:str='1m',limit:int=300,before:int|None=None,gap_policy:str='recorded',aggregation:str='pool',history:str='executions'):\n",
         "    def engine_candles(response:Response,mint:str|None=None,pool:str|None=None,market:str|None=None,interval:str='1m',limit:int=300,before:int|None=None,gap_policy:str='recorded',aggregation:str='pool',history:str='executions',network:str|None=None):\n"
         "        if network not in (None,'solana',*evm_data.NETWORKS):raise HTTPException(422,'Unknown network.')\n"
         "        if network in evm_data.NETWORKS or evm_data.chosen(market):\n"
         "            t=evm_data.selection(market,network,mint)\n"
         "            return JSONResponse(engine_data.candles(network=t['network'],mint=t['mint'],pool_pin=pool,interval=interval,limit=limit,before=before,gap_policy=gap_policy,aggregation=aggregation,history=history),headers={'Access-Control-Allow-Origin':'*'})\n"),
        ("    def engine_trades(response:Response,mint:str|None=None,pool:str|None=None,market:str|None=None,limit:int=50,aggregation:str='pool'):\n",
         "    def engine_trades(response:Response,mint:str|None=None,pool:str|None=None,market:str|None=None,limit:int=50,aggregation:str='pool',network:str|None=None):\n"
         "        if network not in (None,'solana',*evm_data.NETWORKS):raise HTTPException(422,'Unknown network.')\n"
         "        if network in evm_data.NETWORKS or evm_data.chosen(market):\n"
         "            t=evm_data.selection(market,network,mint)\n"
         "            return JSONResponse(engine_data.trades(network=t['network'],mint=t['mint'],pool=pool,limit=limit,aggregation=aggregation),headers={'Access-Control-Allow-Origin':'*'})\n"),
        # identity-first view: a static catalog row, no engine or provider reads
        ("        if view=='identity':\n            # Approved identity only.",
         "        if view=='identity' and evm_data.chosen(ident):return JSONResponse(evm_data.identity(ident),headers={'Access-Control-Allow-Origin':'*','Cache-Control':'public, max-age=0, s-maxage=5, must-revalidate'})\n"
         "        if view=='identity':\n            # Approved identity only."),
        ("        if global_data.chosen(ident):return await markets.global_detail(ident)\n",
         "        if evm_data.chosen(ident):return await asyncio.to_thread(evm_data.detail,ident)\n"
         "        if global_data.chosen(ident):return await markets.global_detail(ident)\n"),
        ("        t=markets.token(ident)\n        # Asset history must not follow today's preferred-pool ranking.",
         "        if evm_data.chosen(ident):\n"
         "            t=evm_data.selection(ident)\n"
         "            return engine_data.candles(network=t['network'],mint=t['mint'],pool_pin=pool,aggregation=aggregation or ('pool' if pool else 'token'),history=history,interval=interval or {'minute':'1m','hour':'1h','day':'1d'}.get(frame,'1h'),before=before,gap_policy=gap_policy)\n"
         "        t=markets.token(ident)\n        # Asset history must not follow today's preferred-pool ranking."),
    ],
    'realtime.py': [
        ("    async def public_markets(request:Request):\n        async def load(): return await market_data.markets.snapshot(compact=True, reviewed_only=True)\n        return stream(request, ('markets',), load, candle=False, period=5, initial=await load())",
         "    async def public_markets(request:Request,networks:str='solana'):\n        include=networks=='all'\n        async def load(): return await market_data.markets.snapshot(compact=True, reviewed_only=True, include_evm=include)\n        return stream(request, ('markets',include), load, candle=False, period=5, initial=await load())"),
        ("            'solana':sum(r.get('network')!='exchange' for r in rows),\n",
         "            'solana':sum(r.get('network') in (None,'solana') for r in rows),\n"
         "            'evm':sum(r.get('network') in ('arbitrum','robinhood') for r in rows),\n"),
        ('    from . import engine_data, global_data, market_data\n',
         '    from . import engine_data, global_data, market_data, evm_data\n'),
        ("                             aggregation:str='token', interval:str='1m', history:str='full'):\n        if global_data.chosen(market):\n",
         "                             aggregation:str='token', interval:str='1m', history:str='full', network:str|None=None):\n"
         "        if network in evm_data.NETWORKS or evm_data.chosen(market):\n"
         "            t=evm_data.selection(market,network,mint)\n"
         "            if aggregation not in {'token','pool'} or pool and aggregation!='pool': raise HTTPException(422, 'Invalid chart scope.')\n"
         "            query=await threaded(engine_data.pin_stream_selection,dict(network=t['network'],mint=t['mint'],pool_pin=pool,aggregation=aggregation,interval=interval,limit=600,history=history,gap_policy='carry'),'candles')\n"
         "            async def load(): return await threaded(engine_data.candles, **query)\n"
         "            return stream(request, ('candles',encode(query)), load, mint=t['id'], initial=await load(), dependencies=frozenset(('evm_'+t['network'],'config')))\n"
         "        if global_data.chosen(market):\n"),
        ("    async def public_trades(request:Request, mint:str|None=None, pool:str|None=None, aggregation:str='token',market:str|None=None):\n",
         "    async def public_trades(request:Request, mint:str|None=None, pool:str|None=None, aggregation:str='token',market:str|None=None,network:str|None=None):\n"
         "        if network in evm_data.NETWORKS or evm_data.chosen(market):\n"
         "            t=evm_data.selection(market,network,mint)\n"
         "            if aggregation not in {'token','pool'} or pool and aggregation!='pool': raise HTTPException(422, 'Invalid tape scope.')\n"
         "            query=await threaded(engine_data.pin_stream_selection,dict(network=t['network'],mint=t['mint'],pool=pool,aggregation=aggregation,limit=100),'trades')\n"
         "            async def load(): return await threaded(engine_data.trades, **query)\n"
         "            return stream(request, ('trades',encode(query)), load, mint=t['id'], candle=False, initial=await load(), dependencies=frozenset(('evm_'+t['network'],'config')))\n"),
        ("                      dependencies=GLOBAL if claims.get('aggregation')=='exchange' else",
         "                      dependencies=frozenset(('evm_'+claims['network'],'config')) if claims.get('network','solana')!='solana' else GLOBAL if claims.get('aggregation')=='exchange' else"),
    ],
    'changes.py': [
        ("def dependencies(path, query):\n",
         "def dependencies(path, query):\n"
         "    if query.get('network') in ('arbitrum', 'robinhood'): return frozenset(('evm_' + query['network'], 'config'))\n"),
    ],
    'token_icons.py': [
        ("EXCHANGE_NAME=re.compile(r'exchange-binance-(?:spot|usdm)-[a-z0-9]{2,30}-[a-f0-9]{16}\\.webp')\n",
         "EXCHANGE_NAME=re.compile(r'exchange-binance-(?:spot|usdm)-[a-z0-9]{2,30}-[a-f0-9]{16}\\.webp')\n"
         "NETWORK_NAME=re.compile(r'(?:arbitrum|robinhood)-0x[0-9a-f]{40}-[a-f0-9]{16}\\.webp')\n"),
        ("            if not isinstance(value.get('icons',{}),dict) or not isinstance(value.get('exchangeIcons',{}),dict):raise ValueError('Invalid artwork manifest')\n",
         "            if not isinstance(value.get('icons',{}),dict) or not isinstance(value.get('exchangeIcons',{}),dict) or not isinstance(value.get('networkIcons',{}),dict):raise ValueError('Invalid artwork manifest')\n"),
        ("def icon_url(mint):\n    name=index().get(mint,{}).get('file','')\n",
         "def icon_url(mint,network='solana'):\n"
         "    if network!='solana':\n"
         "        row=manifest().get('networkIcons',{}).get(network+':'+str(mint),{});name=row.get('file','')\n"
         "        return '/api/public/token-icons/'+name if NETWORK_NAME.fullmatch(name) and name.startswith(network+'-'+str(mint)+'-') and row.get('identity')=={'network':network,'address':mint} else None\n"
         "    name=index().get(mint,{}).get('file','')\n"),
        ("        if not (NAME.fullmatch(name) or EXCHANGE_NAME.fullmatch(name)):raise HTTPException(404)\n",
         "        if not (NAME.fullmatch(name) or EXCHANGE_NAME.fullmatch(name) or NETWORK_NAME.fullmatch(name)):raise HTTPException(404)\n"),
    ],
    'store.py': [
        ('            c.execute("ALTER TABLE display_tokens ADD COLUMN quote_mint TEXT")\n',
         '            c.execute("ALTER TABLE display_tokens ADD COLUMN quote_mint TEXT")\n'
         "        if 'network' not in {r['name'] for r in c.execute('PRAGMA table_info(display_tokens)')}:\n"
         "            c.execute(\"ALTER TABLE display_tokens ADD COLUMN network TEXT NOT NULL DEFAULT 'solana'\")\n"),
    ],
    'app.py': [
        ("def candles(request:Request,market:str|None=None,mint:str|None=None,pool:str|None=None,interval:str='1m',limit:int=300,before:int|None=None,gap_policy:str='recorded',aggregation:str='pool',history:str='executions'):\n"
         "    api_user(request,'candles:read')\n"
         "    return JSONResponse(engine_data.candles(market=market,mint=mint,pool=pool,interval=interval,limit=limit,before=before,gap_policy=gap_policy,aggregation=aggregation,history=history))\n",
         "def candles(request:Request,market:str|None=None,mint:str|None=None,pool:str|None=None,interval:str='1m',limit:int=300,before:int|None=None,gap_policy:str='recorded',aggregation:str='pool',history:str='executions',network:str|None=None):\n"
         "    api_user(request,'candles:read')\n"
         "    return JSONResponse(engine_data.candles(market=market,mint=mint,pool=pool,interval=interval,limit=limit,before=before,gap_policy=gap_policy,aggregation=aggregation,history=history,network=network))\n"),
        ("def resolve_asset(request:Request,mint:str|None=None,pool:str|None=None,market:str|None=None,quote:str|None=None,aggregation:str='pool'):\n"
         "    api_user(request,'markets:read');return engine_data.resolve(mint,pool,market,quote,aggregation)\n",
         "def resolve_asset(request:Request,mint:str|None=None,pool:str|None=None,market:str|None=None,quote:str|None=None,aggregation:str='pool',network:str|None=None):\n"
         "    api_user(request,'markets:read');return engine_data.resolve(mint,pool,market,quote,aggregation,network)\n"),
        ("def asset_trades(request:Request,mint:str|None=None,pool:str|None=None,market:str|None=None,limit:int=50,aggregation:str='pool'):\n"
         "    api_user(request,'trades:read');return engine_data.trades(mint,pool,market,limit,aggregation=aggregation)\n",
         "def asset_trades(request:Request,mint:str|None=None,pool:str|None=None,market:str|None=None,limit:int=50,aggregation:str='pool',network:str|None=None):\n"
         "    api_user(request,'trades:read');return engine_data.trades(mint,pool,market,limit,aggregation=aggregation,network=network)\n"),
        ("    pool:str|None=Field(default=None,max_length=44)\n    market:str|None=Field(default=None,max_length=40)\n",
         "    pool:str|None=Field(default=None,max_length=66)\n    market:str|None=Field(default=None,max_length=40)\n"
         "    network:Literal['solana','arbitrum','robinhood']='solana'\n"),
        ("    if body.aggregation=='exchange':\n        if body.mint or body.pool:raise HTTPException(422,'Exchange grants require a market, without a Solana selector.')\n",
         "    if body.network!='solana':\n"
         "        if body.aggregation=='exchange' or body.market:raise HTTPException(422,'Network grants select an exact token address, without a market selector.')\n"
         "        t=engine_data.evm_data.selection(None,body.network,body.mint)\n"
         "        pool=engine_data.evm_data.pinned(t,body.pool)['pool'] if body.aggregation=='pool' else ''\n"
         "        token='td_'+secrets.token_urlsafe(32);expires=now()+300\n"
         "        with db() as c:\n"
         "            c.execute('DELETE FROM display_tokens WHERE expires<?',(now(),))\n"
         "            c.execute('INSERT INTO display_tokens(hash,key_id,origin,mint,pool,expires,aggregation,quote_mint,network,project_id) VALUES(?,?,?,?,?,?,?,?,?,?)',(digest(token),key['id'],target,t['mint'],pool,expires,body.aggregation,'USD',body.network,body.project_id))\n"
         "        return {'token':token,'expires':expires,'origin':target,'network':body.network,'mint':t['mint'],'pool':pool or None,'quoteMint':'USD','aggregation':body.aggregation,'mode':'executed_history','live':False}\n"
         "    if body.aggregation=='exchange':\n        if body.mint or body.pool:raise HTTPException(422,'Exchange grants require a market, without a Solana selector.')\n"),
        ("    if row['aggregation']=='token' and row['project_id'] and not any(",
         "    if row['aggregation']=='token' and row['project_id'] and ('network' not in row.keys() or row['network']=='solana') and not any("),
        ("def display_history(d,interval,limit,before=None,gap_policy='recorded',history='executions'):\n",
         "def display_history(d,interval,limit,before=None,gap_policy='recorded',history='executions'):\n"
         "    if d.get('network','solana')!='solana':\n"
         "        t=engine_data.evm_data.chosen(network=d['network'],mint=d['mint'])\n"
         "        if not t:raise HTTPException(409,'Market identity changed. Request a new grant.')\n"
         "        return engine_data.evm_data.candles(t,interval,limit,before,gap_policy,d['aggregation'],d['pool'] or None,history)\n"),
        ("    if d.get('aggregation')=='exchange':return engine_data.global_data.trades(d['asset'])\n",
         "    if d.get('network','solana')!='solana':return engine_data.evm_data.trades(engine_data.evm_data.chosen(network=d['network'],mint=d['mint']),d['pool'] or None,limit,d['aggregation'])\n"
         "    if d.get('aggregation')=='exchange':return engine_data.global_data.trades(d['asset'])\n"),
    ],
}


def review_entries(server):
    """Pin the reviewed EVM rows by network + address next to the existing Solana records."""
    review = json.loads((server / 'reviewed_markets.json').read_text())
    catalog = json.loads((HERE / 'ui-server/evm_catalog.json').read_text())['markets']
    known = {m['id'] for m in review['markets']}
    for t in catalog:
        if t['id'] not in known:
            review['markets'].append({'id': t['id'], 'network': t['network'], 'mint': t['mint'], 'coingecko': None,
                                      'reviewed_at': catalog and t.get('reviewed_at', '2026-10-18'), 'basis': 'em_catalog_review_v1'})
    return json.dumps(review, indent=1) + '\n'


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    server = Path(sys.argv[1])
    check = '--check' in sys.argv
    apply(server, PATCHES, [(HERE / 'ui-server/evm_data.py', 'evm_data.py'), (HERE / 'ui-server/evm_catalog.json', 'evm_catalog.json')], check=check)
    if not check:
        (server / 'reviewed_markets.json').write_text(review_entries(server))
        print('  reviewed_markets.json: +%d EVM records' % len(json.loads((HERE / 'ui-server/evm_catalog.json').read_text())['markets']))
