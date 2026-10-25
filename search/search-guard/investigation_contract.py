"""One public investigation identity shared by Ask, charts and retained evidence.

Client context selects a subject and time. It never establishes coverage, a
balance, a label or permission. Those remain properties of returned source data.
"""
import contextvars
import datetime
import hashlib
import json
import re
from urllib.parse import quote,urlencode
from economic_machine import chain_catalog

SCHEMA='ticking.investigation-context.v1'
CURRENT=contextvars.ContextVar('investigation_context',default=None)
ADDR=re.compile(r'^0x[0-9a-f]{40}$')
class Clarify(ValueError):pass
WINDOWS={'1h','24h','7d','30d','90d','all'}

def normalize(raw):
    if raw is None:return None
    if type(raw) is not dict or set(raw)-{'schema','subject','selection','task'} or raw.get('schema')!=SCHEMA:raise ValueError('Invalid investigation context.')
    subject=raw.get('subject');selection=raw.get('selection',{})
    if type(subject) is not dict or set(subject)-{'kind','chain','address','asset','symbol','protocol','entity'}:raise ValueError('Invalid investigation subject.')
    s={k:v for k,v in subject.items() if v is not None}
    if s.get('kind') not in {'token','wallet','protocol','market','institution'}:raise ValueError('Choose a token, wallet, protocol or market.')
    if 'chain' in s and s['chain'] not in chain_catalog.ids():raise ValueError('Unknown chain; no other network was substituted.')
    if 'address' in s:
        if not isinstance(s['address'],str):raise ValueError('Invalid public address.')
        s['address']=s['address'].lower()
        if not ADDR.fullmatch(s['address']):raise ValueError('Invalid EVM public address.')
    if 'symbol' in s and (not isinstance(s['symbol'],str) or not re.fullmatch(r'[A-Za-z0-9._:/-]{1,40}',s['symbol'])):raise ValueError('Invalid display symbol.')
    if 'asset' in s:
        if not isinstance(s['asset'],str) or not re.fullmatch(r'[-A-Za-z0-9_.:]{1,100}',s['asset']):raise ValueError('Invalid exact market.')
        if ':0x' in s['asset']:
            s['asset']=s['asset'].lower()
            if not chain_catalog.market(s['asset']):raise ValueError('Invalid chain and contract identity.')
            chain,address=s['asset'].split(':')
            if s.get('chain',chain)!=chain or s.get('address',address)!=address:raise ValueError('The selected chain and token contract do not match.')
            s.update(chain=chain,address=address)
    if s.get('kind')=='token' and s.get('asset') and not chain_catalog.market(s['asset']):s['kind']='market'
    if s['kind']=='token' and not s.get('asset'):raise ValueError('Choose an exact token market.')
    if s['kind']=='wallet' and not s.get('address'):raise ValueError('Choose a public wallet address.')
    if s['kind']=='institution' and (not isinstance(s.get('entity'),str) or not re.fullmatch(r'[a-z0-9_-]{1,80}',s['entity'])):raise ValueError('Invalid named institution identity.')
    if s['kind']=='protocol' and (not isinstance(s.get('protocol'),str) or not re.fullmatch(r'[a-z0-9_-]{1,80}',s['protocol'])):raise ValueError('Invalid protocol identity.')
    if type(selection) is not dict or set(selection)-{'block','time','from','to','window','interval'}:raise ValueError('Invalid investigation time selection.')
    t={k:v for k,v in selection.items() if v is not None}
    for k in ['block','time','from','to']:
        if k in t and (type(t[k]) is not int or not 0<=t[k]<2**53):raise ValueError('Invalid requested '+k+'.')
    if 'window' in t and t['window'] not in WINDOWS:raise ValueError('Unsupported investigation window.')
    if 'interval' in t and (not isinstance(t['interval'],str) or not re.fullmatch(r'[1-9][0-9]{0,3}[mhdw]',t['interval'])):raise ValueError('Invalid chart interval.')
    if ('from' in t)!=('to' in t) or ('from' in t and t['from']>t['to']):raise ValueError('Choose a complete ordered time range.')
    result={'schema':SCHEMA,'subject':s,'selection':t}
    if raw.get('task') is not None:
        task=raw['task']
        if type(task) is not dict or set(task)-{'intent'} or not isinstance(task.get('intent'),str) or not re.fullmatch(r'[a-z_]{1,64}',task['intent']):raise ValueError('Invalid retained task intent.')
        result['task']={'intent':task['intent']}
    return result

