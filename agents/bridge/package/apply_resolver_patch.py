"""First-class Arbitrum / Robinhood markets in the intelligence resolver, plus Economic Machine wallet intents.

usage: apply_resolver_patch.py <new_release_dir> [--check] [--staging]
<new_release_dir> must be a fresh copy of the release ticking-intelligence.service runs. Anchored, all-or-nothing.
  * em_wallet_intent.py: deterministic wallet / holders / borrowing intents over the loopback em-ticking-bridge tools.
  * intelligence_app.py: wallet intents run first; an exact 0x token address opens its reviewed Arbitrum One or
    Robinhood Chain market (the console catalog now lists them natively); unknown 0x addresses get a clarification.
  * analyst_service.py: EVM catalog rows may carry 0x mint/pool selectors; candles still come from the console API.
The production Origin rule is untouched. --staging (never for production) adds loopback origins and points the
catalog at the staging console (127.0.0.1:19438) and the tools at the staging proxy (127.0.0.1:8893).
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from patchlib import apply  # noqa: E402

EVM_CHART = '''def evm_chart(address,rows,tool='chart'):
 """An exact 0x token address opens its reviewed network market; never a ticker or a guessed network."""
 from urllib.parse import quote
 a=address.lower();hits=[t for t in rows if t.get('network') in ('arbitrum','robinhood') and t.get('mint')==a and t.get('id')==t['network']+':'+a]
 if not hits:return {'state':'clarify','message':'That address is not a reviewed Arbitrum One or Robinhood Chain market. Paste a wallet address to open its dossier, or choose a listed market.'}
 names={'arbitrum':'Arbitrum One','robinhood':'Robinhood Chain'}
 choices=[{'title':str(t.get('name') or t.get('symbol'))+' · '+names[t['network']],'route':('#/terminal/' if tool=='terminal' else '#/token/')+quote(t['id'],safe='')} for t in hits[:4]]
 return {'state':'ready',**choices[0]} if len(choices)==1 else {'state':'choices','message':'This address is listed on more than one network.','choices':choices}

def resolve_native(plan,q,rows):'''


def patches(staging):
    app = [
        ("from analyst_service import analyze as run_analyst, requested as analyst_requested, parse_request as parse_analyst_request",
         "from analyst_service import analyze as run_analyst, requested as analyst_requested, parse_request as parse_analyst_request\n"
         "import em_wallet_intent  # Economic Machine wallet tools (loopback em-ticking-bridge)"),
        (" if analyst_requested(q):\n",
         " wallet=await asyncio.to_thread(em_wallet_intent.resolve,q,chart)\n if wallet is not None:\n"
         "  wallet.update(resolver='economic-machine-wallets',modelCalls=0,routingMs=round((time.perf_counter()-started)*1000,3))\n"
         "  return reply(wallet)\n if analyst_requested(q):\n"),
        ("def resolve_native(plan,q,rows):", EVM_CHART),
        (" if tool in ('chart','terminal') and kind=='evm':return {'state':'clarify','message':'Token-address charts currently accept Solana mints. Use a listed symbol for other markets.'}\n",
         " if tool in ('chart','terminal') and kind=='evm':return evm_chart(asset,rows,tool)\n"),
        (" if found and not found[0].startswith('0x'):return {'state':'ready','title':'Token chart',",
         " if found and found[0].startswith('0x'):return evm_chart(found[0],rows,tool)\n"
         " if found and not found[0].startswith('0x'):return {'state':'ready','title':'Token chart',"),
        ("rows=await current_catalog() if native['tool'] in ('chart','terminal') and not public_address(native['asset'] or '') else []",
         "rows=await current_catalog() if native['tool'] in ('chart','terminal') and public_address(native['asset'] or '')!='solana' else []"),
        ("rows=await current_catalog() if plan['tool'] in ('chart','terminal','position_plan') and not addresses(q) else []",
         "rows=await current_catalog() if plan['tool'] in ('chart','terminal','position_plan') and not [a for a in addresses(q) if not a.startswith('0x')] else []"),
    ]
    app.append(("http://127.0.0.1:19428/api/public/markets?view=table", "http://127.0.0.1:%d/api/public/markets?view=table&networks=all" % (19438 if staging else 19428)))
    if staging:
        app += [
            (" if request.headers.get('origin')!='https://ticking.dev':return reply({'error':'Open this tool on ticking.dev.'},403)",
             " if request.headers.get('origin')!='https://ticking.dev' and not re.fullmatch(r'http://(127\\.0\\.0\\.1|localhost):\\d{2,5}',request.headers.get('origin') or ''):return reply({'error':'Open this tool on ticking.dev.'},403)  # STAGING ONLY"),
        ]
    svc = [
        ("  if source.get(key) and not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',source[key]):raise ValueError('Invalid source address.')",
         "  if source.get(key) and not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',source[key]) and not (row.get('network') in ('arbitrum','robinhood') and re.fullmatch(r'0x(?:[0-9a-f]{40}|[0-9a-f]{64})',source[key])):raise ValueError('Invalid source address.')"),
    ]
    if staging:
        svc.append(("url='http://127.0.0.1:19428/api/public/engine/candles?'", "url='http://127.0.0.1:19438/api/public/engine/candles?'"))
    return {'intelligence_app.py': app, 'analyst_service.py': svc}


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    release, staging, check = Path(sys.argv[1]), '--staging' in sys.argv, '--check' in sys.argv
    if (release / 'em_wallet_intent.py').exists():
        sys.exit('already patched: ' + str(release))
    intent = HERE / 'intelligence/em_wallet_intent.py'
    if staging and not check:
        staged = release / '.em_wallet_intent.staging.py'
        staged.write_text(intent.read_text().replace("'http://127.0.0.1:8896/api/public/em/tools/'", "'http://127.0.0.1:8893/api/public/em/tools/'")
                          .replace("'http://127.0.0.1:19428')", "'http://127.0.0.1:19438')"))
        intent = staged
    apply(release, patches(staging), [(intent, 'em_wallet_intent.py')], check=check)
    if staging and not check:
        intent.unlink()
