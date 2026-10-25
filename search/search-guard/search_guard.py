"""Search robustness layer (search-guard-1) in front of route_unmetered.
1 normalize input  2 repair addresses  3 phrase routes (markets / whale tracker / chains)
4 fuzzy alias index (markets, institutions, people, all web3 chains) used to rewrite typos and
  as the deterministic answer before any model call  5 never a 500.
Alias sources are files in this release; truncated 0x (>=8 hex) resolve against labeled/mapped ADDR_SET; command/token vocab uses edit-distance-2; tx hashes tolerate missing 0x / glued tx0x."""
import json,re,unicodedata,time,os,urllib.request
from pathlib import Path
from urllib.parse import quote
from starlette.requests import Request
HERE=Path(__file__).resolve().parent
VERSION='search-guard-7'
# ---------- 1. normalization
_INJ=[r'(?is)<\s*(script|style|iframe)[^>]*>.*?<\s*/\s*\1\s*>',r'(?is)\b(?:ignore|disregard|forget)\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?).*$',r'(?is)\b(?:print|reveal|show)\s+(?:your\s+)?system\s+prompt.*$',
 r'(?s)<\s*/?\s*[a-zA-Z][^>]{0,200}>',r'\$\{[^}]{0,200}\}',r'\{\{[^}]{0,200}\}\}',r"(?i)['\"]\s*;?\s*(?:drop|delete|insert|update|select)\b.*$",r'(?i)\s(?:or|and)\s+1\s*=\s*1\b.*$',r'--\s*$',r';\s*$']
_FILLER=r'(?:yo|bro|bruh|pls|plz|please|hey|lol|lmao|idk|ffs|fucking|fuckin|fuck|damn|wtf|shit|ㅋ+|ㅎ+|ㅇㅇ|ㄱㄱ|ㅅㅂ|시발|씨발|존나|졸라|개|아|어|음|저기|그\s*뭐더라|뭐냐|이거|can\s+u\s+check|can\s+you\s+check|tell\s+me\s+about|what\s+about|check)'
def normalize(q):
 q=q.translate({i:i-0xFEE0 for i in range(0xFF01,0xFF5F)}|{0x3000:32});q=unicodedata.normalize('NFC',q)
 q=''.join(' ' if unicodedata.category(c) in ('Zs',) else c for c in q if unicodedata.category(c) not in ('Cf','Cc','Co','Cs','So','Sk') or c in '\n\t')
 q=re.sub(r'[\u2600-\u27bf\U0001F000-\U0001FFFF\ufe0f]','',q)
 for p in _INJ:q=re.sub(p,' ',q)
 q=re.sub(r'\s+',' ',q).strip()
 for _ in range(4):
  q2=re.sub(r'\s*[!?.,~…]+$','',q).strip()
  q2=re.sub(r'(?i)^(?:'+_FILLER+r')(?:\s+|$)','',q2);q2=re.sub(r'(?i)(?:^|\s+)(?:'+_FILLER+r')$','',q2).strip()
  if q2==q:break
  q=q2 or q
 q=re.sub(r'\s*[!?.,~…]+$','',q).strip()
 return fix_phrase_words(q)
PHRASE_VOCAB=sorted(set('graph counterparties counterparty connections research trendline draw pump dump bollinger fibonacci markets market trending tokens token coins gainers losers movers whales whale tracker alerts biggest largest wallets wallet list compare chain network mainnet bought sold large trades today holdings inflows outflows transactions transaction trace stats price chart charts terminal show open deep dive news macd rsi ema sma vwap fibonacci fib bands support resistance indicators volume'.split()))
def fix_phrase_words(q):
 q=re.sub(r'(?i)(?<![0-9A-Za-z])alll(?![0-9A-Za-z])','all',q)
 q=re.sub(r'(?i)(?<![0-9A-Za-z])(?:markett|marktes|marktd|markse|markted|msrkett|makett|makte)(?![0-9A-Za-z])','markets',q)
 def fix(m):
  w=m.group(0);lw=w.lower()
  if len(lw)<3 or lw in PHRASE_VOCAB or (IDX and lw in IDX.alias) or _is_word(lw):return w
  maxd=2 if len(lw)>=5 else 1
  best=sorted((_dl(lw,v),v) for v in PHRASE_VOCAB if abs(len(v)-len(lw))<=maxd)
  if best and best[0][0]<=maxd and (len(best)==1 or best[1][0]>best[0][0]):return best[0][1]
  return w
 return re.sub(r'(?<![0-9A-Za-z])[A-Za-z]{3,}(?![0-9A-Za-z])',fix,q)
# ---------- 2. address repair
HEX=r'[0-9a-fA-F]'
def repair(q):
 # peel glued tx0xHASH / transaction0xHASH (lookbehind otherwise fails on trailing x of tx)
 q=re.sub(r'(?i)(?<![0-9A-Za-z])(?:tx|txn|transaction|트랜잭션)\s*(0[xX]'+HEX+r'{64})(?![0-9A-Za-z])',lambda m:'0x'+m[1][2:].lower(),q)
 q=re.sub(r'(?i)(?<![0-9A-Za-z])(?:tx|txn|transaction|트랜잭션)\s*('+HEX+r'{64})(?![0-9A-Za-z])',lambda m:'0x'+m[1].lower() if re.search('[0-9]',m[1]) and re.search('[a-fA-F]',m[1]) else m[0],q)
 q=re.sub(r'(?<![0-9A-Za-z])[xX]+(0[xX]'+HEX+'{40}(?![0-9A-Za-z])|0[xX]'+HEX+'{64}(?![0-9A-Za-z]))',lambda m:m[1],q)
 q=re.sub(r'(?<![0-9A-Za-z])('+HEX+r'{64})(?![0-9A-Za-z])',lambda m:'0x'+m[1].lower() if re.search('[0-9]',m[1]) and re.search('[a-fA-F]',m[1]) else m[0],q)
 q=re.sub(r'(?<![0-9A-Za-z])0X('+HEX+'{40}|'+HEX+'{64})(?![0-9A-Za-z])',lambda m:'0x'+m[1],q)
 q=re.sub(r'(?<![0-9A-Za-z])(0[xX]'+HEX+r'{4,60})\s+('+HEX+r'{4,60})(?![0-9A-Za-z])',lambda m:'0x'+m[1][2:]+m[2] if len(m[1])-2+len(m[2]) in (40,64) else m[0],q)
 q=re.sub(r'(?<![0-9A-Za-z])0x('+HEX+r'{64})(?![0-9A-Za-z])',lambda m:'0x'+m[1].lower(),q)
 q=re.sub(r'(?<![0-9A-Za-z])('+HEX+r'{40})(?![0-9A-Za-z])',lambda m:'0x'+m[1] if re.search('[0-9]',m[1]) and re.search('[a-fA-F]',m[1]) else m[0],q)
 def fix_case(m):
  a=m[0];return '0x'+a[2:].lower() if a[2:].isupper() else a
 return re.sub(r'(?<![0-9A-Za-z])0x'+HEX+r'{40}(?![0-9A-Za-z])',fix_case,q)