def prepare(q,raw,chart=None,previous=None):
    ctx=normalize(raw)
    if ctx is None and isinstance(chart,dict) and chart.get('asset'):
        ctx=normalize({'schema':SCHEMA,'subject':{'kind':'token' if ':0x' in chart['asset'] else 'market','asset':chart['asset'],'symbol':chart.get('symbol')},'selection':{'interval':chart.get('interval')}})
    prior=normalize(previous)
    if prior:
        if re.search(r'그\s?기관|그\s?거래소|that\s+(?:institution|exchange)',q,re.I) and prior['subject'].get('kind')=='institution':ctx=prior
        elif not re.search(r'이\s?토큰|this\s+(?:token|market)|이\s?지갑|this\s?wallet',q,re.I) and re.search(r'^(?:그럼|그러면|그거|그걸|and\s+what|what\s+about|then\b)',q.strip(),re.I):ctx=prior
    # A wallet follow-up can occur anywhere in the sentence, not only after
    # "then". Keep its public address but resolve the explicitly named network.
    wallet_followup=bool(re.search(r'(?i)\b(?:same|that|this)\s+(?:wallet|address)\b|같은\s?(?:지갑|주소)|그\s?(?:지갑|주소)|이\s?지갑',q))
    if ctx is None and prior and prior['subject'].get('kind')=='wallet' and wallet_followup:ctx=prior
    # An explicit exact subject in this message supersedes a previous selection.
    explicit=re.search(r'\b([a-z][a-z0-9-]*):(0x[0-9a-fA-F]{40})\b',q)
    if explicit:
        asset=explicit.group(1).lower()+':'+explicit.group(2).lower()
        old=(ctx or {}).get('subject') or {};chain,address=asset.split(':')
        wallet_words=bool(re.search(r'(?i)\bwallet|holdings|portfolio|transactions?|transfers?|trace|funding\b|지갑|보유\s?자산|거래|이체|추적',q))
        token_words=bool(re.search(r'(?i)\bholders?|who\s+held|token|candles?\b|보유자|홀더|토큰|캔들',q))
        same_address=old.get('chain')==chain and old.get('address')==address
        kind='wallet' if wallet_words and not token_words else 'token' if token_words else old.get('kind') if same_address else 'wallet' if re.search(r'(?i)graph|connections?|connected|그래프|연결',q) else 'market'
        selected={'kind':'wallet','chain':chain,'address':address} if kind=='wallet' else {'kind':kind,'asset':asset}
        same_subject=same_address and (old.get('kind')==kind or old.get('kind') in ('market','token') and kind in ('market','token'))
        ctx=normalize({'schema':SCHEMA,'subject':selected,'selection':dict((ctx or {}).get('selection') or {}) if same_subject else {}})
    if ctx and not explicit and ctx['subject'].get('kind')=='wallet':
        chains=chain_catalog.hints(q)
        network_selection=bool(re.search(r'(?i)\bon\s+(?:the\s+)?[a-z가-힣]|[a-z가-힣](?:에서|에서는)|\b(?:chain|network)\b|체인|네트워크',q))
        if network_selection and len(chains)>1:raise Clarify('Choose one exact network for this wallet; the previous chain was not silently reused.')
        if network_selection and len(chains)==1 and chains[0]!=ctx['subject'].get('chain'):
            if 'block' in ctx['selection']:raise Clarify('Block numbers are chain-specific. Choose the requested block on the new network or explicitly select the current view.')
            ctx=normalize({**ctx,'subject':{**ctx['subject'],'chain':chains[0]}})
    if ctx is None:
        found_addresses=set(x.lower() for x in re.findall(r'(?i)\b0x[0-9a-f]{40}\b',q));chains=chain_catalog.hints(q)
        if len(found_addresses)==1:
            address=next(iter(found_addresses));chain=chains[0] if len(chains)==1 else None
            token=bool(chain and re.search(r'(?i)holders?|who\s+held|accumulat|보유자|홀더',q))
            s={'kind':'token','asset':chain+':'+address} if token else {'kind':'wallet','address':address,**({'chain':chain} if chain else {})}
            ctx=normalize({'schema':SCHEMA,'subject':s,'selection':{}})
    found=re.findall(r'(?i)\bblock\s*#?\s*(\d+)\b|블록\s*#?\s*(\d+)|(\d+)\s*번?\s*블록',q)
    blocks={int(next(x for x in row if x)) for row in found}
    if len(blocks)>1:raise Clarify('Choose one exact block for this request.')
    dates=re.findall(r'\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|\s*UTC)?)?\b',q)
    if len(set(dates))>1:raise Clarify('Choose one time, or use the investigation range controls.')
    if blocks or dates:
        if ctx is None:ctx={'schema':SCHEMA,'subject':{},'selection':{}}
        t=ctx['selection']
        for key in ('block','time','from','to','window'):t.pop(key,None)
        if blocks:t['block']=blocks.pop();t.pop('time',None)
        elif dates:
            stamp=dates[0].replace(' UTC','').rstrip('Z');t['time']=int(datetime.datetime.fromisoformat(stamp).replace(tzinfo=datetime.timezone.utc).timestamp());t.pop('block',None)
    windows=re.findall(r'(?i)\b(1h|24h|7d|30d|90d|all)\b',q)
    if len(set(x.lower() for x in windows))>1:raise Clarify('Choose one investigation window.')
    calendar_week=bool(re.search(r'이번\s?주|\bthis\s+week\b',q,re.I));calendar_today=bool(re.search(r'오늘|\btoday\b',q,re.I))
    if not windows and not (calendar_week or calendar_today):
        if re.search(r'7\s*일|일주일|\bweek\b|\b7\s*days?\b',q,re.I):windows=['7d']
        elif re.search(r'24\s*시간|하루|\b24\s*hours?\b',q,re.I):windows=['24h']
        elif re.search(r'30\s*일|한\s*달|\bmonth\b|\b30\s*days?\b',q,re.I):windows=['30d']
        elif re.search(r'90\s*일|\b90\s*days?\b',q,re.I):windows=['90d']
    if ctx and (windows or calendar_week or calendar_today):
        if blocks or dates:raise Clarify('Choose one historical point or one period; neither was silently discarded.')
        for key in ('block','time','from','to','window'):ctx['selection'].pop(key,None)
        if calendar_week or calendar_today:
            now=datetime.datetime.now(datetime.timezone.utc);start=now.replace(hour=0,minute=0,second=0,microsecond=0)
            if calendar_week:start-=datetime.timedelta(days=now.weekday())
            ctx['selection'].update({'from':int(start.timestamp()),'to':int(now.timestamp())})
        else:ctx['selection']['window']=windows[-1].lower()
    if not windows and not (calendar_week or calendar_today) and re.search(r'graph|connected|holders?|accumulat|연결|보유자|매집',q,re.I) and re.search(r'\b\d+\s*(?:days?|hours?|weeks?|months?|years?|d|h|w|y)\b',q,re.I):raise Clarify('This reader does not support that requested period. Choose a supported window or exact point; no all-time period was substituted.')
    if ctx:
        if chart and ctx['subject'].get('asset')==chart.get('asset') and chart.get('interval'):ctx['selection'].setdefault('interval',chart['interval'])
        CURRENT.set(ctx)
    return ctx