FULL=re.compile(r'(?<![0-9A-Za-z])0x'+HEX+r'{40}(?![0-9A-Za-z])')
PART=re.compile(r'(?<![0-9A-Za-z])0x('+HEX+r'{6,39})(?![0-9A-Za-z])')
TX=re.compile(r'(?<![0-9A-Za-z])0x'+HEX+r'{64}(?![0-9A-Za-z])')
TX_PART=re.compile(r'(?i)(?<![0-9A-Za-z])(?:(?:tx|txn|transaction|트랜잭션)\s*)?0x('+HEX+r'{48,63})(?![0-9A-Za-z])')
SOLA=re.compile(r'(?<![0-9A-Za-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![0-9A-Za-z])')
def sol_addr(q):
 m=[x for x in SOLA.findall(q) if re.search('[0-9]',x) and re.search('[a-z]',x) and re.search('[A-Z]',x)]
 return m[0] if len(m)==1 else None
# ---------- 4. alias index
CHAIN_WORD=r'(?:chain|체인|network|네트워크|mainnet|메인넷|l1|l2|tvl|stats?|statistics|정보|통계|compare|비교|overview|생태계|ecosystem)'
KO_CHAIN={'bsc':['bnb chain','bnb smart chain','bsc','바낸체인','바이낸스 체인','비엔비 체인','비엔비체인','bnb 체인'],'tron':['tron','트론'],'avalanche':['avalanche','아발란체','avax c-chain','avax'],'ton':['ton','톤','톤체인'],'sui':['sui','수이'],'aptos':['aptos','앱토스'],'bitcoin':['bitcoin','비트코인'],'solana':['solana','솔라나'],'ethereum':['ethereum','이더리움','eth mainnet','mainnet','이더리움 메인넷'],'near':['near','니어'],'cardano':['cardano','카르다노','에이다'],'polygon':['polygon','matic','폴리곤','매틱','polygon pos'],'arbitrum':['arbitrum','arbitrum one','arb1','arb','아비트럼','아비트럼 원'],'optimism':['optimism','op mainnet','op','옵티미즘'],'base':['base','base mainnet','베이스'],'linea':['linea','linea mainnet','리니아','리네아'],'unichain':['unichain','유니체인'],'hyperevm':['hyperevm','hyper evm','hyperliquid evm','hyperliquid','하이퍼evm','하이퍼 evm','하이퍼리퀴드'],'xlayer':['xlayer','x layer','x-layer','okx x layer','x레이어','엑스레이어'],'robinhood':['robinhood','robinhood chain','로빈후드','로빈후드 체인'],'arc':['arc','arc network','아크'],'monad':['monad','모나드'],'berachain':['berachain','베라체인'],'sei':['sei','세이'],'starknet':['starknet','스타크넷'],'zksync':['zksync','zksync era','지케이싱크'],'scroll':['scroll','스크롤'],'mantle':['mantle','맨틀'],'xrpl':['xrpl','xrp ledger','리플 체인'],'stellar':['stellar','스텔라'],'dogecoin':['dogecoin','도지코인'],'litecoin':['litecoin','라이트코인'],'cronos':['cronos','크로노스'],'injective':['injective','인젝티브'],'algorand':['algorand','알고랜드'],'hedera':['hedera','헤데라'],'tezos':['tezos','테조스'],'sonic':['sonic','소닉'],'blast':['blast','블라스트'],'megaeth':['megaeth','메가이더']}
PEOPLE=[('vitalik_buterin','Vitalik Buterin',['vitalik','vitalik buterin','vitalik.eth','비탈릭','부테린','비탈릭 부테린'],'0xd8da6bf26964af9d7eed9e03e53415d37aa96045'),('justin_sun','Justin Sun',['justin sun','저스틴 선','저스틴선'],None),('cz','Changpeng Zhao',['cz','changpeng zhao','창펑자오','창펑 자오'],None)]
EXTRA_INST=[('coinbase','Coinbase',['코베','coinbase exchange']),('binance','Binance',['binance 14','binance hot wallet','바이낸스 핫월렛']),('okx','OKX',['오케이엑스']),('jump_trading','Jump Trading',['jump trading','jump crypto','점프','점프 트레이딩']),('wintermute','Wintermute',['wintermute','윈터뮤트']),('kraken','Kraken',['kraken','크라켄']),('bybit','Bybit',['bybit','바이빗']),('upbit','Upbit',['upbit','업비트']),('bithumb','Bithumb',['bithumb','빗썸']),('a16z','a16z',['a16z','andreessen horowitz']),('paradigm','Paradigm',['paradigm','패러다임']),('microstrategy','MicroStrategy',['microstrategy','strategy inc','마이크로스트래티지']),('tether','Tether',['tether treasury','테더 재단']),('ethereum_foundation','Ethereum Foundation',['ethereum foundation','이더리움 재단'])]
COMMON=set("""a an the and or of to in on for at by is are be me my you your it its this that these those what which who whom whose when where why how show open view see get give find list all any some top today now chart charts price prices token tokens coin coins market markets wallet wallets address holdings holders hold holds inflows outflows flows flow trace history pnl stats info chain chains network compare tvl about check please map terminal pro buy sell bought sold whale whales tracker alerts alert large biggest trades trade gainers losers trending new hot volume tell help docs pricing news more less up down long short swap send bridge fee fees owns own day week month year hour""".split())
class Index:
 def __init__(s,markets,chains_rows,institutions):
  s.alias={}
  def add(a,item):
   k=re.sub(r'\s+',' ',unicodedata.normalize('NFKC',a).casefold().strip())
   if len(k)>=2 and item not in s.alias.setdefault(k,[]):s.alias[k].append(item)
  for r in markets:
   if r.get('symbol'):add(r['symbol'],('token',r['id'],r['symbol']))
   if r.get('name') and len(r['name'])>=3:add(r['name'],('token',r['id'],r['symbol']))
  from assistant_entity_intent import KOREAN_TOKENS
  bysym={}
  for r in markets:bysym.setdefault(r.get('symbol'),r)
  allsym={}
  for r in markets:allsym.setdefault(r.get('symbol'),[]).append(r)
  for k,v in {**KOREAN_TOKENS,'리플':'XRP','도지':'DOGE','도지코인':'DOGE','페페':'PEPE','주피터':'JUP','봉크':'BONK','체인링크':'LINK','유니스왑':'UNI','위프':'WIF','테더':'USDT','바이낸스코인':'BNB','트론':'TRX','에이다':'ADA','하이프':'HYPE'}.items():
   for r in allsym.get(v,[]):add(k,('token',r['id'],v))
  for id,name,al in institutions:
   add(name,('entity',id,name))
   for a in al:add(a,('entity',id,name))
  for id,name,al,addr in PEOPLE:
   for a in al+[name]:add(a,('person',id,name,addr))
  s.chain_names={}
  for c in chains_rows:
   s.chain_names[c['id']]=c['name']
   for a in [c['id'],c['name']]+KO_CHAIN.get(c['id'],[]):add(a,('chain',c['id'],c['name']))
  for cid,al in KO_CHAIN.items():
   s.chain_names.setdefault(cid,cid.title())
   for a in al:add(a,('chain',cid,s.chain_names[cid]))
  s.keys=sorted(s.alias,key=len,reverse=True)
  s.dele={}
  for k in s.keys:
   if len(k)<3 or not re.fullmatch(r'[a-z0-9 .-]+',k):continue
   for v in _deletes(k,2 if len(k)>=5 else 1):s.dele.setdefault(v,set()).add(k)
 def spans(s,q):
  """Longest exact alias spans (word-bounded), then fuzzy single words/bigrams. Returns [(start,end,alias,dist)]."""
  low=q.casefold();taken=[0]*len(low);out=[]
  words=[(m.start(),m.end()) for m in re.finditer(r'[0-9a-z가-힣._-]+',low)]
  for n in (4,3,2,1):
   for i in range(len(words)-n+1):
    a,b=words[i][0],words[i+n-1][1];t=re.sub(r'\s+',' ',low[a:b])
    if any(taken[a:b]):continue
    if t in s.alias:
     # Do not bind filler/COMMON unigrams to token tickers (e.g. "me" → ME in "show me SKR").
     items=s.alias[t]
     if n==1 and (t in COMMON or len(t)<=2) and all(it[0]=='token' for it in items):
      pass
     else:
      out.append((a,b,t,0));taken[a:b]=[1]*(b-a)
    else:
     # Korean particles: 비탈릭이, 바이낸스는
     m=re.fullmatch(r'(.+?)(?:이|가|은|는|을|를|의|에서|에|도|랑|이랑|꺼|거)',t) if n==1 else None
     if m and m.group(1) in s.alias and re.search('[가-힣]',m.group(1)):out.append((a,a+len(m.group(1)),m.group(1),0));taken[a:b]=[1]*(b-a)
  for n in (2,1):
   for i in range(len(words)-n+1):
    a,b=words[i][0],words[i+n-1][1];t=re.sub(r'\s+',' ',low[a:b])
    if any(taken[a:b]) or len(t)<3 or t in COMMON or not re.fullmatch(r'[a-z0-9 .-]+',t) or re.fullmatch(r'0x[0-9a-f]*',t):continue
    if n==1 and _is_word(t):continue
    best=_fuzzy(s,t)
    if best:out.append((a,b,best[0],best[1]));taken[a:b]=[1]*(b-a)
  return sorted(out)
def _deletes(w,d):
 res={w};frontier={w}
 for _ in range(d):
  frontier={x[:i]+x[i+1:] for x in frontier for i in range(len(x))};res|=frontier
 return res
def _dl(a,b):
 d={(i,-1):i+1 for i in range(-1,len(a)+1)};d.update({(-1,j):j+1 for j in range(-1,len(b)+1)})
 for i in range(len(a)):
  for j in range(len(b)):
   c=a[i]!=b[j];d[i,j]=min(d[i-1,j]+1,d[i,j-1]+1,d[i-1,j-1]+c)
   if i and j and a[i]==b[j-1] and a[i-1]==b[j]:d[i,j]=min(d[i,j],d[i-2,j-2]+1)
 return d[len(a)-1,len(b)-1]
def _fuzzy(s,t):
 maxd=2 if len(t)>=5 else 1;cands=set()
 for v in _deletes(t,maxd):cands|=s.dele.get(v,set())
 scored=sorted((_dl(t,c),-len(c),c) for c in cands)
 scored=[x for x in scored if x[0]<=maxd and (x[0]<=1 or len(x[2])>=5)]
 if not scored:return None
 if len(scored)>1 and scored[1][0]==scored[0][0] and {i[:2] for i in IDX.alias[scored[1][2]]}!={i[:2] for i in IDX.alias[scored[0][2]]}:return None
 return scored[0][2],scored[0][0]
_VOC=None
def _is_word(t):
 global _VOC
 if _VOC is None:
  try:
   import em_wallet_intent as em;_VOC={w.lower() for w in em._vocab()}
  except Exception:_VOC=set()
  _VOC|=COMMON
 return t in _VOC
IDX=None
ENT_WALLETS={}  # id -> [{chain,address,label,grade,source}]
ENT_META={}     # id -> {type,name,subtype}

def _alias_pref(alias, prefer_entity=False):
 items=IDX.alias.get(alias) or []
 if prefer_entity:
  for it in items:
   if it[0] in ('entity','person'):return it
 return items[0] if items else None
MAPPED=set();LAYER_IDS={'vitalik_buterin'};ADDR_SET=set();ADDR_PREFIX={}
def load():
 global IDX,ENT_WALLETS,ENT_META,PEOPLE,MAPPED,LAYER_IDS
 cat=json.loads((HERE/'navigation-catalog.json').read_text())['rows']
 try:
  ident=json.loads((HERE/'assistant-identity-catalog.json').read_text());cat=cat+[m for m in ident.get('markets',[]) if m.get('id') not in {r['id'] for r in cat}]
 except Exception:pass
 chains=json.loads((HERE/'chains-web3.json').read_text())['rows']
 from assistant_entity_intent import INSTITUTIONS
 inst=[(i,n,list(a)) for i,n,a in INSTITUTIONS]
 for i,n,a in EXTRA_INST:
  hit=[x for x in inst if x[0]==i]
  if hit:hit[0][2].extend(a)
  else:inst.append((i,n,a))
 people_extra=[]
 for path in (HERE/'entity-catalog.json',):
  if not path.exists():continue
  try:doc=json.loads(path.read_text())
  except Exception:continue
  for row in doc.get('entities') or []:
   eid=row.get('id') or '';name=row.get('name') or eid;typ=row.get('type') or 'institution'
   aliases=list(row.get('aliases') or [])
   wallets=[w for w in (row.get('wallets') or []) if isinstance(w,dict) and re.fullmatch(r'0x[0-9a-fA-F]{40}',str(w.get('address') or ''))]
   if wallets:ENT_WALLETS[eid]=[{**w,'address':w['address'].lower()} for w in wallets]
   ENT_META[eid]={'type':typ,'name':name,'subtype':row.get('subtype') or ''}
   if typ=='person':
    primary=(wallets[0]['address'].lower() if wallets else None)
    people_extra.append((eid,name,aliases,primary))
   else:
    inst.append((eid,name,aliases))
 for eid,name,aliases,primary in people_extra:
  if any(p[0]==eid for p in PEOPLE):
   PEOPLE[:]=[(eid,name,list(dict.fromkeys(list(p[2])+aliases)), primary or p[3]) if p[0]==eid else p for p in PEOPLE]
  else:PEOPLE.append((eid,name,aliases,primary))
 extra=HERE/'search-aliases.json'
 if extra.exists():
  for row in json.loads(extra.read_text()).get('entities',[]):
   if row.get('id') and not any(x[0]==row['id'] for x in inst):
    inst.append((row['id'],row.get('name') or row['id'],row.get('aliases',[])))
 global MAPPED,ADDR_SET,ADDR_PREFIX
 try:MAPPED={c['id'] for c in json.loads((HERE/'chains.json').read_text())['chains']}
 except Exception:pass
 LAYER_IDS.update(i for i,_,_ in INSTITUTIONS)
 LAYER_IDS.update(ENT_META)
 ADDR_SET=set()
 for path in (HERE/'curated_labels.json',Path('/srv/skew/ticking-mcp-gateway/curated_labels.json'),HERE/'entity-catalog.json'):
  if not path.exists():continue
  try:doc=json.loads(path.read_text())
  except Exception:continue
  if path.name=='entity-catalog.json':
   for row in doc.get('entities') or []:
    for w in row.get('wallets') or []:
     a=str(w.get('address') or '').lower()
     if re.fullmatch(r'0x[0-9a-f]{40}',a):ADDR_SET.add(a)
  elif isinstance(doc,dict):
   for k,v in doc.items():
    if re.fullmatch(r'0x[0-9a-fA-F]{40}',str(k)):ADDR_SET.add(str(k).lower())
    elif isinstance(v,dict) and re.fullmatch(r'0x[0-9a-fA-F]{40}',str(v.get('address') or '')):ADDR_SET.add(str(v['address']).lower())
 for _,_,_,addr in PEOPLE:
  if addr and re.fullmatch(r'0x[0-9a-f]{40}',addr):ADDR_SET.add(addr.lower())
 for ws in ENT_WALLETS.values():
  for w in ws:
   a=str(w.get('address') or '').lower()
   if re.fullmatch(r'0x[0-9a-f]{40}',a):ADDR_SET.add(a)
 for a in ('0xd8da6bf26964af9d7eed9e03e53415d37aa96045','0x28c6c06298d514db089934071355e5743bf21d60','0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503','0xbe0eb53f46cd790cd13851d5eff43d12404d33e8','0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be','0x71660c4005ba85c37ccec55d0c4493e66fe775d3','0x6cc5f688a315f3dc28a7781717a9a798a59fda7b'):
  ADDR_SET.add(a)
 ADDR_PREFIX={}
 for a in ADDR_SET:
  for n in range(8,41):
   ADDR_PREFIX.setdefault(a[:2+n],set()).add(a)
 IDX=Index(cat,chains,inst);IDX.markets={r['id']:r for r in cat}
 return IDX