def selection():return (CURRENT.get() or {}).get('selection') or {}
def subject():return (CURRENT.get() or {}).get('subject') or {}

def validate_evidence(card,*,market=None,address=None,chain=None):
    if not isinstance(card,dict):raise LookupError('The source returned an invalid evidence record.')
    expected_chain=market.split(':')[0] if market else chain
    observed=card.get('chain') or card.get('network') or str(card.get('market') or '').partition(':')[0] or None
    if expected_chain and observed and observed!=expected_chain:raise LookupError('The returned evidence belongs to a different chain. No network was substituted.')
    if market:
        observed_market=card.get('market')
        token=card.get('token')
        if observed_market and observed_market.lower()!=market.lower() or token and token.lower()!=market.split(':')[1].lower():raise LookupError('The returned evidence belongs to a different token contract.')
    if address and card.get('address') and card['address'].lower()!=address.lower():raise LookupError('The returned evidence belongs to a different wallet.')
    return card

def evidence(card):
    if not isinstance(card,dict):return {'state':'not_requested'}
    snaps=card.get('snaps') if isinstance(card.get('snaps'),list) else []
    sheet=card.get('sheet') if isinstance(card.get('sheet'),dict) else {}
    status=card.get('status');status=status.get('state') if isinstance(status,dict) else status
    held=bool(status=='held' or card.get('held') or sheet.get('held') or any(s.get('held') for s in snaps))
    unavailable=card.get('available') is False or status in {'unavailable','unsupported','coming'}
    state='held' if held else status if status in ('failed','unverified','incomplete') else 'preparing' if status=='preparing' else 'unavailable' if unavailable else 'partial' if card.get('complete') is False or any(s.get('available') is False or s.get('complete') is False for s in snaps) else 'returned'
    out={'state':state,'classification':card.get('class'),'reasonKo':card.get('reasonKo'),'sourceStatus':card.get('status'),'asOf':card.get('asOf',card.get('as_of')),'coverage':card.get('coverage'), 'heldReason':sheet.get('heldReason') or card.get('heldReason') or card.get('reason')}
    if snaps:out['points']=[{k:s.get(k) for k in ['requestedBlock','requestedTime','effectiveBlock','effectiveTime','timeBasis','available','complete','coverage','supplyBlock','supplyBasis','supplyAvailable','code','reason'] if k in s} for s in snaps[:8]]
    return out