# ---------- 3. phrase routes
MARKET_P=re.compile(r'(?i)^(?:(?:show|open|list|view)\s+)?(?:all\s+)?(?:markets?|마켓|시장|market\s+list|all\s+(?:tokens?|coins?|markets?)|전체\s*(?:코인|토큰|마켓)|top\s+(?:gainers|losers|movers)|(?:today\'?s\s+)?(?:gainers|losers|movers)|trending(?:\s+(?:tokens|coins))?|급등|급락|상승률|하락률|오늘\s*(?:뭐|머)\s*(?:올랐|떨어졌|떡상|떡락)\S*|떡상\s*코인|인기\s*(?:코인|토큰))$')
WHALE_P=re.compile(r'(?i)\bwhales?\b|whale\s*(?:tracker|alerts?)|고래|큰손|biggest\s+wallets|largest\s+wallets|large\s+(?:trades|transfers)|big\s+(?:money|wallets|trades)|대형\s*거래|대규모\s*(?:거래|이체)')
WHALE_Q=re.compile(r'(?i)who\s+(?:bought|sold|is\s+buying|is\s+selling)|누가\s*(?:샀|팔았|사|팔)')
def reply_route(state,title,route,message,**more):
 _ctx(more.get('entity'))
 return {'state':state,'title':title,'route':route,'message':message,'resolver':'economic-machine-search',**more,'modelCalls':0,'searchGuard':VERSION}
def _ctx(ent):
 """Share the resolved subject with the per-user context layer (investigation.context), so the next turn in any capability continues it."""
 try:
  import investigation_contract as ic
  if not ent:return
  k=ent.get('type');i=ent.get('id')
  subj={'wallet':{'kind':'wallet','address':i},'token':{'kind':'market','asset':i},'institution':{'kind':'institution','entity':i},'exchange':{'kind':'institution','entity':i},'person':{'kind':'wallet','address':(ENT_WALLETS.get(i) or [{}])[0].get('address') if ENT_WALLETS.get(i) else None}}.get(k)
  if k=='person' and subj and not subj.get('address'):subj=None
  if subj:ic.CURRENT.set(ic.normalize({'schema':ic.SCHEMA,'subject':subj,'selection':{}}))
 except Exception:pass
def chain_reply(cid,name):
 return reply_route('ready',name+' chain',('#/chains?chain='+quote(cid)),name+' recognized as a chain. Opens the cross-chain comparison focused on it.',entity={'type':'chain','id':cid})
HOME={'PEPE':'ethereum:0x6982508145454ce325ddbe47a25d4ec3d2311933'}
NET_RANK=['solana','exchange','ethereum','arbitrum','base','robinhood','polygon','optimism','unichain','linea','arc']
CHART_P=re.compile(r'(?i)^(?:(?:open|show|show me|view)\s+)?(?:(?:chart|차트)\s+(?:of\s+)?)?(.+?)\s*(?:(?:price\s+)?charts?|차트|가격\s*차트|가격|price|(?:pro\s+)?terminal|프로(?:\s*터미널)?|캔들|candles?|시세)?$')
def default_market(sym_items):
 toks=[i for i in sym_items if i[0]=='token']
 if not toks:return None,[]
 sym=toks[0][2];rows=[IDX.markets.get(i[1]) or {'id':i[1],'network':''} for i in toks]
 rows=list({r['id']:r for r in rows}.values())
 if len({(IDX.markets.get(r['id']) or {}).get('symbol',sym) for r in rows})>1:return None,[]
 if sym in HOME and any(r['id']==HOME[sym] for r in rows):best=HOME[sym]
 else:
  plain=[r for r in rows if r['id']==sym or ':' not in r['id']]
  best=(plain or sorted(rows,key=lambda r:NET_RANK.index(r.get('network')) if r.get('network') in NET_RANK else 99))[0]['id']
 return best,[r['id'] for r in rows if r['id']!=best]
def chart_route(q):
 if not re.search(r'(?i)charts?|차트|가격|price|terminal|프로|캔들|candles?|시세',q):return None
 m=CHART_P.fullmatch(q.strip())
 if not m:return None
 name=m.group(1).strip().casefold()
 items=IDX.alias.get(name)
 if not items:
  f=_fuzzy(IDX,name) if re.fullmatch(r'[a-z0-9 .-]{3,}',name) else None
  items=IDX.alias.get(f[0]) if f else None
 if not items:return None
 best,alts=default_market(items)
 if not best:return None
 r=IDX.markets.get(best,{});sym=r.get('symbol') or best
 terminal=bool(re.search(r'(?i)terminal|프로',q))
 _ctx({'type':'token','id':best})
 return {'state':'ready','title':'Open '+sym,'route':('#/console/workspace/' if terminal else '#/token/')+quote(best,safe=''),'message':sym+' chart on its default market'+(' ('+str(r.get('network'))+')' if r.get('network') else '')+'.','alternatives':[{'title':sym+' · '+str((IDX.markets.get(a) or {}).get('network') or a.split(':')[0]),'route':'#/console/workspace/'+quote(a,safe='')} for a in alts[:6]],'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION,'entity':{'type':'token','id':best}}
def token_in(q):
 for a,b,alias,d in IDX.spans(q):
  items=IDX.alias[alias]
  if any(i[0]=='token' for i in items) and not (len(alias)<=3 and alias in COMMON):
   best,alts=default_market(items)
   if best:return best
 return None
def intent_route(q):
 """Research / chart-tool intents: open the subject on its default market and hand off to the capability with its request payload."""
 if FULL.search(q):return None
 res=RESEARCH_P.search(q);tool=CHART_TOOL_P.search(q)
 if not (res or tool):return None
 tq=re.sub(r'(?i)\b(?:why|did|is|has|are|pump|dump|research|deep|dive|news|on|about|for)\b|리서치|뉴스|심층|왜|올랐어?|떨어졌어?',' ',q) if res else CHART_TOOL_P.sub(' ',q)
 if len(q.split())>8:return None
 best=token_in(tq)
 if not best:
  if not re.sub(r'\s+','',tq) or len(q.split())<=1:return None
  cap='research-assist' if res else 'chart-assist'
  return {'state':'message','message':('Which market should I research? Name a token, or open one and ask again.' if res else 'Open a chart, then ask for this tool in the assistant beside it.'),'choices':[{'title':'Open SOL Pro','route':'#/console/workspace/SOL'},{'title':'Choose a market','route':'#/markets'}],'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION,'handoff':{'capability':cap,'next':{'capability':cap,'request':{'query':q}},'schema':'ticking.handoff.v1','context':_cur_ctx()}}
 r=IDX.markets.get(best,{});sym=r.get('symbol') or best
 _ctx({'type':'token','id':best})
 if res:
  nxt={'capability':'research-assist','request':{'query':q,'research':{'selection':{'asset':best,'symbol':sym}}}}
  msg='Research on '+sym+': opening its chart; the research assistant reads sources for this market.'
 else:
  nxt={'capability':'chart-assist','request':{'query':q,'chart':{'symbol':sym,'asset':best,'interval':'15m'}}}
  msg='Opening the '+sym+' chart; the chart assistant applies this tool there.'
 return {'state':'ready','title':'Open '+sym,'route':'#/console/workspace/'+quote(best,safe=''),'message':msg,'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION,'entity':{'type':'token','id':best},'handoff':{'capability':nxt['capability'],'next':nxt,'schema':'ticking.handoff.v1','context':_cur_ctx()}}
def _cur_ctx():
 try:
  import investigation_contract as ic;return ic.CURRENT.get()
 except Exception:return None
def pre_route(q):
 ir=intent_route(q)
 if ir:return ir
 """Deterministic routes that must win before the generic pipeline (which would read e.g. 'bnb chain' as the BNB token)."""
 low=q.casefold()
 if MARKET_P.fullmatch(q.strip()):return reply_route('ready','Markets','#/markets','Opening Markets.')
 addrs=FULL.findall(q)
 if (WHALE_P.search(q) or WHALE_Q.search(q)) and not addrs and not re.search(r'(?i)chart|차트|holders?|보유자',q):
  sp=[x for x in IDX.spans(q) if IDX.alias[x[2]][0][0]=='token' and x[2] not in ('whale','whales')]
  ch=[x for x in IDX.spans(q) if IDX.alias[x[2]][0][0]=='chain']
  route='#/tracker'+('?chain='+quote(IDX.alias[ch[0][2]][0][1]) if ch else '')
  if sp:
   t=IDX.alias[sp[0][2]][0]
   return {'state':'choices','message':'Whale activity for '+t[2]+'.','choices':[{'title':'Whale Tracker','route':route},{'title':t[2]+' chart with large-wallet evidence','route':'#/console/workspace/'+quote(t[1],safe='')}],'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION}
  return reply_route('ready','Whale Tracker',route,'Opening the live Whale Tracker.')
 if len(addrs)==1 and GRAPH_P.search(q) and not TX.search(q):
  a=addrs[0].lower();cid=None
  for x in IDX.spans(q):
   c=[i for i in IDX.alias[x[2]] if i[0]=='chain']
   if c:cid=c[0][1];break
  route='#/investigate/'+a+(('?chain='+quote(cid)) if cid else '')
  _ctx({'type':'wallet','id':a})
  return {'state':'ready','title':'Wallet graph','route':route,'message':'Opening the counterparty graph for this wallet'+(' on '+cid if cid else ' across every indexed chain (no chain needed; narrow it inside the graph)')+'.','resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION,'entity':{'type':'wallet','id':a,'chain':cid},'handoff':{'capability':'graph-loop','next':{'capability':'graph-loop','route':route},'schema':'ticking.handoff.v1','context':_cur_ctx()}}
 if addrs or TX.search(q):return None
 sa=sol_addr(q)
 if sa and len(q.replace(sa,'').split())<=3:return reply_route('ready','Investigate wallet','#/investigate/'+sa,'Opening Solana wallet Flow (chart + holdings pixel; no topology map).',entity={'type':'wallet','id':sa,'chain':'solana'})
 if re.search(r'(?i)wallet\s*map|지갑\s*(?:지도|맵)|매핑|\bmap\b',q):
  ch=[IDX.alias[x[2]] for x in IDX.spans(q) if any(i[0]=='chain' for i in IDX.alias[x[2]])]
  if len(ch)!=1:return None
  c=[i for i in ch[0] if i[0]=='chain'][0]
  if c[1] in MAPPED:return reply_route('ready',c[2]+' Whale Tracker','#/tracker?chain='+quote(c[1]),'Opening '+c[2]+' Whale Tracker (Flow charts + alerts — not a topology map).',entity={'type':'chain','id':c[1]})
  return {**chain_reply(c[1],c[2]),'message':c[2]+' has no Economic Machine Flow coverage yet. Opening its chain overview instead.'}
 ents=[IDX.alias[x[2]][0] for x in IDX.spans(q) if IDX.alias[x[2]][0][0] in ('entity','person')]
 if ents and ents[0][0]=='person' and ents[0][3]:
  p=ents[0];return reply_route('ready',p[2],'#/investigate/'+p[3],p[2]+' recognized (publicly attributed ENS wallet). Opening the wallet investigation.',entity={'type':'wallet','id':p[3]},person=p[1])
 if ents and ents[0][1] not in LAYER_IDS:return fallback(q)
 sp=IDX.spans(q)
 chains=[x for x in sp if any(i[0]=='chain' for i in IDX.alias[x[2]])]
 rest=low
 for a,b,_,_ in sp:rest=rest[:a]+' '*(b-a)+rest[b:]
 rest=re.sub(CHAIN_WORD,' ',rest,flags=re.I).strip()
 cids={[i for i in IDX.alias[x[2]] if i[0]=='chain'][0][1] for x in chains}
 if len(cids)==1 and not rest and len(sp)==len(chains):
  if len(chains)>1:chains=[max(chains,key=lambda x:len(x[2]))]
  c=[i for i in IDX.alias[chains[0][2]] if i[0]=='chain'][0];toks=[i for i in IDX.alias[chains[0][2]] if i[0]=='token']
  explicit=re.search(CHAIN_WORD,low,re.I)
  if explicit or not toks:return chain_reply(c[1],c[2])
  if q.strip().isupper() and len(q.strip())<=6:return None
  t=toks[0]
  return {'state':'choices','message':chains[0][2]+' is both a chain and a token.','choices':[{'title':c[2]+' chain overview','route':'#/chains?chain='+quote(c[1])},{'title':t[2]+' chart','route':'#/console/workspace/'+quote(t[1],safe='')}],'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION,'entity':{'type':'chain','id':c[1]}}
 return None
def rewrite(q):
 """Replace fuzzy (typo) alias spans by their canonical alias so downstream exact matchers see them."""
 out=q
 for a,b,alias,dist in reversed(IDX.spans(q)):
  it=IDX.alias[alias][0]
  items=IDX.alias[alias]
  if any(i[0]=='chain' for i in items):
   if dist==0:continue
   canon=alias
  elif it[0]=='person':canon='vitalik' if it[1]=='vitalik_buterin' else it[2]
  elif dist==0 and not (re.search('[가-힣]',alias) and it[0] in ('token','entity')) and alias!='코베':continue
  else:canon=it[2]
  out=out[:a]+canon+out[b:]
 return out
def prefix_lookup(prefix):
 """Complete truncated 0x (>=8 hex) from labeled/mapped ADDR_SET first; EM search is a live fallback only."""
 pref=prefix.lower()
 if not pref.startswith('0x'):pref='0x'+pref
 hexpart=pref[2:]
 if len(hexpart)<8 or not re.fullmatch(r'[0-9a-f]+',hexpart):return None
 local=ADDR_PREFIX.get(pref) or {a for a in ADDR_SET if a.startswith(pref)}
 if local:return sorted(local)
 if len(hexpart)<10:return None
 try:
  with urllib.request.urlopen('http://127.0.0.1:8896/api/public/em/search?'+__import__('urllib.parse').parse.urlencode({'q':pref,'limit':8}),timeout=1.5) as r:d=json.loads(r.read(400000))
 except Exception:return None
 full={x.get('id','').split(':')[-1].lower() for x in d.get('results',[]) if x.get('type')=='wallet'}
 full={a for a in full if re.fullmatch(r'0x[0-9a-f]{40}',a) and a.startswith(pref)}
 return sorted(full) if full else None
def fallback(q):
 """Deterministic answer instead of the paid model when an alias is recognized."""
 sp=IDX.spans(q)
 m=PART.search(q)
 if m and not FULL.search(q):
  hits=prefix_lookup('0x'+m[1])
  if hits and len(hits)==1:
   if GRAPH_P.search(q):
    a=hits[0];route='#/investigate/'+a
    _ctx({'type':'wallet','id':a})
    return {'state':'ready','title':'Wallet graph','route':route,'message':'Opening the counterparty graph for this wallet across every indexed chain.','resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION,'entity':{'type':'wallet','id':a},'handoff':{'capability':'graph-loop','next':{'capability':'graph-loop','route':route},'schema':'ticking.handoff.v1','context':_cur_ctx()}}
   return reply_route('ready','Investigate wallet','#/investigate/'+hits[0],'Opening wallet Flow for a labeled/mapped address (chart + mapped overlays).',entity={'type':'wallet','id':hits[0]})
  if hits:
   return {'state':'choices','message':'That address is incomplete; '+str(len(hits))+' labeled/mapped wallets start with it.','choices':[{'title':h[:10]+'…'+h[-4:],'route':'#/investigate/'+h} for h in hits[:6]],'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION}
  return {'state':'clarify','message':'That EVM address looks incomplete ('+str(len(m[1]))+' of 40 hex characters). Paste the full 0x address.','resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION}
 tm=TX_PART.search(q)
 if tm and not TX.search(q):
  return {'state':'clarify','message':'That transaction hash looks incomplete ('+str(len(tm[1]))+' of 64 hex characters). Paste the full 0x tx hash.','resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION}
 if TX.search(q):
  h=TX.search(q)[0]
  return {'state':'choices','message':'Transaction hash recognized. Open it on an explorer for its chain.','choices':[{'title':n,'url':u+h} for n,u in [('Etherscan','https://etherscan.io/tx/'),('Arbiscan','https://arbiscan.io/tx/'),('Basescan','https://basescan.org/tx/')]],'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION}
 a=FULL.search(q)
 if a:return reply_route('ready','Investigate wallet','#/investigate/'+a[0].lower(),'Opening wallet Flow — this wallet plus mapped related wallets on one chart (neon pixel for holdings).',entity={'type':'wallet','id':a[0].lower()})
 cr=chart_route(q)
 if cr:return cr
 if FLOW_P.search(q):
  addr=FULL.search(q);sym=ASSET_FLOW_SYM.search(q)
  if addr:
   a=addr[0].lower()
   return {'state':'ready','title':'Wallet Flow','route':'#/investigate/'+a+'?kind=wallet','message':'Opening wallet Flow — ask Muse for inflow / outflow / holdings (not a price chart). Chain is taken from the wallet page, not forced to Arbitrum.','resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION,'entity':{'type':'wallet','id':a},'handoff':{'capability':'muse-agent','next':{'capability':'muse-agent','request':{'query':q,'flow':True}},'schema':'ticking.handoff.v1'}}
  if sym:
   m={'ARB':'arbitrum:0x912ce59144191c1204e64559fe8253a0e49e6548','WETH':'arbitrum:0x82af49447d8a07e3bd95bd0d56f35241523fbab1','PENDLE':'arbitrum:0x0c880f6761f1af8d9aa9c466984b80dab9a8c9e8','GMX':'arbitrum:0xfc5a1a6eb076a2c7ad06ed22c90d7e710e35ad0a'}
   s=sym[0].upper();route='#/investigate/'+m[s]
   return {'state':'ready','title':s+' Asset Flow','route':route,'message':'Opening '+s+' Markets asset Flow — Muse reads asset-flow and mounts In/Out/holdings (not price candles).','resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION,'entity':{'type':'token','id':m[s]},'handoff':{'capability':'muse-agent','next':{'capability':'muse-agent','request':{'query':q,'flow':True}},'schema':'ticking.handoff.v1'}}
  return reply_route('ready','Whale Tracker','#/tracker','Opening Whale Tracker — ask Muse a Flow question (inflow/outflow/who accumulated) to mount charts in the answer.')
 if WHALE_P.search(q) or WHALE_Q.search(q):return pre_route('whales '+q) or reply_route('ready','Whale Tracker','#/tracker','Opening Whale Tracker — large-flow feed with wallet Flow charts and alerts (not a bubble map).')
 prefer_ent=bool(re.search(r'(?i)\b(?:dao|treasury|foundation|fund|ventures|capital|labs)\b|재단|트레저리|다오', q))
 for a_,b_,alias,_ in sp:
  it=_alias_pref(alias, prefer_ent) or IDX.alias[alias][0]
  if it[0]=='person':
   if it[3]:return reply_route('ready',it[2],'#/investigate/'+it[3],it[2]+' recognized. Opening the publicly attributed wallet (ENS).',entity={'type':'person','id':it[1]})
   return {'state':'message','message':it[2]+' is a named individual. No verified wallet attribution is in our label set; paste a public address.','entity':{'type':'person','id':it[1]},'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION}
  if it[0]=='entity':
   eid,ename=it[1],it[2]
   _ctx({'type':'institution','id':eid} if re.fullmatch(r'[a-z0-9_-]{1,80}',eid) else None)
   wallets=ENT_WALLETS.get(eid) or []
   meta=ENT_META.get(eid) or {}
   etype=meta.get('type') or 'institution'
   if wallets:
    primary=wallets[0];addr=primary['address']
    choices=[{'title':(w.get('label') or ename)+' · '+w.get('chain','?'),'route':'#/investigate/'+w['address']} for w in wallets[:8]]
    if len(wallets)==1:
     return reply_route('ready',ename,'#/investigate/'+addr,ename+' recognized. Opening the verified labeled wallet (holdings path). Source: '+str(primary.get('source') or 'label')+'.',entity={'type':etype,'id':eid,'address':addr,'chain':primary.get('chain')})
    return {'state':'choices','title':ename,'message':ename+': '+str(len(wallets))+' verified labeled wallets. Opening holdings on a selected wallet — aggregate portfolio across all wallets is not claimed.','choices':choices,'route':'#/investigate/'+addr,'entity':{'type':etype,'id':eid,'address':addr,'chain':primary.get('chain')},'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION}
   return {'state':'message','message':ename+' recognized as a '+etype+' in the curated catalog, but no verified labeled wallet is attached yet on this server. Paste a public address, or wait for PoR/official label coverage. No holdings or ownership were inferred.','entity':{'type':etype,'id':eid},'choices':[{'title':'Paste a wallet address','route':'#/explore'},{'title':'Whale Tracker','route':'#/tracker'}],'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION}
  if it[0]=='chain':return chain_reply(it[1],it[2])
  if it[0]=='token' and len(q.split())<=6:
   if alias in COMMON or len(alias)<=2:continue
   return reply_route('ready','Open '+it[2],'#/console/workspace/'+quote(it[1],safe=''),it[2]+' recognized.',entity={'type':'token','id':it[1]})
 if re.search(r'(?i)\bmarkets?\b|마켓|시장|gainers|losers|trending|급등|급락|올랐|떨어졌',q):return reply_route('ready','Markets','#/markets','Opening Markets.')
 return None
# ---------- install
import contextvars
CUR=contextvars.ContextVar('search_guard_q',default=None)
REQ=contextvars.ContextVar('search_guard_req',default=None)
class Hit(Exception):
 def __init__(s,v):s.v=v
G={}
def install(g):
 G.update(g=g)
 load()
 orig=g['route_unmetered'];orig_reply=g['reply'];orig_reserve=g['reserve']
 omp=g.get('model_plan')
 if omp and not getattr(omp,'_sg_cached',False):
  PC={}
  def model_plan(q,chart=None):
   """Muse plan cache: temperature-0 plans keyed by case/space-folded query + chart, 1h, 5000 entries (route cache was 5 min / 128 exact keys)."""
   k=(re.sub(r'\s+',' ',q.casefold()).strip(),json.dumps(chart,sort_keys=True) if chart else None)
   c=PC.get(k)
   if c and time.time()-c[0]<3600:return json.loads(c[1])
   p=omp(q,chart)
   try:
    PC[k]=(time.time(),json.dumps(p))
    if len(PC)>5000:PC.pop(next(iter(PC)))
   except Exception:pass
   return p
  model_plan._sg_cached=True;g['model_plan']=model_plan
 def reply(value,status=200):
  try:
   cur=REQ.get()
   if isinstance(value,dict) and 'handoff' not in value and cur is not None:
    t0=cur[2];value['handoff']=handoff(cur[0],cur[1],value);value['handoff']['routingMs']=round((time.perf_counter()-t0)*1000,2)
  except Exception:pass
  return orig_reply(value,status)
 g['reply']=reply
 def reserve(key):
  q=CUR.get()
  if q:
   fb=fallback(q)
   if fb:raise Hit(fb)
  return orig_reserve(key)
 g['reserve']=reserve
 async def route_unmetered(request):
  try:
   raw=await request.body()
   body=json.loads(raw) if raw and len(raw)<=2048 else None
  except Exception:body=None
  if not isinstance(body,dict) or not isinstance(body.get('query'),str):return await _safe(orig,request,reply,None)
  _t0=time.perf_counter()
  q0=body['query'];q=repair(normalize(q0))
  REQ.set((q,body,_t0))
  if not q:return reply({'state':'clarify','message':'Type a token, wallet address, chain or question.','modelCalls':0,'searchGuard':VERSION})
  plain=not ({'chart','analysis','research','walletResearch','context','previousContext'}&set(body))
  for name in re.findall(r'(?i)(?<![\w.-])((?:[a-z0-9-]{1,63}\.)+eth)(?![\w.-])',q)[:1]:
   lname=name.lower();known=[p for p in PEOPLE if p[3] and any(x.endswith('.eth') and (x==lname or _dl(x,lname)<=(2 if len(x)>=10 else 1)) for x in p[2])]
   if known:
    q=q.replace(name,'vitalik.eth' if known[0][0]=='vitalik_buterin' else name);continue
   try:
    import ens_resolve
    import asyncio
    a=await asyncio.to_thread(ens_resolve.resolve,lname)
   except Exception:a=None
   if a:q=q.replace(name,a)
   elif plain and re.fullmatch(r'(?i)(?:[a-z0-9-]{1,63}\.)+eth',q.strip()):return reply({'state':'clarify','message':name+' has no ENS address record (or ENS is unreachable right now). Paste the 0x address.','modelCalls':0,'searchGuard':VERSION})
  if plain and len(q)<=400:
   try:
    from query_safety import public_query
    public_query(q)
    if TX.search(q):return reply(fallback(q))
    pr=pre_route(q)
    if pr:return reply(pr)
   except ValueError:pass
   except Exception:pass
  m=re.fullmatch(r'(?i)(?:(?:wallet|지갑|address|주소)\s+)?([1-9A-HJ-NP-Za-km-z]{32,44})(?:\s+(?:wallet|지갑|address|주소)(?:\s*보여줘)?)?',q)
  if plain and m:q=m[1]
  pm=PART.search(q)
  if plain and pm and not FULL.search(q):
   import asyncio
   hits=await asyncio.to_thread(prefix_lookup,'0x'+pm[1])
   if hits and len(hits)==1:
    q=q.replace(pm[0],hits[0])
    # Prefer deterministic wallet/graph routes over native multi-chain clarify.
    try:
     pr=pre_route(q)
     if pr:return reply(pr)
     fb=fallback(q)
     if fb:return reply(fb)
    except Exception:pass
   elif hits:
    return reply({'state':'choices','message':'That address is incomplete; '+str(len(hits))+' labeled/mapped wallets start with it.','choices':[{'title':h[:10]+'…'+h[-4:],'route':'#/investigate/'+h} for h in hits[:6]],'resolver':'economic-machine-search','modelCalls':0,'searchGuard':VERSION})
  if plain:
   try:q=rewrite(q)
   except Exception:pass
   try:
    sp=IDX.spans(q) if IDX else []
    instish=bool(re.search(r'(?i)\b(?:dao|treasury|foundation|fund|ventures|capital|labs)\b|재단|트레저리|다오|거래소', q))
    def _kinds(alias):return {it[0] for it in (IDX.alias.get(alias) or [])}
    ent_sp=[x for x in sp if _kinds(x[2]) & {'entity','person'}]
    if ent_sp and len(q)<=200 and not MARKET_P.search(q):
     # DAO/treasury wording, or the dominant span is entity/person (not a pure chart query)
     if instish or (_kinds(sp[0][2]) & {'entity','person'} and not chart_route(q)):
      fb=fallback(q)
      if fb and (fb.get('entity') or {}).get('type') in ('institution','exchange','person'):
       return reply(fb)
   except Exception:pass
  nb=json.dumps({**body,'query':q}).encode()
  sent=[False]
  async def receive():
   if not sent[0]:
    sent[0]=True;return {'type':'http.request','body':nb,'more_body':False}
   return await request.receive()
  new=Request(request.scope,receive)
  tok=CUR.set(q if plain else None)
  try:return await _safe(orig,new,reply,q if plain else None)
  finally:CUR.reset(tok)
 g['route_unmetered']=route_unmetered
GRAPH_P=re.compile(r'(?i)\bgraph\b|그래프|연결|counterpart|connections?|flow\s*map|자금\s*맵|trace')
FLOW_P=re.compile(r'(?i)\b(?:wallet\s*flow|asset\s*flow|fund\s*flow|net\s*(?:in|out)flow|inflows?|outflows?|accumulat\w*|whale\s*flow|money\s+flow|flow\s*chart|(?:token|asset|wallet)?\s*flow)\b|플로우|유입|유출|축적|고래\s*흐름|자금\s*흐름|(?:누가|who).*(?:쌓|accumul|hold)|(?:show|open).*(?:flow|유입)')
ASSET_FLOW_SYM=re.compile(r'(?i)\b(?:ARB|WETH|PENDLE|GMX)\b')
RESEARCH_P=re.compile(r'(?i)\bresearch\b|리서치|why\s+(?:did|is|has|are)\s+.{1,30}(?:pump|dump|up|down|moon|crash|rally|drop)|왜.{0,20}(?:올랐|떨어졌|펌핑|폭락|빠졌|오르|떨어지)|news\s+(?:on|about|for)|뉴스|deep\s*dive|심층')
CHART_TOOL_P=re.compile(r'(?i)\b(?:rsi|macd|bollinger(?:\s+bands?)?|ema|sma|vwap|fib(?:onacci)?|trend\s*line|trendline|draw)\b|추세선|지지선|저항선|보조지표|그려(?:줘)?')
def capability_of(q,body,res):
 st=str(res.get('state',''));rv=str(res.get('resolver',''))
 if res.get('modelCalls'):return 'muse-agent'
 if q and FLOW_P.search(q):return 'muse-agent'
 if 'analysis' in body or st=='analysis':return 'graph-assist'
 if 'research' in body or st=='asset_research':return 'research-assist'
 if st in ('chart_actions','chart_workflow') or 'chart' in body:return 'chart-assist'
 if q and GRAPH_P.search(q) and ('wallet' in st or 'graph' in st or 'wallet' in rv or FULL.search(repair(normalize(q)))):return 'graph-loop'
 if 'wallet-research' in rv:return 'research-assist'
 if 'economic-machine' in rv or 'wallet' in st:return 'economic-machine'
 return 'search'
def handoff(q,body,res):
 """Deterministic next-capability hint with the shared context payload (no paid call is made here)."""
 try:
  import investigation_contract as ic
  ctx=(res.get('investigation') or {}).get('context') or ic.CURRENT.get()
 except Exception:ctx=None
 cap=capability_of(q,body,res);nxt=None
 subj=(ctx or {}).get('subject') or {}
 if q and FLOW_P.search(q):
  asset=subj.get('asset') or ''
  # Prefer Markets asset Flow when subject is a token/contract (never treat mint as wallet)
  if isinstance(asset,str) and ':0x' in asset.lower():
   route='#/investigate/'+asset
  elif ASSET_FLOW_SYM.search(q):
   m={'ARB':'arbitrum:0x912ce59144191c1204e64559fe8253a0e49e6548','WETH':'arbitrum:0x82af49447d8a07e3bd95bd0d56f35241523fbab1','PENDLE':'arbitrum:0x0c880f6761f1af8d9aa9c466984b80dab9a8c9e8','GMX':'arbitrum:0xfc5a1a6eb076a2c7ad06ed22c90d7e710e35ad0a'}
   route='#/investigate/'+m[ASSET_FLOW_SYM.search(q)[0].upper()]
  else:
   addr=subj.get('address') or (FULL.search(repair(normalize(q)))[0].lower() if FULL.search(repair(normalize(q))) else None)
   chain=subj.get('chain') or None
   # Only treat as wallet when subject kind is wallet or bare 0x (not a token mint mistaken for wallet)
   if addr and subj.get('kind')=='wallet':
    route='#/investigate/'+addr+('?kind=wallet'+('&chain='+chain if chain else ''))
   elif addr and not asset:
    route='#/investigate/'+addr+('?kind=wallet'+('&chain='+chain if chain else ''))
   else:
    route='#/tracker'
  nxt={'capability':'muse-agent','route':route,'note':'Muse interprets the ask, picks wallet-flow or asset-flow (and the chain DB that serves it), then mounts Flow charts.','request':{'query':q,'flow':True}}
 elif q and RESEARCH_P.search(q) and subj.get('asset') and 'research' not in body:
  nxt={'capability':'research-assist','request':{'query':q,'research':{'selection':{'asset':subj['asset'],'symbol':subj.get('symbol') or subj['asset']}}}}
 elif q and GRAPH_P.search(q) and (subj.get('address') or FULL.search(repair(normalize(q)))):
  subj=dict(subj);subj.setdefault('address',FULL.search(repair(normalize(q)))[0].lower() if FULL.search(repair(normalize(q))) else None)
  nxt={'capability':'graph-loop','route':'#/investigate/?'+__import__('urllib.parse').parse.urlencode({'focus':subj['address'],**({'chain':subj['chain']} if subj.get('chain') else {})})}
 elif q and CHART_TOOL_P.search(q) and 'chart' not in body:
  nxt={'capability':'chart-assist','note':'Open a chart, then send this query with chart context.','request':{'query':q,'chart':{'symbol':subj.get('symbol') or 'SOL','asset':subj.get('asset') or 'SOL','interval':'15m'}}}
 return {'capability':cap,'next':nxt,'context':ctx,'schema':'ticking.handoff.v1'}
async def _safe(orig,request,reply,q):
 try:
  r=await orig(request)
  if q and r.status_code==200:
   try:
    v=json.loads(r.body)
    if v.get('state')=='clarify':
     cr=chart_route(q)
     if cr:return reply(cr)
     # Only upgrade clarify for the failure clusters we own (addr/tx/graph/market/whale).
     # Do not run generic token fallback here — it turns "show me SKR" into ME.
     if PART.search(q) or TX.search(q) or TX_PART.search(q) or GRAPH_P.search(q) or MARKET_P.search(q) or WHALE_P.search(q) or WHALE_Q.search(q):
      fb=fallback(q)
      if fb and fb.get('state')!='clarify':return reply(fb)
   except Exception:pass
  if r.status_code>=500 and q:
   fb=fallback(q)
   if fb:return reply(fb)
  return r
 except Hit as h:return reply(h.v)
 except Exception as e:
  fb=None
  try:
   if q and G and G['g'].get('native_resolve'):
    c=G['g']['native_resolve'](q,True)
    if c and c.get('state')=='chart_actions':fb={'state':'message','message':'Open a chart first, then ask for this tool in the assistant beside it.','choices':[{'title':'Open SOL Pro','route':'#/console/workspace/SOL'},{'title':'Choose a market','route':'#/markets'}],'modelCalls':0,'searchGuard':VERSION,'capability':'chart-assist'}
  except Exception:pass
  try:fb=fb or (fallback(q) if q else None)
  except Exception:pass
  return reply(fb or {'state':'clarify','message':'I could not resolve that exactly. Name a token, a full wallet address, or a chain.','modelCalls':0,'searchGuard':VERSION})