def decorate(value):
    if not isinstance(value,dict) or 'investigation' in value:return value
    ctx=CURRENT.get();card=value.get('card')
    if not ctx and isinstance(card,dict):
        if chain_catalog.market(card.get('market')):ctx=normalize({'schema':SCHEMA,'subject':{'kind':'token','asset':card['market'],'symbol':card.get('symbol')},'selection':{}})
        elif card.get('address') and card.get('chain') in chain_catalog.evm_ids():ctx=normalize({'schema':SCHEMA,'subject':{'kind':'wallet','chain':card['chain'],'address':card['address']},'selection':{}})
    if not ctx or not ctx.get('subject',{}).get('kind'):return value
    s=ctx['subject'];identity=hashlib.sha256(json.dumps(ctx,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:24]
    actions=[];sel=ctx['selection'];params={k:v for k,v in sel.items() if k in {'block','time','from','to','window','interval'}}
    if s.get('asset'):
        asset=quote(s['asset'],safe='')
        actions.append({'kind':'open','title':'Open chart','route':'#/console/workspace/'+asset+('?' +urlencode(params) if params else ''),'context':ctx})
        if chain_catalog.market(s['asset']):actions.append({'kind':'open','title':'Holders and history','route':'#/investigate/'+quote(s['asset'],safe=':')+('?' +urlencode(params) if params else ''),'context':ctx})
    if s.get('kind')=='wallet' and s.get('chain'):
        actions.append({'kind':'open','title':'Open wallet','route':'#/address/'+quote(s['chain']+':'+s['address'],safe=':')+('?' +urlencode(params) if params else ''),'context':ctx})
    if params:
        for action in actions:
            action.update(kind='context_required',viewExecution='unverified',requiresContextApplication=list(params),title=action['title']+' · selected period must be applied')
    return {**value,'investigation':{'schema':'ticking.investigation-result.v1','contextId':identity,'context':ctx,'evidence':evidence(card),'actions':actions}}
