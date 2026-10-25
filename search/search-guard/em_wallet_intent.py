"""Economic Machine wallet intents for the staging Ticking resolver.

Deterministic routing (no model call): one 0x wallet -> wallet card (or its money trace), "who borrowed USDG against NVDA"
-> ranked Morpho Blue positions, "NVDA holders" -> holders card. Data comes from the em-ticking-bridge
wallet tools (/api/public/em/tools/*), which read em-galaxy. Markets are first-class '<network>:<0x address>' rows. The sentence is built from those numbers only.
"""
import json
import investigation_contract as investigation
from economic_machine import chain_catalog
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from request_identity import forwarded_request

TOOLS = os.environ.get('EM_TOOLS_BASE', 'http://127.0.0.1:8896/api/public/em/tools/')   # em-ticking-bridge (loopback)
CONSOLE = os.environ.get('EM_CONSOLE_BASE', 'http://127.0.0.1:19428')                     # Ticking console API (candles)
ADDR = re.compile(r'(?<![A-Za-z0-9])0x[a-fA-F0-9]{40}(?![A-Za-z0-9])')
class MarketIdentity:
    fullmatch = staticmethod(chain_catalog.market)
MARKET = MarketIdentity()
STABLE = {'USDG', 'USDC', 'USDT', 'USDT0', 'DAI', 'USDE', 'USD', 'USDCE', 'FRAX'}
STOP = {'WHO', 'THE', 'TOP', 'AND', 'ON', 'FOR', 'ME', 'SHOW', 'LIST', 'WALLET', 'WALLETS', 'HOLDERS', 'HOLDER', 'AGAINST', 'WITH',
        'BORROWED', 'BORROW', 'LARGEST', 'BIGGEST', 'MOST', 'OF', 'IN', 'THIS', 'THAT', 'WHAT', 'WHICH', 'IS', 'ARE', 'HOLD', 'HOLDS',
        'DEBT', 'LOANS', 'COLLATERAL', 'STOCK', 'TOKENS', 'TOKEN', 'MONEY', 'TRACE', 'WHERE', 'DID', 'GO', 'FROM', 'TO', 'IT', 'ITS',
        'MAP', 'WHALES', 'WHALE', 'OWNS', 'OWNERS', 'ROBINHOOD', 'ARBITRUM', 'CHAIN', 'ONE', 'MORPHO', 'AAVE', 'AI', 'USING', 'POSTED'}
class ChainNames:
    get = staticmethod(lambda key, default=None: chain_catalog.names().get(key, default))
CHAIN = ChainNames()
FLAG = {'unverified_principal': 'a principal not confirmed by a receipt token', 'morpho_interest_runaway': 'a Morpho market with runaway bad debt',
        'vault_pass_through': 'vault assets that belong to depositors', 'debt_exceeds_assets': 'debt above its priced assets',
        'collateral_unpriced': 'collateral without a trusted price', 'lp_unpriced': 'LP without a trusted price',
        'supplied_unpriced': 'a supplied asset without a trusted price', 'lp_owner_inferred': 'LP ownership inferred from the sender',
        'unverified': 'balances far above its observed inflow'}


def short(a):
    return a[:6] + '…' + a[-4:] if a else ''


def usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return '—'
    a, s = abs(v), '-' if v < 0 else ''
    if a >= 1e9:
        return '%s$%.2fB' % (s, a / 1e9)
    if a >= 1e6:
        return '%s$%.2fM' % (s, a / 1e6)
    if a >= 1e3:
        return '%s$%.1fK' % (s, a / 1e3)
    return '%s$%.0f' % (s, a)


def question_chain(q):return chain_catalog.query_chain(q)

def get_on(tool,chain,**q):
    # Failure on a named chain must never retry against an unscoped default.
    card=get(tool,**dict(q,**({'chain':chain} if chain else {})))
    if chain and card.get('chain',card.get('network')) not in (None,chain):raise LookupError('The evidence does not match the requested chain. No network was substituted.')
    return card


def get(tool, **q):
    deferred=tool in {'wallet','trace'} and q.get('chain')
    base=TOOLS.split('/tools/')[0]
    url=(base+'/wallet-read?' if deferred else TOOLS+tool+'?')+urllib.parse.urlencode({**{k:v for k,v in q.items() if v},**({'kind':tool} if deferred else {})})
    try:
        with urllib.request.urlopen(forwarded_request(url), timeout=4 if deferred else 8) as r:
            value=json.loads(r.read(524289))
            if deferred:
                if value.get('state')=='complete':return value['result']
                if value.get('state')=='failed':raise LookupError(value.get('error') or 'The retained source read could not finish.')
                return {'kind':tool,'chain':q['chain'],'address':q['address'],'status':'preparing','job':value.get('job'),'message':'Reading this exact wallet source. The shared assistant will continue the retained read.'}
            return value
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read(20_000)).get('detail')
        except ValueError:
            detail = None
        raise LookupError(detail or 'Economic Machine data is unavailable for this request.')
    except OSError:
        raise LookupError('Economic Machine data is unavailable right now.')


def symbols(q):
    return [w.replace('.', '') for w in re.findall(r'(?<![A-Za-z0-9])[A-Z][A-Z0-9.]{1,9}(?![A-Za-z0-9])', q) if w not in STOP]


# ---- EM layer em3 (apply_intel_em3.py): onchain search intents on the Economic Machine V2 routes ------------------------------
import datetime as _dt
import threading
import time
_L = threading.local()


def ko():
    return getattr(_L, 'ko', False)


def T(en, kr):
    """The answer in the question's language (Korean when the question has Hangul)."""
    return kr if ko() else en


EM_BASE = TOOLS.split('/tools/')[0]   # http://127.0.0.1:8896/api/public/em


def em(path, timeout=25, **q):
    """GET an Economic Machine route on the bridge (graph, featured, holders-at, address-chains); LookupError with its reason."""
    url = EM_BASE + '/' + path + '?' + urllib.parse.urlencode({k: v for k, v in q.items() if v not in (None, '')})
    for attempt in (0, 1):   # one retry when the connection drops (not on an HTTP answer)
        try:
            with urllib.request.urlopen(forwarded_request(url), timeout=timeout) as r:
                return json.loads(r.read(3_000_000))
        except urllib.error.HTTPError as e:
            if e.code == 503 and not attempt:   # a busy engine answers again a moment later
                time.sleep(0.8)
                continue
            try:
                detail = json.loads(e.read(20_000)).get('detail')
            except ValueError:
                detail = None
            err = LookupError(T(detail or 'being prepared for this request.', '준비 중이에요.' if e.code in (404, 501, 503) or not detail else '지금은 읽을 수 없어요.'))
            err.code, err.detail = e.code, detail or ''
            raise err
        except OSError:
            if attempt:
                raise LookupError(T('Economic Machine data is unavailable right now.', '지금은 Economic Machine 데이터를 읽을 수 없어요.'))


def _one_edit(a, b):
    if a == b or abs(len(a) - len(b)) > 1:
        return a == b
    if len(a) == len(b):
        d = [i for i in range(len(a)) if a[i] != b[i]]
        return len(d) == 1 or len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]]
    if len(a) > len(b):
        a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))


def fuzzy_chain(q):
    """A chain name typed with one typo ("polyogn", "linae", "arbitrm"): names of 5+ letters only, one chain or none."""
    words = [w for w in re.findall(r'[a-z]{4,}', q.lower()) if len(w) >= 5 or w not in _vocab()]
    hits = {c for w in words for a, c in chain_catalog.aliases().items() if ' ' not in a and len(a) >= 5 and a.isascii() and _one_edit(w, a)
            and (len(w) >= 5 or len(a) == 5)}
    return hits.pop() if len(hits) == 1 else None


def intent_chain(q):
    """The chain a token or entity question names. The intent itself is chain context ("USDC holders on arc", "아크 큰손"), so this
    also accepts what chain_catalog.hints keeps only with wallet or chart words; a bare ETH stays the asset, not Ethereum."""
    found = chain_catalog.hints(q) or chain_catalog.hints(q + ' chain') or ([fuzzy_chain(q)] if fuzzy_chain(q) else [])
    if found == ['ethereum'] and not re.search(r'(?i)\bethereum\b|이더리움|mainnet|메인넷', q):
        found = []
    return found[0] if len(found) == 1 else None


def utc(t):
    try:
        if t is None or int(t)<=0:return ''
        return _dt.datetime.fromtimestamp(int(t), _dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    except (TypeError, ValueError, OSError):
        return ''


def as_of(a, chain=None):
    """'as of <chain> block N, <time> UTC' from an as_of / asOf object (one chain or per chain)."""
    if not isinstance(a, dict):
        return T('as of ' + utc(a), utc(a) + ' 기준') if isinstance(a, (int, float)) and a else ''
    if 'block' not in a and 'time' not in a:
        parts = [as_of(v, k) for k, v in a.items() if isinstance(v, dict)]
        return '; '.join(p for p in parts if p)
    c = CHAIN.get(chain or a.get('chain'), chain or a.get('chain') or '')
    blk = '{:,}'.format(a['block']) if isinstance(a.get('block'), int) and a['block']>0 else ''
    if not blk and not utc(a.get('time')):return ''
    return T('as of %s block %s, %s' % (c, blk, utc(a.get('time'))), '%s 블록 %s(%s) 기준' % (c, blk, utc(a.get('time'))))


GRADE_KO = {'proven': '확인됨', 'matched': '대조됨', 'inferred': '추정', 'judged': '판단', 'official': '공식', 'strong': '강함', 'candidate': '후보'}


def grade(g):
    if not g:
        return T('ungraded', '등급 없음')
    return '%s(%s)' % (GRADE_KO.get(g, g), g) if ko() else g


def amount(x):
    a = abs(x or 0)
    return next(('%.2f%s' % (a / d, u) for d, u in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')) if a >= d), '%.4g' % a)


def held_reason(c):
    """The engine's held / spam answer (holders-at, tools/holders, accumulators): its reason, never an amount."""
    if c.get('spam') or c.get('class') in ('spam', 'revert'):
        return T('this token is classified as spam (a fake or look-alike token), so it is not ranked.', '스팸으로 분류된 토큰이에요(가짜·사칭 토큰). 순위를 보여 주지 않아요.')
    return (c.get('reasonKo') if ko() else None) or c.get('reason') or c.get('heldReason') or T('being prepared.', '준비 중이에요.')


def label(n):
    name = (n.get('entity') or n.get('label') or n.get('categoryName') or n.get('category') or T('wallet', '지갑')).replace('_', ' ')
    return '%s %s' % (name, short(n.get('address') or ''))


SELL = re.compile(r'(?i)\b(?:selling|seling|sold|sells|sellers?|distribut\w*|dump\w*)\b|팔고|파나|팔았|파는|매도|던지|던졌|분산')
GRAPH = re.compile(r'(?i)\b(?:connected|linked|related)\s+(?:wallets?|addresses?|accounts?)\b|\b(?:wallets?|addresses?|accounts?)\s+(?:connected|linked|related)\s+(?:to|with)\b'
                   r'|\bwho\s+(?:sent|sends|funded|funds|paid|gave|transferred)\b|누가.{0,60}?(?:보냈|보낸|줬|송금)|(?:돈|자금|이더|코인).{0,12}?(?:보냈|보낸|받았|받은)'
                   r'|\b(?:funds?|money)\s+flows?\b|\bflow\s+of\s+(?:funds|money)\b|\bcounterpart(?:y|ies)\b|\bconnections?\b|\bfunded\s+by\b'
                   r'|연결된?\s?(?:지갑|주소)|연결\s?관계|누가\s?\S*\s?(?:돈|자금|이더|ETH|USDC|코인)?\s?(?:을|를)?\s?(?:보냈|보낸|줬|송금)|자금\s?(?:흐름|출처)'
                   r'|돈\s?(?:흐름|어디서|어디로)|거래\s?상대|(?:자금|돈)\s?(?:flow|플로우)|\bflows?\s+(?:of|for|around)\b'
                   r'|(?:\bmoney|\bfunds?|자금|돈)\s?(?:fow|flw|folw|flwo|floww)\b')
FEATURED = re.compile(r'(?i)\b(?:big|biggest|major|mjor|majr|maor|main|key|top)\s+(?:players?|entities|wallets|accounts|whales)\b|\bwho\s+are\s+the\s+(?:big|main|major)\s+players\b'
                      r'|\b(?:whales?|whals|wahles|whles)\s+on\b|큰손|주요\s?(?:엔티티|지갑|주체|플레이어|세력)|대표\s?(?:엔티티|지갑)|고래(?:들)?\s?(?:누구|목록|보여)')
HOLDERS = re.compile(r'(?i)\b(?:holders?|holdrs|holdres|who\s+(?:held|holds|hols|hods|hodls|hlds)|top\s+wallets|largest\s+wallets|who\s+owns|owners?)\b|\b(?:largest|biggest|top)\s+\S+\s+(?:wallets|holders)\b|보유자|홀더|누가\s?(?:들고|가지고|보유)|상위\s?(?:지갑|보유)|많이\s?(?:들고|가진)')
WHEN = re.compile(r'(?i)\bwhen\b|\b(?:over|ovr|oevr|ovre|oer)\s+(?:time|tiem|tme)\b|\btimeline\b|\bpiled?\s+in\b|언제|시점|몰렸|몰린|추이|변화')
CHARTY = re.compile(r'(?i)\b(?:chart|candles?|price|terminal|ohlc)\b|차트|캔들|가격|터미널')


EN_WORDS = set("""WHO WHAT WHATS WHEN WHERE WHICH WHY HOW ARE IS WAS THE A AN ON IN OF FOR TO FROM BY WITH AND OR ME MY I YOU YOUR PLEASE PLS
THANKS ASAP HEY YO HI OK CAN COULD WOULD SHOW OPEN VIEW FIND TELL ABOUT INFO LIST TOP BIG BIGGEST MAJOR MAIN KEY LARGEST PLAYERS PLAYER ENTITIES ENTITY
WHALE WHALES NEW HOLDER HOLDERS HOLDS HOLD OWNS OWNERS WALLET WALLETS ADDRESS CONNECTED LINKED RELATED COUNTERPARTIES COUNTERPARTY FLOW FLOWS MONEY FUNDS
SENT FUNDED WHO'S ACCUMULATING ACCUMULATION BUYING SELLING DUMPING DISTRIBUTION OVER TIME TIMELINE PILE PILED DID THIS PAST LAST TODAY WEEK MONTH QUARTER
HOUR DAY DAYS CHART PRICE PRO TERMINAL MARKETS MARKET DOCS RESEARCH BLOG PRICING NETWORK CHAIN MAINNET ONE POS EVM BALANCE POSITIONS CHECK ACTIVITY
OTHER CHAINS CROSS BRIDGE MAP WANT KNOW QUESTION LOOKING ONCHAIN DATA EARLIER LIKE LOT""".split())   # QUICK is a token (Polygon)


def _chain_words():
    return {w.upper() for a in chain_catalog.aliases() for w in a.split()}


CHAIN_TICKERS = {'ARB', 'OP', 'MATIC', 'POL', 'LINEA', 'ETH', 'ARC'}   # tickers spelled like a chain name


def _named_apart(s, q):
    """The chain a ticker-like chain word belongs to is also named by another spelling ("arb on arbitrum one", "matic polygon pos"),
    or the word comes twice ("linea holders on linea")."""
    al = chain_catalog.aliases()
    mine, low = al.get(s.lower()), q.lower()
    if any(c == mine and s.lower() not in a.split() and re.search(r'(?<![\w])' + re.escape(a) + r'(?![\w])', low) for a, c in al.items()):
        return True
    return len(re.findall(r'(?i)(?<![\w])' + re.escape(s) + r'(?![\w])', q)) >= 2


def loose_symbols(q):
    """Lower-case tickers ("base usdc holders", "robinhood hoodie holders"): words that are not English, intent or chain words."""
    chainw = _chain_words()
    out = []
    for w in re.findall(r'(?<![\w$])\$?([A-Za-z][A-Za-z0-9.]{1,11})(?![\w])', q):
        u = w.upper().rstrip('.')
        if not w.islower() or u in EN_WORDS or len(u) > 10:
            continue
        if u in chainw and not (u in CHAIN_TICKERS and _named_apart(w, q)):
            continue
        if u not in out:
            out.append(u)
    return out


def asset_symbols(q):
    """Token symbols in the question, without the chain names written in capitals (ARC, BASE) or inside a multi-word chain name
    ("OP Mainnet", "Arbitrum One", "Base mainnet")."""
    chains = {a.upper() for a, c in chain_catalog.aliases().items()}
    low = q.lower()
    inside = {w.upper() for a in chain_catalog.aliases() if ' ' in a and re.search(r'(?<![\w])' + re.escape(a) + r'(?![\w])', low) for w in a.split()}
    caps = [w.rstrip('.') for w in re.findall(r'(?<![A-Za-z0-9])\$?([A-Z][A-Z0-9.]{1,9})(?![A-Za-z0-9])', q)]
    out = []
    for s in caps:
        if s in EN_WORDS:
            continue
        if s in chains and s not in ('ARB', 'OP', 'ETH'):   # a chain name in capitals is a token only when the chain is also named apart
            if not _named_apart(s, q):
                continue
        elif s in inside and len(re.findall(r'(?i)(?<![\w])' + re.escape(s) + r'(?![\w])', q)) < 2:
            continue
        out.append(s)
    return out


MAPWORD = re.compile(r'(?i)\b(?:wallet\s+)?map(?:ping)?\b|지갑\s?(?:맵|매핑|지도)|매핑|지도')
PRICING = re.compile(r'(?i)\b(?:pricing|price\s+plans?|plans?\s+and\s+pricing|subscription|how\s+much\s+(?:is|does)\s+(?:pro|ticking|the\s+pro)|pro\s+plan|upgrade)\b|요금|구독|프로\s?(?:얼마|가격|요금)|유료\s?플랜|플랜\s?(?:가격|얼마)')
TOKEN_INFO = re.compile(r'(?i)^(?:what\s+is|what\'?s|wat\s+is|whta\s+is|wht\s+is|tell?\s+me\s+about|tel\s+me\s+about|info\s+(?:on|about))\s+\$?([A-Za-z][A-Za-z0-9.]{1,11})\??$'
                        r'|^\$?([A-Za-z][A-Za-z0-9.]{1,11})\s+(?=.*(?:info|ifo|inof|ino|inf|정보|뭐야|뭐임|알려줘|코인))'
                        r'(?:(?:info|ifo|inof|ino|inf|정보|뭐야|뭐임|알려줘|어떤|무슨|코인|좀|해줘|줘|보여줘|부탁해|please|pls)\s*)+[?!.]*$')
def research_requested(q, requested):
    """wallet_research.requested, except when its only trigger is "whale" inside a longer token name ("BLUEWHALE chart")."""
    if not requested(q):
        return False
    masked = re.sub(r'(?i)[a-z0-9]*whale[a-z0-9]*', lambda m: m.group(0) if re.fullmatch(r'(?i)whales?', m.group(0)) else 'TKN', q)
    return masked == q or requested(masked)


INDICATOR = re.compile(r'(?i)chaikin|money\s+flow\s+index|\bmfi\b|\bcmf\b')   # chart indicators, not the economy's money flows


_VOCAB = None


def _vocab():
    """Intent and grammar words (em3 phrases, the navigation grammar, the native commands' example words) that a typo is repaired to."""
    global _VOCAB
    if _VOCAB is None:
        words = set(w.lower() for w in EN_WORDS) | set("""holders holder accumulating accumulation selling buying dumping players entities connected connections connection counterparties
flow funded markets market docs documentation research articles pricing plans subscription terminal chart charts mainnet network timeline quarter month
week weeks today hour hours days months quarter wallet wallets whales info bridge ethereum arbitrum polygon optimism unichain linea robinhood hyperevm upgrade""".split())
        try:
            from economic_machine import native_navigation as _N
            for c in _N.commands:
                for e in c.get('examples') or []:
                    words.update(w for w in re.findall(r'[a-z]{4,}', e.lower()))
        except Exception:
            pass
        words.update(w for a in chain_catalog.aliases() for w in re.findall(r'[a-z]+', a.lower()))   # chain names are words, never typos
        words.update(r['name'].lower() for r in chain_catalog.rows() if isinstance(r.get('name'), str))
        _VOCAB = {w for w in words if len(w) >= 4}
    return _VOCAB


SWAP4 = set("""flow sent send docs base fund from info sell hold buys whom what show open time hour days week""".split())


def fix_typos(q):
    """Repair one-edit typos in words of 5+ letters that are not words already (never tickers in capitals, numbers or addresses);
    a 4-letter lower-case word only by swapping two neighbouring letters into an intent or chain word ("flwo", "setn", "baes")."""
    V = _vocab()
    latin = re.findall(r'(?<![0-9a-zA-Z])(?!0x)[A-Za-z]{2,}(?![0-9a-zA-Z])', q)
    shouted = len(latin) >= 3 and sum(w.isupper() for w in latin) >= 0.8 * len(latin)
    def fix(m):
        w = m.group(0)
        lw = w.lower()
        if lw in V or (w.isupper() and (len(w) < 6 or not shouted)) or lw.endswith('s') and lw[:-1] in V or lw + 's' in V:
            return w
        if len(lw) == 4:
            if not w.islower():
                return w
            hits = {v for v in SWAP4 | {a for a in chain_catalog.aliases() if len(a) == 4 and a.isalpha() and a.isascii()}
                    if v[0] == lw[0] and sorted(v) == sorted(lw) and _one_edit(lw, v)}
            return hits.pop() if len(hits) == 1 else w
        hits = {v for v in V if abs(len(v) - len(lw)) <= 1 and v[0] == lw[0] and _one_edit(lw, v)}
        if len(hits) > 1:   # "holdesr": the same-length swap (holders) beats a deletion (holder)
            same = {v for v in hits if len(v) == len(lw)}
            hits = same if len(same) == 1 else hits
        if len(hits) != 1:
            return w
        v = hits.pop()
        return v.upper() if w.isupper() else v.capitalize() if w[0].isupper() else v
    q = re.sub(r'(?<![0-9a-zA-Z])(?!0x)[A-Za-z]{4,}(?![0-9a-zA-Z])', fix, q)
    al = chain_catalog.aliases()
    def split(m):
        return m.group(1) + ' ' + m.group(2) if m.group(1).casefold() in al else m.group(0)
    return re.sub(r'([A-Za-z가-힣]+?)(에서|에선|에는|에|의|요|은|는|을|를|으로|로)(?=\s|$|[?!.,])', split, q)


def em3_requested(q):
    """An em3 intent that must reach em_wallet_intent before wallet research and navigation."""
    addrs = ADDR.findall(q)
    if investigation.subject().get('kind')=='wallet' and re.search(r'(?i)holdings|portfolio|balances?|transactions?|transfers?|trades?|activity|trace|잔고|보유|거래|이체|활동|추적',q):return True
    if INDICATOR.search(q) and not addrs:
        return False
    if MAPWORD.search(q) and not GRAPH.search(q) and not HOLDERS.search(q) and not accumulate_requested(q):
        return False   # wallet maps stay with Ticking's navigation
    if PRICING.search(q) and not ADDR.search(q):
        return True
    if TOKEN_INFO.search(clean_q(q)):
        return True
    if accumulate_requested(q):
        return True
    if addrs and GRAPH.search(q):
        return True
    if not addrs and FEATURED.search(q) and intent_chain(q) and not [x for x in asset_symbols(q) if x not in _chain_words()]:
        return True
    if HOLDERS.search(q) and (asset_symbols(q) or loose_symbols(q) or ADDR.search(q) or investigation.subject().get('kind')=='token') and (intent_chain(q) or WHEN.search(q) or investigation.subject().get('chain') or re.search('[가-힣]', q)):
        return True
    return len(addrs) == 1 and not chain_catalog.hints(q) and not CHARTY.search(q) and len(q.strip()) <= 60


_TOK = {}


def token_on_chain(sym, chain):
    """<chain>:0x… for a symbol the bridge search does not list yet: the engine's token list (/tokens: real tokens first, then by
    holders; a held token is returned so its answer says why), else the chain's featured snapshot (assets its big players moved)."""
    if not sym or not chain:
        return None
    try:
        for t in em('tokens', symbol=sym.upper(), chain=chain, limit=5, timeout=5).get('tokens') or []:
            if t.get('chain') == chain and (t.get('symbol') or '').upper() == sym.upper() and not t.get('spam') and t.get('market'):
                return t['market'].lower()
        failed = False
    except LookupError as e:   # a refused or timed-out read is not "no such token": then the answer says the data is unavailable
        failed = getattr(e, 'code', None) is None or e.code >= 500
    gone = LookupError(T('Economic Machine data is unavailable right now.', '지금은 Economic Machine 데이터를 읽을 수 없어요.'))
    hit = _TOK.get(chain)
    if not hit or hit[0] < _dt.datetime.now().timestamp() - 600:
        found = {}
        try:
            for e in em('featured', chain=chain, timeout=8).get('entities') or []:
                for ed in (e.get('graph') or {}).get('edges') or []:
                    a = ed.get('asset') or {}
                    if a.get('symbol') and a.get('address'):
                        found.setdefault(a['symbol'].upper(), {}).setdefault(a['address'].lower(), 0)
                        found[a['symbol'].upper()][a['address'].lower()] += ed.get('n') or 0
        except (LookupError, OSError, ValueError):
            if not hit:   # a failed read is never remembered: the next question asks again
                if failed:
                    raise gone
                return None
            found = None
        if found is not None:
            hit = _TOK[chain] = (_dt.datetime.now().timestamp(), {k: max(v, key=v.get) for k, v in found.items()})
    t = hit[1].get(sym.upper())
    if not t and failed:
        raise gone
    return '%s:%s' % (chain, t) if t else None


# Markets whose balances are known wrong in the engine until it says otherwise (native gas tokens: value moves and gas carry no
# Transfer log). Answered as "being reconciled", never with an amount or share.
NATIVE_HELD = set()  # Availability comes from the source's current reconciliation gate.


def native_held(market, sym=None):
    if market not in NATIVE_HELD:
        return None
    net = CHAIN.get(market.split(':')[0], market.split(':')[0])
    return T('%s %s is the chain\'s native gas token; its balances are being reconciled against the node (native transfers carry no token log), so holders and flows are not shown yet.',
             '%s %s는 체인의 네이티브 가스 토큰이라 노드 잔고와 대조하는 중이에요(네이티브 이동에는 토큰 로그가 없어요). 그래서 보유자와 흐름은 아직 보여 주지 않아요.') % (net, sym or 'USDC')


def em_market(sym, chain, chart, ctx, q):
    """<chain>:0x… for a token question: the open chart, a chain:0x… or 0x… in the question (with its chain), else search on the chain."""
    lit = re.search(r'\b([a-z][a-z0-9]{1,15}):(0x[0-9a-fA-F]{40})\b', q)
    if lit:
        return lit.group(1).lower() + ':' + lit.group(2).lower()
    selected=investigation.subject()
    if selected.get('kind')=='token' and selected.get('asset'):
        if chain and selected.get('chain')!=chain:raise LookupError('Choose an exact token on the requested chain; no network was substituted.')
        return selected['asset']
    open_ = (chart or {}).get('asset')
    if isinstance(open_, str) and MARKET.fullmatch(open_) and (not sym or sym == ctx) and (not chain or open_.startswith(chain + ':')):
        return open_
    a = ADDR.search(q)
    if a and chain:
        return '%s:%s' % (chain, a.group(0).lower())
    return (search_market(sym, chain) or token_on_chain(sym, chain)) if sym else None


def graph_card(q, addr, chain):
    hops = 2 if re.search(r'(?i)\b(?:2|two)\s*hops?\b|2\s?홉|두\s?단계|2단계', q) else 1
    win = ('24h' if re.search(r'(?i)\b(?:24h|today)\b|오늘|하루', q) else '7d' if re.search(r'(?i)\b(?:7d|week)\b|일주일|이번\s?주|7일', q)
           else '30d' if re.search(r'(?i)\b(?:30d|month)\b|한\s?달|30일', q) else 'all')
    chosen=investigation.selection()
    if any(k in chosen for k in ('block','time','from','to')):
        return {'state':'wallet_card','message':T('This graph reader cannot prove the selected historical point. Open the exact-time investigation; no current or all-time graph was substituted.','이 그래프 조회는 선택한 과거 시점을 검증하지 못해요. 현재·전체 기간 그래프로 바꾸지 않았어요.'),'card':{'kind':'graph','chain':chain,'address':addr,'available':False,'reason':'historical_graph_point_unavailable'}}
    win=chosen.get('window',win)
    try:
        g=em('graph',address=addr,chain=chain,hops=hops,window=win,limit=25)
        returned=(g.get('query') or {}).get('window')
        if returned and returned!=win:raise LookupError('The returned graph covers a different time window. No all-time graph was substituted.')
        investigation.validate_evidence(g,chain=chain)
    except LookupError as e:
        return {'state':'wallet_card','message':T('Connections of %s: %s','%s의 연결: %s')%(short(addr),e),'card':{'kind':'graph','chain':chain,'address':addr,'available':False,'reason':getattr(e,'detail','') or str(e)}}
    return {'state':'wallet_card','message':graph_message(g,addr),'card':dict(g,kind='graph',address=addr)}


def graph_message(g, addr, top=5):
    message = _graph_message(g, addr, top)
    coverage = g.get('coverage') or {}
    if coverage.get('mode') == 'retained_current_observations':
        prefix = T('Partial current observations only: retained configured stablecoin transfers of at least $100K face value, not the full requested history or all assets. ',
                   '현재 관측 일부예요. 보관된 10만 달러 상당 이상 지정 스테이블코인 이체이며, 요청 기간 전체·모든 자산의 장부는 아니에요. ')
        return prefix + message
    return message


def _graph_message(g, addr, top=5):
    if not g.get('available', True):
        return T('Connections of %s: %s', '%s의 연결: %s') % (short(addr), g.get('reason') or T('being prepared.', '준비 중이에요.'))
    nodes = {n['id']: n for n in g.get('nodes') or []}
    roots = {i for i, n in nodes.items() if n.get('hop') == 0}
    if not roots:
        return T('No transfer rows were returned for %s in this requested window. This bounded read does not establish zero activity.', '%s의 선택 기간에 이체 행이 반환되지 않았어요. 제한된 조회이므로 활동이 0이라는 뜻은 아니에요.') % short(addr)
    cps = {}
    for e in g.get('edges') or []:
        if e.get('hop') != 1 or not ({e['source'], e['target']} & roots):
            continue
        other = e['target'] if e['source'] in roots else e['source']
        d = cps.setdefault(other, {'n': 0, 'out': [], 'in': [], 'grades': set(), 'usd': 0.0})
        d['n'] += e.get('n') or 0
        d['grades'].add(e.get('grade'))
        if e.get('usd'):
            d['usd'] += e['usd']
        (d['out'] if e['source'] in roots else d['in']).append(e)
    if not cps:
        return T('No direct transfer rows were returned for %s in this requested window. This bounded read does not establish zero activity.', '%s의 선택 기간에 직접 이체 행이 반환되지 않았어요. 제한된 조회이므로 활동이 0이라는 뜻은 아니에요.') % short(addr)
    lines = []
    for other, d in sorted(cps.items(), key=lambda kv: -kv[1]['n'])[:top]:
        n = nodes.get(other, {'address': other.split(':')[-1]})
        def legs(es, verb):
            es = sorted(es, key=lambda e: -(e.get('n') or 0))[:2]
            result = []
            for e in es:
                n = e.get('n') or 0
                quantity = e.get('amount')
                if quantity is not None:
                    quantity = quantity if isinstance(quantity, str) else amount(quantity)
                    result.append('%s %s %s (%s)' % (verb, quantity, (e.get('asset') or {}).get('symbol') or '', T('%d transfers' % n, '%d회' % n)))
                elif e.get('kind') == 'call' and e.get('amountRaw') in (0, '0'):
                    result.append(T('%d zero-value calls' % n, '금액 없는 호출 %d회' % n))
                else:
                    result.append(T('%d recorded movements; amount unavailable' % n, '기록된 이동 %d회 · 수량 미확인' % n))
            return result
        parts = legs(d['out'], T('sent', '보냄')) + legs(d['in'], T('received', '받음'))
        g_ = '/'.join(grade(x) for x in sorted(x for x in d['grades'] if x))
        hub = T(' · hub, not expanded', ' · 허브라 더 펼치지 않음') if n.get('stop') else ''
        usd_ = ' · ≈%s' % usd(d['usd']) if d['usd'] else ''
        lines.append('%s: %s%s · %s%s' % (label(n), ', '.join(parts), usd_, g_, hub))
    chains = sorted({nodes[r].get('chain') for r in roots})
    head = T('Top connections of %s on %s (%s, %s):', '%s의 주요 연결, %s(%s, %s):') % (
        short(addr), ', '.join(CHAIN.get(c, c) for c in chains), T('hops %d' % g.get('query', {}).get('hops', 1), '%d홉' % g.get('query', {}).get('hops', 1)),
        as_of(g.get('asOf') or g.get('as_of')))
    tail = []
    if g.get('stops'):
        tail.append(T('%d hubs (exchanges, routers, pools, bridges) are shown but not expanded.', '허브 %d곳(거래소·라우터·풀·브리지)은 보여만 주고 더 펼치지 않았어요.') % len(g['stops']))
    if g.get('xcLegs'):
        tail.append(T('%d cross-chain bridge moves are included.', '크로스체인 브리지 이동 %d건이 포함돼 있어요.') % g['xcLegs'])
    if g.get('edgesHidden'):
        tail.append(T('%d more counterparty rows are not shown.', '외 %d건은 보여 주지 않았어요.') % g['edgesHidden'])
    if g.get('spamEdgesHidden'):
        tail.append(T('%d transfers of spam or fake tokens are left out.', '스팸·가짜 토큰 이체 %d건은 뺐어요.') % g['spamEdgesHidden'])
    tail.append(T('USD is shown for stablecoins only for now.', 'USD는 지금은 스테이블코인만 보여요.'))
    return head + ' ' + '; '.join(lines) + '. ' + ' '.join(tail)


def featured_card(chain):
    try:
        f = em('featured', chain=chain)
    except LookupError as e:
        return {'state': 'wallet_card', 'message': T('Major entities on %s: %s', '%s의 주요 엔티티: %s') % (CHAIN.get(chain, chain), e),
                'card': {'kind': 'featured', 'chain': chain, 'available': False, 'reason': str(e)}}
    ents = f.get('entities') or []
    if not ents:
        return {'state': 'wallet_card', 'message': T('Major entities on %s are being prepared.', '%s의 주요 엔티티는 준비 중이에요.') % CHAIN.get(chain, chain),
                'card': dict(f, kind='featured')}
    names = {'exchange': T('exchanges', '거래소'), 'whale': T('whales', '고래'), 'protocol': T('protocols', '프로토콜'), 'bridge': T('bridges', '브리지')}
    groups = {}
    for e in sorted(ents, key=lambda e: -(e.get('importance') or 0)):
        groups.setdefault(e.get('group') or 'other', []).append(e)
    parts = []
    for gname, es in groups.items():
        parts.append('%s: %s' % (names.get(gname, gname), ', '.join('%s (%s)' % (label(e), grade(((e.get('graph') or {}).get('nodes') or [{}])[0].get('entityGrade')))
                                                                     for e in es[:3])))
    msg = T('Major entities on %s, ranked by how much of the chain\'s money passes through them (%s): ',
            '%s의 주요 엔티티예요. 체인의 돈이 얼마나 지나가는지로 순위를 매겼어요(%s): ') % (CHAIN.get(chain, chain), T('snapshot ' + utc(f.get('generatedAt')), utc(f.get('generatedAt')) + ' 스냅숏'))
    return {'state': 'wallet_card', 'message': msg + '; '.join(parts) + T('. Open one to see who it is connected to.', '. 하나를 열면 무엇과 연결돼 있는지 보여요.'),
            'card': dict(f, kind='featured')}


def holders_v2(q, chain, chart, ctx):
    syms = asset_symbols(q) or loose_symbols(q) or ([ctx] if ctx else [])
    sym = syms[0] if syms else None
    try:
        market = em_market(sym, chain, chart, ctx, q)
    except LookupError as e:
        return {'state': 'wallet_card', 'message': T('%s holders: %s', '%s 보유자: %s') % (sym or T('This token', '이 토큰'), e),
                'card': {'kind': 'holders', 'symbol': sym, 'chain': chain, 'available': False, 'reason': 'unavailable'}}
    if not market:
        if chain:
            return {'state': 'wallet_card', 'message': T('%s holders on %s: this market is not in our list yet.', '%s의 %s 보유자: 이 마켓은 아직 목록에 없어요.') % (sym or T('This token', '이 토큰'), CHAIN.get(chain, chain)) if not ko()
                    else '%s %s 보유자: 이 마켓은 아직 목록에 없어요.' % (CHAIN.get(chain, chain), sym or '이 토큰'),
                    'card': {'kind': 'holders', 'symbol': sym, 'chain': chain, 'available': False}}
        return None   # Ticking's own holders path (catalog by symbol)
    nh = native_held(market, sym)
    if nh:
        return {'state': 'wallet_card', 'message': nh, 'card': {'kind': 'holders', 'market': market, 'symbol': sym, 'available': False, 'reason': 'native_reconciling'}}
    point=investigation.selection()
    if any(k in point for k in ('from','to')):
        return {'state':'wallet_card','message':'The holder reader cannot prove this complete requested range. Choose one block or time; no current snapshot was substituted.','card':{'kind':'holders-at','market':market,'available':False,'reason':'historical_holder_range_unavailable'}}
    if point.get('window') in ('1h','24h','7d','30d','90d') and not any(k in point for k in ('block','time')):
        seconds={'1h':3600,'24h':86400,'7d':604800,'30d':2592000,'90d':7776000}[point['window']];end=int(time.time());snaps=[]
        for requested in (end-seconds,end):
            try:source=investigation.validate_evidence(em('holders-at',market=market,t=requested,limit=10,timeout=3),market=market)
            except LookupError as error:return {'state':'wallet_card','message':'The requested holder window is unavailable: '+str(error),'card':{'kind':'holders-at','market':market,'available':False,'reason':str(error),'requestedWindow':point['window']}}
            if source.get('held') or source.get('available') is False:return {'state':'wallet_card','message':held_reason(source),'card':dict(source,kind='holders-at',requestedWindow=point['window'])}
            matched=[x for x in source.get('snaps') or [] if x.get('requestedTime')==requested]
            if len(matched)!=1:return {'state':'wallet_card','message':'The source did not return the requested window endpoints. No latest holder snapshot was substituted.','card':{'kind':'holders-at','market':market,'available':False,'reason':'requested_holder_window_unproven','requestedWindow':point['window']}}
            snaps.extend(matched)
        combined={'kind':'holders-at','market':market,'symbol':sym,'available':True,'snaps':snaps,'requestedWindow':point['window'],'requestedRange':{'from':end-seconds,'to':end},'complete':all(x.get('complete') is True for x in snaps)}
        return {'state':'wallet_card','message':holders_timeline(combined),'card':combined}
    if WHEN.search(q) or 'block' in point or 'time' in point:
        args={'block':point['block']} if 'block' in point else {'t':point['time']} if 'time' in point else {'snaps':1}
        try:
            h = investigation.validate_evidence(em('holders-at', market=market, limit=10, **args),market=market)
        except LookupError as e:
            return {'state': 'wallet_card', 'message': T('Holders over time for %s: %s', '%s 보유자 추이: %s') % (sym or market, e), 'card': {'kind': 'holders-at', 'market': market, 'available': False}}
        return {'state': 'wallet_card', 'message': holders_timeline(h), 'card': dict(h, kind='holders-at')}
    try:
        c = investigation.validate_evidence(get('holders', market=market),market=market)
    except LookupError as e:
        return {'state': 'wallet_card', 'message': T('%s holders: %s', '%s 보유자: %s') % (sym or market, e), 'card': {'kind': 'holders', 'market': market, 'available': False}}
    return {'state': 'wallet_card', 'message': holders_v2_message(c) + launch_message(c), 'card': c}


def holders_v2_message(c):
    hs = c.get('holders') or []
    net = CHAIN.get(c.get('chain'), c.get('chain'))
    if c.get('available') is False:
        return T('%s holders on %s: %s', '%s %s 보유자: %s') % (c.get('symbol'), net, held_reason(c)) if not ko() else '%s %s 보유자: %s' % (net, c.get('symbol') or '', held_reason(c))
    if not hs:
        return T('No holder data for %s on %s yet.', '%s의 %s 보유자 데이터가 아직 없어요.') % (c.get('symbol'), net)
    bad = any((h.get('share') or 0) > 100 for h in hs)
    def row(h):
        share = '' if bad or h.get('share') is None else ' %.2f%%' % h['share']
        return '%s%s' % (label(h), share)
    when = as_of(c.get('asOf')) if c.get('asOf') else ''
    msg = T('%s on %s has %s holders%s. The largest: %s.', '%s %s 보유자 %s명%s. 상위: %s.') % (
        c.get('symbol'), net, '{:,}'.format(c['holderCount']) if c.get('holderCount') else T('many', '여러'), (', ' + when) if when else '',
        '; '.join(row(h) for h in hs[:5]))
    if bad:
        msg += T(' Shares are not shown: this token\'s supply basis does not match its balances yet.', ' 비율은 보여 주지 않아요. 이 토큰은 공급량 기준이 잔고와 아직 맞지 않아요.')
    if any(h.get('category') in ('dex_pool', 'lending', 'cex_hot_wallet', 'bridge') for h in hs[:5]):
        msg += T(' Pools, lending markets, exchanges and bridges hold for many people, not one investor.', ' 풀·대출 시장·거래소·브리지는 한 사람이 아니라 여러 사람 몫을 들고 있어요.')
    return msg


def holders_timeline(h):
    points=h.get('snaps') or []
    if not points:return T('Holder history for %s: %s','%s 보유자 이력: %s')%(h.get('symbol') or h.get('market'),held_reason(h))
    lines=[]
    for p in points:
        requested=('block '+str(p['requestedBlock'])) if p.get('requestedBlock') is not None else utc(p.get('requestedTime')) or str(p.get('key') or 'selected point')
        actual=p.get('effectiveBlock',p.get('block'))
        identity=requested+(' → '+T('actual block ','실제 블록 ')+str(actual) if actual is not None else '')
        if p.get('available') is not True:
            lines.append(identity+': '+str(p.get('reason') or p.get('code') or T('unavailable; no zero balance inferred','조회 불가; 0잔고로 판단하지 않음')));continue
        count=p.get('holders');count=count if type(count) is int else None
        if p.get('complete') is False and count==0:count=None
        text=identity+': '+(T('%s recorded holders','기록된 보유자 %s개')%format(count,',') if count is not None else T('holder count unavailable','보유자 수 미확인'))
        pct=p.get('topPct');supply_ok=p.get('supplyAvailable') is True and p.get('supplyBlock')==actual and p.get('supplyBasis')=='totalSupply() at selected block'
        if supply_ok and isinstance(pct,(int,float)) and 0<=pct<=100:text+=T('; top holders %.2f%% of totalSupply at this same block','; 같은 블록의 totalSupply 기준 상위 보유 비율 %.2f%%')%pct
        elif count!=0:text+=T('; shares withheld: same-block supply is unavailable or not reconciled','; 같은 블록의 공급량을 확인·대사하지 못해 비율은 보류')
        if p.get('timeBasis'):text+=' · '+p['timeBasis']
        lines.append(text)
    return T('Holder history for %s: ','%s 보유자 이력: ')%(h.get('symbol') or h.get('market'))+'; '.join(lines)+T('. Transfer-derived balances; a holder address is not a unique person.','. 이체에서 계산한 잔고이며, 보유 주소는 고유한 사람 수가 아니에요.')


def held_message(c, head):
    s = c.get('sheet') or {}
    if ko():
        head = '%s %s %s' % (CHAIN.get(c.get('chain'), c.get('chain')), c.get('entity') or c.get('categoryName') or '지갑', short(c.get('address') or ''))
    why = (s.get('heldReasonKo') if ko() else None) or s.get('heldReason') or T('it does not reconcile yet', '아직 대사가 맞지 않아요')
    return T('%s: balance sheet held back — %s. Amounts are not shown until it reconciles.', '%s: 대차표 보류 — %s. 대사가 맞을 때까지 금액은 보여 주지 않아요.') % (head, why)


def overview_card(addr):
    """A bare 0x: the most active chain's wallet card and the address's top connections; the other active chains are named."""
    try:
        rows = [r for r in (em('address-chains', address=addr).get('chains') or []) if isinstance(r, dict)]
    except LookupError:
        rows = []
    act = sorted((r for r in rows if chain_catalog.is_evm(r.get('chain')) and ((r.get('txSent') or 0) + (r.get('txReceived') or 0) + (r.get('dexTrades') or 0)) > 0),
                 key=lambda r: -((r.get('txSent') or 0) + (r.get('txReceived') or 0) + (r.get('dexTrades') or 0)))
    if not act:
        return chain_catalog.wallet_choices(addr)
    card = msg = None
    for r in act[:3]:   # the most active chain whose card answers within 8 s
        chain = r['chain']
        try:
            card = em('tools/wallet', timeout=8, address=addr, chain=chain)
            msg = wallet_message(card)
            break
        except LookupError:
            continue
    if card is None:
        chain = act[0]['chain']
        card, msg = {'kind': 'wallet', 'address': addr, 'chain': chain, 'available': False}, T('Wallet %s: its card is being prepared.', '%s 지갑 카드는 준비 중이에요.') % short(addr)
    try:
        g = em('graph', address=addr, hops=1, limit=10)
        msg += ' ' + graph_message(g, addr, top=3)
        card = dict(card, graph=g)
    except LookupError:
        pass
    others = [CHAIN.get(r['chain'], r['chain']) for r in act if r['chain'] != chain]
    if others:
        msg += T(' Also active on: %s.', ' 다른 활동 체인: %s.') % ', '.join(others)
    return {'state': 'wallet_card', 'message': msg, 'card': card}


def clean_q(q):
    """The question without polite fillers ("please", "좀", "?", "알려줘" stays: it is part of a token-info phrase)."""
    q = re.sub(r'(?i)^(?:i was looking at .{0,80}?(?:know|ask|see)[:,]?\s+|아까 .{0,40}?(?:그런데|근데)\s+)', '', q.strip())
    q = re.sub(r'(?i)^(?:hey|yo|hi|ok|please|pls|can you|could you|quick question:?|i want to know)[ ,]+', '', q)
    q = re.sub(r'(?i)[.,]?\s*(?:thanks a lot|thank you|thanks|고마워|감사해요|감사합니다)[.!]?$', '', q)
    return re.sub(r'(?i)[\s,]*(?:please|pls|thanks|for me|asap|좀|요|!|\?)+$', '', q).strip()


def em3_resolve(q, chart, ctx, addrs, chain):
    """em3 intents in order; None hands the question to the rest of resolve(); 'map' sends it back to navigation."""
    text = q.lower()
    if INDICATOR.search(q) and not addrs:
        return 'map'   # "chaikin money flow 20": a chart indicator for the native router
    if MAPWORD.search(q) and not GRAPH.search(q) and not HOLDERS.search(q) and not accumulate_requested(q):
        return 'map'
    if PRICING.search(q) and not addrs:
        return {'state': 'ready', 'title': T('Plans and pricing', '요금제'), 'route': '#/pricing',
                'message': T('Plans and pricing: current access and prices come from the console catalog.', '현재 요금과 이용 범위는 콘솔 상품 목록에서 확인할 수 있어요.')}
    m = TOKEN_INFO.search(clean_q(q))
    if m and not addrs:
        sym = (m.group(1) or m.group(2) or '').upper()
        return {'state': 'ready', 'title': sym, 'route': '#/search?q=' + urllib.parse.quote(sym),
                'message': T('%s: here is what Ticking has on it (markets, holders, research).', '%s: Ticking에 있는 정보(마켓, 보유자, 리서치)를 보여 줄게요.') % sym}
    if len(addrs) == 1 and GRAPH.search(q):
        return graph_card(q, addrs[0], chain)
    ichain = chain or intent_chain(q)
    if not addrs and FEATURED.search(q) and ichain and not [x for x in asset_symbols(q) if x not in _chain_words()]:
        return featured_card(ichain)
    if HOLDERS.search(q) and (asset_symbols(q) or loose_symbols(q) or addrs or (chart or {}).get('asset')) and (ichain or WHEN.search(q) or investigation.selection() or re.search('[가-힣]', q)) and not accumulate_requested(q):
        r = holders_v2(q, ichain, chart, ctx)
        if r is not None:
            return r
    if len(addrs) == 1 and not chain and not CHARTY.search(q) and not accumulate_requested(q) and not HOLDERS.search(q):
        return overview_card(addrs[0])
    return None


def wallet_message(c):
    if c.get('status')=='preparing':return c['message']
    if c.get('available') is False or c.get('status')=='coming' and c.get('inMap') is False:return str(c.get('message') or 'The requested wallet source is not connected for this chain. No balance or position was inferred.')
    s = c.get('sheet') or {}
    who = c.get('entity') or c.get('categoryName') or 'Wallet'
    head = '%s %s on %s' % (who, short(c['address']), CHAIN.get(c.get('chain'), c.get('chain')))
    if s.get('held'):   # EM layer em3: held = the reason, never an amount
        return held_message(c, head)
    if not s.get('net_worth') and not s.get('collateral') and not s.get('debt'):
        return head + ': no open lending, LP or perp positions were decoded in this returned source. This does not establish that the wallet has no other positions.'
    pos = c.get('positions') or []
    coll = [p for p in pos if p.get('kind') == 'collateral' and p.get('usd')]
    debt = [p for p in pos if p.get('kind') == 'debt' and p.get('usd')]
    parts = ['net worth %s' % usd(s.get('net_worth'))]
    if s.get('collateral'):
        names = ', '.join(dict.fromkeys(p['asset'] for p in coll[:4] if p.get('asset')))
        parts.append('%s posted as collateral%s' % (usd(s['collateral']), ' (%s)' % names if names else ''))
    if s.get('debt'):
        names = ', '.join(dict.fromkeys(p['asset'] for p in debt[:3] if p.get('asset')))
        where = ', '.join(dict.fromkeys(p['protocol'] for p in debt[:3] if p.get('protocol')))
        parts.append('%s borrowed%s%s' % (usd(abs(s['debt'])), ' in %s' % names if names else '', ' on %s' % where if where else ''))
    if s.get('lp'):
        parts.append('%s in liquidity positions' % usd(s['lp']))
    msg = head + ': ' + '; '.join(parts) + '.'
    if s.get('collateral') or s.get('lp'):
        msg += ' That money was not sold — it sits in positions the wallet still owns.'
    if c.get('flags'):
        msg += ' Unverified: ' + ', '.join(FLAG.get(f, f) for f in c['flags']) + '.'
    return msg


def top_message(c, sym):
    rows = c.get('rows') or []
    if not rows:
        return 'No wallet in the Economic Machine balance sheets borrowed USDG against %s.' % (sym or 'Robinhood stock tokens')
    r = rows[0]
    lead = ', '.join('%s %s' % (k, usd(v)) for k, v in list((r.get('collateral') or {}).items())[:3])
    return ('%d wallet%s borrowed USDG against %s on Morpho Blue. The largest, %s, posted %s as collateral and owes %s USDG '
            '(net worth %s). Select a wallet to open its dossier and see its actions on the chart.'
            % (len(rows), '' if len(rows) == 1 else 's', sym or 'Robinhood stock tokens', short(r['address']), lead, usd(r.get('usdgDebt')), usd(r.get('netWorth'))))


ACCUMULATE = re.compile(r'(?i)\b(accumulat\w*|distribut\w*|who (?:is|are) (?:buying|selling|accumulating|dumping|bying|seling)|new whal\w*)\b|누가\s?\S*\s?(?:모으|사\s?모으|매집|팔|던지|파나|파는|파니|매도|매수)|(?:매도|매수|매집)\s?누가|매집|모으는|모으고|사\s?모으|새\s?고래|분산')


def accumulate_requested(q):
    """A who-is-accumulating / distributing question about a named token (EMV2 /accumulators)."""
    return bool(ACCUMULATE.search(q)) and bool(symbols(q) or asset_symbols(q) or loose_symbols(q) or ADDR.search(q))   # stablecoins count; AAVE / lower-case tickers too (em3)


def accumulate_card(q, chart, ctx, text, addrs):
    """Who is accumulating: the market from the open chart, a chain:0x… or 0x… token in the question (with the question's chain),
    the holders card (catalog by symbol) or search; then tools/accumulators (core's EMV2 card: buyers / sellers)."""
    syms = asset_symbols(q) or loose_symbols(q) or ([ctx] if ctx else [])
    ch = question_chain(q) or intent_chain(q)
    lit = re.search(r'\b([a-z][a-z0-9]{1,15}):(0x[0-9a-fA-F]{40})\b', q)
    open_ = (chart or {}).get('asset')
    market = open_ if isinstance(open_, str) and MARKET.fullmatch(open_) and (not syms or syms[0] == ctx) and (not ch or open_.startswith(ch+':')) else None
    try:
        if not market and lit:
            market = lit.group(1).lower() + ':' + lit.group(2).lower()
        elif not market and addrs and ch:
            market = '%s:%s' % (ch, addrs[0])
        elif not market and syms:   # a holders card on another chain than the one asked is not an answer: then search
            market = search_market(syms[0], ch) or token_on_chain(syms[0], ch)   # EM layer em3: search first (fast), then the holders card
            if not market:
                try:
                    h = get_on('holders', ch, symbol=syms[0])
                    market = '%s:%s' % (h['chain'], h['token']) if not ch or h.get('chain') == ch else None
                except (LookupError, KeyError):
                    market = None
            if not market and ch:
                return {'state': 'wallet_card', 'message': 'Who is accumulating %s on %s: this market is not in our list yet.' % (syms[0], CHAIN.get(ch, ch)),
                        'card': {'kind': 'accumulators', 'symbol': syms[0], 'network': ch, 'available': False, 'reason': 'Market not listed yet.'}}
        if not market:
            return {'state': 'clarify', 'message': 'Which token, on which chain? Try “who is accumulating NVDA this week”, or open its chart first.'}
        if native_held(market, syms[0] if syms else None):   # EM layer em3: known-wrong native balances
            return {'state': 'wallet_card', 'message': native_held(market, syms[0] if syms else None), 'card': {'kind': 'accumulators', 'market': market, 'available': False, 'reason': 'native_reconciling'}}
        selected=investigation.selection()
        if any(k in selected for k in ('block','time','from','to')):raise LookupError('This accumulation reader cannot prove the selected historical point. No current window was substituted.')
        win = selected.get('window') or acc_window(text)
        card = investigation.validate_evidence(get('accumulators', market=market, window=win),market=market)

    except LookupError as e:
        card = {'kind': 'accumulators', 'symbol': syms[0] if syms else None, 'market': market, 'network': (market or '').split(':')[0] or ch,
                'available': False, 'reason': str(e)}
    card['ask'] = 'sell' if SELL.search(q) else 'buy'
    return {'state': 'wallet_card', 'message': accumulators_message(card), 'card': card}


def accumulators_message_em3(c, sell_q):
    def who(r):
        return '%s %s (%s)' % ((r.get('label') or T('wallet', '지갑')).replace('_', ' '), short(r['address']), grade(r.get('grade')))
    buy, sell = c.get('buyers') or [], c.get('sellers') or []
    net = CHAIN.get(c.get('network'), c.get('network'))
    sym = c.get('symbol')
    if not buy and not sell:
        return c.get('message') or T('No net %s balance changes on %s in this window.', '이 기간 %s의 %s 순잔고 변화가 없어요.') % (sym, net)
    w = c.get('window') or {}
    pre = T('The last %s is not ready on this chain yet, so here is the last day. ', '이 체인은 %s 데이터가 아직 준비 중이라 지난 하루로 답해요. ') % c['fallbackFrom'] if c.get('fallbackFrom') else ''
    span = T('From %s to %s on %s' % (utc(w.get('from')), utc(w.get('to')), net), '%s~%s, %s' % (utc(w.get('from')), utc(w.get('to')), net))
    pct = lambda r: (T(' (%.2f%% of supply)', '(공급의 %.2f%%)') % r['pctOfSupply']) if r.get('pctOfSupply') is not None and abs(r['pctOfSupply']) <= 100 else ''
    bl = '; '.join('%s +%s%s' % (who(r), amount(r.get('delta')), pct(r)) for r in buy[:3])
    sl = '; '.join('%s −%s%s' % (who(r), amount(r.get('delta')), pct(r)) for r in sell[:3])
    if sell_q:
        s = span + T(', the largest net sellers of %s were %s' % (sym, sl or 'none'), ' %s 순매도 상위: %s' % (sym, sl or '없음'))
        if buy:
            s += T('. The largest net buyer was %s +%s' % (who(buy[0]), amount(buy[0].get('delta'))), '. 순매수 1위는 %s +%s' % (who(buy[0]), amount(buy[0].get('delta'))))
    else:
        s = span + T(', the largest net buyers of %s were %s' % (sym, bl or 'none'), ' %s 순매수 상위: %s' % (sym, bl or '없음'))
        if sell:
            s += T('. The largest net seller was %s −%s' % (who(sell[0]), amount(sell[0].get('delta'))), '. 순매도 1위는 %s −%s' % (who(sell[0]), amount(sell[0].get('delta'))))
    if c.get('method') == 'checkpoint_top':
        s += T('. Busy window: only the top 1,000 holders at both ends were compared', '. 거래가 많은 구간이라 양 끝의 상위 1,000 보유자만 비교했어요')
    return pre + s + T('. Exact net balance change from the token\'s transfers; pools, routers and exchanges are included and labelled.',
                       '. 토큰 이체에서 계산한 정확한 순잔고 변화예요. 풀·라우터·거래소도 포함하고 표시했어요.')


def search_market(sym, chain):
    """A <chain>:0x… market id for a symbol from the bridge search (the question's chain first), or None."""
    try:
        with urllib.request.urlopen(TOOLS.split('/tools/')[0] + '/search?' + urllib.parse.urlencode({'q': sym}), timeout=4) as r:
            rows = json.loads(r.read(400_000)).get('results') or []
    except Exception:
        return None
    ids = [x.get('id') for x in rows if isinstance(x.get('id'), str) and MARKET.fullmatch(x['id'])]
    return next((i for i in ids if i.startswith(chain + ':')), None) if chain else (ids[0] if ids else None)


def acc_window(text):
    """The window a who-is-accumulating question names (default a week)."""
    if re.search(r'\b(1h|hour|hor|huor|hr)\b|1시간|한 시간', text):   # em3
        return '1h'
    if re.search(r'\b(24h|today|day|tday|toay|tody|todya)\b|오늘|하루|24시간', text):   # em3
        return '24h'
    if re.search(r'\b(90d|quarter|3 months|90 days)\b|90일|석 달|3개월', text):   # em3
        return '90d'
    if re.search(r'\b(30d|month|monh|mnth|30 days|30 dys|30 dyas|30 dasy)\b|30일|한 ?달', text):   # em3
        return '30d'
    return '7d'


def accumulators_message(c):
    """Who gained and who shed the token in the window: exact net balance change per wallet (EMV2 card: buyers / sellers), graded
    labels, never a guess. A chain or route EMV2 does not serve yet: 'being prepared'."""
    sell_q = c.get('ask') == 'sell'
    if not c.get('available', True) or c.get('status') == 'preparing':
        return (T('Who is selling %s: %s', '누가 %s를 팔고 있나: %s') if sell_q else T('Who is accumulating %s: %s', '누가 %s를 모으고 있나: %s')) % (
            c.get('symbol'), held_reason(c) if c.get('held') or c.get('spam') else c.get('reason') or T('being prepared for this chain.', '이 체인은 준비 중이에요.'))
    return accumulators_message_em3(c, sell_q)


def _accumulators_message_codex(c):
    def who(r):
        return '%s %s%s' % ((r.get('label') or 'wallet').replace('_', ' '), short(r['address']), ' (%s)' % r['grade'] if r.get('grade') else '')
    def amt(x):
        a = abs(x or 0)
        return next(('%.2f%s' % (a / d, u) for d, u in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')) if a >= d), '%.4g' % a)
    buy, sell = c.get('buyers') or [], c.get('sellers') or []
    net = CHAIN.get(c.get('network'), c.get('network'))
    if not buy and not sell:
        return c.get('message') or 'No net %s balance changes on %s in this window.' % (c.get('symbol'), net)
    w = c.get('window') or {}
    s = ('The last %s is not ready on this chain yet, so here is the last day. ' % c['fallbackFrom']) if c.get('fallbackFrom') else ''
    s += 'From %s to %s on %s' % ((w.get('fromUtc') or '')[:16].replace('T', ' '), (w.get('toUtc') or '')[:16].replace('T', ' '), net)
    if buy:
        s += ', the largest net buyers of %s were ' % c.get('symbol') + '; '.join('%s +%s%s' % (who(r), amt(r.get('delta')), ' (%.2f%% of supply)' % r['pctOfSupply'] if r.get('pctOfSupply') is not None else '') for r in buy[:3])
    if sell:
        s += '. The largest net seller was %s −%s' % (who(sell[0]), amt(sell[0].get('delta')))
    if c.get('method') == 'checkpoint_top':
        s += '. Busy window: only the top 1,000 holders at both ends were compared'
    return s + ' (UTC). Pools, routers and exchanges are included and labelled.'

def launch_message(c):
    v=c.get('launchBuying') or {}
    if v.get('status')=='preparing':return ' Launch buying analysis is preparing; no percentages are available yet.'
    values=(v.get('pct'),v.get('suspectedPct'))
    if not all(isinstance(x,(int,float)) and not isinstance(x,bool) and 0<=x<=100 for x in values):return ''
    return (' In the first hour, %.1f%% of recorded buying was linked-group buying; %.1f%% was suspected shared-exchange-withdrawal buying. '
            'These are separate categories. Sharing an exchange withdrawal address does not establish common ownership.' % values)

def holders_message(c):
    hs = c.get('holders') or []
    if not hs:
        return 'No holder data for %s.' % c.get('symbol')
    h = hs[0]
    who = (h.get('entity') or '').replace('_', ' ') or h.get('categoryName') or 'wallet'
    return '%s has %s holders. The largest is %s %s with %.2f%% of supply%s.' % (
        c.get('symbol'), '{:,}'.format(c['holderCount']) if c.get('holderCount') else 'many', who, short(h['address']), h.get('share') or 0,
        ' — liquidity, not a single investor' if h.get('category') in ('dex_pool', 'lending') else '') + launch_line(c)


def launch_line(c):
    """First-hour buying after launch: linked groups and suspected wallets as two separate shares (bundle-summary via the bridge)."""
    lb = c.get('launchBuying') or {}
    if lb.get('status') != 'ready' or lb.get('pct') is None:
        return ''
    s = ' In the first hour after launch, %.1f%% of the buying came from %s linked group%s' % (lb['pct'], lb.get('groups') or 0, '' if lb.get('groups') == 1 else 's')
    if lb.get('suspectedWallets'):
        s += '; another %.1f%% came from %s suspected wallets that share an exchange withdrawal address (shown apart: thousands of people use the same exchange, so this alone is not evidence of one owner)' % (lb.get('suspectedPct') or 0, lb['suspectedWallets'])
    return s + '.'


def trace_message(c):
    if c.get('status')=='preparing':return c['message']
    cps = c.get('counterparties') or []
    if not cps:
        return 'No mapped counterparties for %s in the drawn galaxy; its full-map card is below.' % short(c['address'])
    top = cps[0]
    who = (top.get('entity') or '').replace('_', ' ') or top.get('categoryName') or 'a wallet'
    flow = 'received from' if top.get('direction') == 'in' else 'sent to' if top.get('direction') == 'out' else 'exchanged with'
    return '%s mostly moved money with %s %s: %s %s it%s across %s transfers. %d counterparties are listed below.' % (
        short(c['address']), who, short(top.get('address')), usd(top.get('usd')), flow,
        ' (%s)' % ', '.join(top['assets']) if top.get('assets') else '', '{:,}'.format(top.get('n') or 0), len(cps))


def pct(v):
    return '—' if not isinstance(v, (int, float)) else ('%.1f%%' % (v * 100) if abs(v) < 0.1 else '%.0f%%' % (v * 100))


def price_facts(market):
    """24h move of the market's USD token reference, straight from the console's candle API."""
    try:
        url = CONSOLE + '/api/public/engine/candles?' + urllib.parse.urlencode({'market': market, 'aggregation': 'token', 'interval': '1h', 'limit': 25})
        with urllib.request.urlopen(forwarded_request(url), timeout=8) as r:
            bars = [b for b in json.loads(r.read(2_000_000)).get('candles', []) if not b.get('synthetic')]
    except (OSError, ValueError):
        return None
    if len(bars) < 2:
        return None
    first, last = bars[0], bars[-1]
    return {'change': last['close'] / first['open'] - 1, 'high': max(b['high'] for b in bars), 'low': min(b['low'] for b in bars),
            'close': last['close'], 'volume': sum(b.get('volume') or 0 for b in bars)}


def economy_card(kind, sym, chart):
    market = (chart or {}).get('asset')
    q = {'market': market} if isinstance(market, str) and MARKET.fullmatch(market) and (chart or {}).get('symbol', '').upper().startswith(sym) else {'symbol': sym}
    e = get('economy', window='24h', **q)
    market, symbol, chain = e['market'], e['symbol'], e['network']
    lev, flows, state = e.get('leverage') or {}, e.get('flows') or {}, e.get('state') or {}
    facts, asof, overlay, title, card_extra = [], None, 'events', '', {}
    if kind == 'liquidations' or kind == 'leverage':
        d = lev.get('data') or {}
        asof = lev.get('asOf')
        if d.get('positions') or d.get('ladder'):
            near = (d.get('positions') or [])[:3]
            facts += [{'label': 'Positions borrowing against %s' % symbol, 'value': '{:,}'.format(int((d.get('totals') or {}).get('positions') or len(d.get('positions') or [])))},
                      {'label': 'Debt at risk', 'value': usd((d.get('totals') or {}).get('debtUsd'))}]
            way = lambda v: '%.1f%% %s' % (abs(v or 0), 'below' if (v or 0) < 0 else 'above')
            facts += [{'label': 'Nearest: %s on %s' % (short(p.get('wallet')), str(p.get('protocol') or '?').replace('_', ' ').title()),
                       'value': '%s USD (%s)' % ('{:,.4g}'.format(p['liqPrice']), way(p.get('distancePct')))} for p in near if isinstance(p.get('liqPrice'), (int, float))]
            overlay = 'liquidations'
            message = '%s: %d positions owe %s against it; the nearest liquidation level is %s the current price.' % (
                symbol, int((d.get('totals') or {}).get('positions') or len(near)), usd((d.get('totals') or {}).get('debtUsd')), way(near[0].get('distancePct')) if near else 'not near')
        elif d.get('borrowers'):
            b = d['borrowers']
            facts += [{'label': 'Wallets borrowing against %s' % symbol, 'value': str(len(b))},
                      {'label': 'USDG borrowed', 'value': usd(sum(x.get('debtUsd') or 0 for x in b))}]
            facts += [{'label': short(x['wallet']), 'value': '%s collateral · %s debt' % (usd(x.get('collateralUsd')), usd(x.get('debtUsd')))} for x in b[:3]]
            message = '%d wallet%s borrow %s USDG on Morpho Blue against %s. Liquidation prices need live analytics; the borrowers are below.' % (
                len(b), '' if len(b) == 1 else 's', usd(sum(x.get('debtUsd') or 0 for x in b)), symbol)
        else:
            message = 'No lending positions use %s as collateral in the current Economic Machine data.' % symbol
        title = ('Liquidation levels' if kind == 'liquidations' else 'Who is leveraged') + ' · ' + symbol
    elif kind == 'flows':
        d = flows.get('data') or {}
        asof = flows.get('asOf')
        if d.get('classes'):
            names = {'whale': 'Whales', 'retail': 'Retail wallets', 'mev': 'MEV bots', 'exchange': 'Exchanges', 'contract': 'Contracts & vaults'}
            facts = [{'label': c.get('label') or names.get(c.get('class'), c.get('class')), 'value': ('+' if (c.get('netUsd') or 0) >= 0 else '-') + usd(abs(c.get('netUsd') or 0)) + ' net'} for c in d['classes'][:6]]
            message = 'Net flows into %s over 24h by wallet class.' % symbol
        elif d.get('actions'):
            facts = [{'label': str(a.get('action', '')).replace('_', ' ').capitalize(), 'value': usd(a.get('usd'))} for a in d['actions'][:6]]
            message = 'Where %s money moved across the mapped history (a 24h class split arrives with live analytics).' % symbol
        else:
            message = 'Flows for %s are not available yet.' % symbol
        title = 'Money flow · ' + symbol
    else:   # why did X move: tie the price change to the money, numbers only
        import time as _t
        px = price_facts(market)
        now = int(_t.time())
        ev = get('events', market=market, **{'from': str(now - 86400), 'to': str(now)})
        asof = ev.get('asOf') or flows.get('asOf')
        names = {'whale': 'whales', 'retail': 'retail wallets', 'mev': 'MEV bots', 'exchange': 'exchanges', 'contract': 'contracts and vaults'}
        fd = flows.get('data') or {}
        classes = fd.get('classes') or []
        clauses = []
        if classes:
            top = max(classes, key=lambda c: abs(c.get('netUsd') or 0))
            n = top.get('netUsd') or 0
            clauses.append('%s were net %s (%s%s)' % (names.get(top.get('class'), top.get('class')), 'buyers' if n >= 0 else 'sellers', '+' if n >= 0 else '−', usd(abs(n))))
            mev = next((c for c in classes if c.get('class') == 'mev'), None)
            if mev and top.get('class') != 'mev' and (mev.get('buyUsd') or 0) + (mev.get('sellUsd') or 0) > 2 * abs(n):
                clauses.append('MEV bots churned %s' % usd((mev.get('buyUsd') or 0) + (mev.get('sellUsd') or 0)))
        lend = fd.get('lending') or {}
        tail = []
        if lend.get('events'):
            nc = lend.get('netCollateralUsd') or 0
            tail.append('%d lending event%s %s %s of collateral' % (lend['events'], '' if lend['events'] == 1 else 's', 'added' if nc >= 0 else 'withdrew', usd(abs(nc))))
        tail.append('%s was liquidated' % usd(lend['liquidatedUsd']) if (lend.get('liquidatedUsd') or 0) > 0 else 'no liquidations')
        ld = lev.get('data') or {}
        cluster = None
        price = ld.get('price') or (px or {}).get('close')
        near = [b for b in ld.get('ladder') or [] if price and b.get('price') and b['price'] >= price * 0.9 and (b.get('debtUsd') or 0) > 0]
        if near:
            cluster = max(near, key=lambda b: b.get('debtUsd') or 0)
            lo = cluster.get('priceLow') or cluster['price']
            tail.append('the nearest liquidation cluster is %s of debt between %s and %s USD (%.1f–%.1f%% below)' % (
                usd(cluster['debtUsd']), '{:,.4g}'.format(lo), '{:,.4g}'.format(cluster['price']), max(0.0, (1 - cluster['price'] / price) * 100), (1 - lo / price) * 100))
        move = ('%s %s%.1f%% in 24h' % (symbol, '+' if px['change'] >= 0 else '−', abs(px['change']) * 100)) if px else '%s over 24h' % symbol
        headline = move + (' while ' + ' and '.join(clauses) if clauses else '') + ('; ' + '; '.join(tail) if tail else '') + '.'
        big = sorted([x for x in ev.get('events') or [] if isinstance(x.get('usd'), (int, float))], key=lambda x: -x['usd'])[:3]
        if px:
            facts.append({'label': '24h range', 'value': '%s – %s USD' % ('{:,.4g}'.format(px['low']), '{:,.4g}'.format(px['high']))})
        facts += [{'label': '%s UTC · %s' % (_t.strftime('%H:%M', _t.gmtime(x['time'])), x.get('text') or x.get('kind')), 'value': usd(x['usd'])} for x in big]
        titled = {'whale': 'Whales', 'retail': 'Retail wallets', 'mev': 'MEV bots', 'exchange': 'Exchanges', 'contract': 'Contracts and vaults'}
        facts += [{'label': 'Net flow · ' + titled.get(c.get('class'), str(c.get('class'))), 'value': ('+' if (c.get('netUsd') or 0) >= 0 else '−') + usd(abs(c.get('netUsd') or 0))}
                  for c in sorted(classes, key=lambda c: -abs(c.get('netUsd') or 0))[:3]]
        if cluster:
            facts.append({'label': 'Nearest liquidation cluster', 'value': '%s at %s–%s USD' % (usd(cluster['debtUsd']), '{:,.4g}'.format(cluster.get('priceLow') or cluster['price']), '{:,.4g}'.format(cluster['price']))})
        message = headline if (classes or big or px) else 'No flow, event or price data is available for %s right now.' % symbol
        title = 'Why %s moved' % symbol
        card_extra = {'headline': headline, 'events': [{'time': x['time'], 'kind': x.get('kind'), 'usd': x['usd']} for x in big]}
    lead = next((b for b in sorted((state.get('data') or {}).get('buckets') or [], key=lambda b: -(b.get('share') or 0)) if b.get('key') != 'other'), None)
    if lead and kind not in ('flows', 'move'):
        facts.append({'label': lead.get('label'), 'value': pct(lead.get('share')) + ' of supply'})
    card = {**(card_extra if kind == 'move' else {}), 'kind': 'economy', 'title': title, 'market': market, 'symbol': symbol, 'network': chain, 'asOf': asof or state.get('asOf'),
            'facts': facts[:7], 'overlay': overlay,
            'note': 'Every value comes from the Economic Machine and Ticking candles; %s data.' % ('live' if any(x.get('status') == 'live' for x in (lev, flows, state)) else 'map-snapshot')}
    return {'state': 'wallet_card', 'message': message, 'card': card}


def coverage_reply(q):
    url=TOOLS.split('/tools/')[0]+'/wallet-coverage'
    try:
        with urllib.request.urlopen(forwarded_request(url),timeout=4) as r:value=json.loads(r.read(131073))
        if value.get('schema')!='ticking.wallet-coverage.v1' or not isinstance(value.get('chains'),list):raise ValueError('Invalid source registry')
        wanted=chain_catalog.hints(q);rows=[r for r in value['chains'] if r.get('id') in chain_catalog.evm_ids() and (not wanted or r.get('id') in wanted)]
        lines=[]
        for row in rows:
            cfg=row.get('configured') or {}
            lines.append('- **'+row['name']+'**: '+str(row.get('reviewedCharts',0))+' reviewed charts; '+('indexed wallet reads' if row.get('hasData') and cfg.get('econ') else 'wallet balances not connected')+('; graph paths' if row.get('hasData') and cfg.get('graph') else '; graph paths not configured')+'.')
        return {'state':'message','message':'Connected data sources\n\n'+'\n'.join(lines)+'\n\nA specific address must exist in that source index. Charts preserve chain and contract identity. Solana pool charts and native BTC movement/reference data remain separate from these EVM wallet stores. Transfers and common services do not prove shared ownership.'}
    except (OSError,ValueError,TypeError,KeyError):return {'state':'message','message':'The connected-source registry is temporarily unavailable. Name the exact chain and public address; no alternate network was substituted.'}


def resolve(q, chart):
    """Return a resolver result for wallet questions, or None to fall through to the Ticking routing."""
    if re.search(r"(?i)\b(?:do not|don't|never)\s+(?:show|open|read|fetch|query)\s+(?:the\s+)?(?:holders?|graph|chart|wallets?)\b|(?:보유자|그래프|차트).{0,8}(?:보여주지|열지|조회하지)",q):
        return {'state':'clarify','message':'That read was excluded by your request. Which remaining action should I run?'}
    context=investigation.subject();selection=investigation.selection()
    if context.get('kind')=='wallet' and not ADDR.search(q):q=q+' '+context['address']+(' on '+context['chain'] if context.get('chain') else '')
    if context.get('kind')=='token' and context.get('asset') and not (chart or {}).get('asset'):
        chart={**(chart or {}),'asset':context['asset'],'symbol':context.get('symbol') or ''}
    text = q.lower()
    addrs = list(dict.fromkeys(a.lower() for a in ADDR.findall(q)))
    networks = chain_catalog.hints(q)
    if len(networks)>1:return {'state':'clarify','message':'Choose one exact chain for this wallet request.'}
    chain = networks[0] if networks else context.get('chain')
    if len(addrs)==1 and not context.get('kind'):
        investigation.CURRENT.set({'schema':investigation.SCHEMA,'subject':{'kind':'wallet','address':addrs[0],**({'chain':chain} if chain else {})},'selection':selection})
    ctx = re.sub(r'[^A-Z0-9]', '', (chart or {}).get('symbol', '').upper()) or None
    _L.ko = bool(re.search('[가-힣]', q))
    try:
        if not addrs and re.search(r'(?i)what (?:data|tools|can you)|(?:supported|which|available) (?:chains|networks|tools)|사용.*기능|(?:어느|어떤|지원).*체인|체인.*(?:지원|데이터.*(?:볼|쓸|사용))',q):return coverage_reply(q)
        em3 = em3_resolve(q, chart, ctx, addrs, chain)   # EM layer em3
        if em3 == 'map':
            return None   # a wallet map: Ticking's navigation
        if em3 is not None:
            return em3
        if accumulate_requested(q):   # EM layer: tools/accumulators (EMV2)
            return accumulate_card(q, chart, ctx, text, addrs)
        if len(addrs) > 1:
            return {'state': 'clarify', 'message': 'Ask about one wallet at a time.'}
        if addrs and re.search(r'\b(chart|candles?|price|token|terminal|ohlc)\b|차트|캔들|가격|터미널', text) and not re.search(r'\b(wallet|holder|trace|flows?|portfolio|holdings)\b|지갑|보유|포트폴리오', text):
            return None   # a token-address chart request; Ticking's native routing opens the network market
        if addrs:
            if any(k in selection for k in ('block','time','from','to')) or selection.get('window') not in (None,'all'):
                return {'state':'wallet_card','message':'This wallet reader cannot prove the selected historical point. No current balances or transactions were substituted.','card':{'kind':'wallet','address':addrs[0],'chain':chain,'available':False,'reason':'historical_wallet_point_unavailable'}}
            if not chain:return chain_catalog.wallet_choices(addrs[0])
            if re.search(r'\b(trace|transactions?|transfers?|trades?|activity|flows?|where|counterpart\w*|sent|received|went|go|goes|move[ds]?)\b|추적|거래|이체|활동|자금.*(?:출처|경로)', text):
                card = investigation.validate_evidence(get('trace', address=addrs[0], chain=chain),address=addrs[0],chain=chain)
                if card.get('chain')!=chain:raise LookupError('The returned wallet evidence does not match the requested chain. No other network was substituted.')
                return {'state': 'wallet_card', 'message': trace_message(card), 'card': card}
            card = investigation.validate_evidence(get('wallet', address=addrs[0], chain=chain),address=addrs[0],chain=chain)
            if card.get('chain')!=chain:raise LookupError('The returned wallet evidence does not match the requested chain. No other network was substituted.')
            return {'state': 'wallet_card', 'message': wallet_message(card), 'card': card}
        econ = ('liquidations' if re.search(r'\bliquidat\w*', text) else 'leverage' if re.search(r'\b(who is leveraged|leveraged|leverage on)\b', text)
                else 'flows' if re.search(r'\b(money flows?|net flows?|inflows?|outflows?|flow of money|flows? for)\b', text)
                else 'move' if re.search(r'\bwhy\b', text) and re.search(r'\b(move[ds]?|moving|pump\w*|dump\w*|drop\w*|fell|fall\w*|rise|rose|up|down|crash\w*|rall\w*)\b', text) else None)
        if econ:
            syms = [s for s in symbols(q) if s not in STABLE] or ([ctx] if ctx and ctx not in STABLE else [])
            if not syms:
                return {'state': 'clarify', 'message': 'Which token? Try “liquidation levels for NVDA” or open an Arbitrum One or Robinhood Chain market first.'}
            if chain and (not chart or not chain_catalog.market(chart.get('asset')) or chart['asset'].split(':')[0]!=chain):return {'state':'clarify','message':'Choose the exact token market on '+chain_catalog.names()[chain]+' for this evidence request. No other network was substituted.'}
            return economy_card(econ, syms[0], chart)
        if re.search(r'\b(borrow\w*|debt|loans?|leverag\w*)\b', text) and re.search(r'\b(usdg|stock|collateral|against)\b', text):
            syms = [s for s in symbols(q) if s not in STABLE] or ([ctx] if ctx and ctx not in STABLE else [])
            sym = syms[0] if syms else ''
            card = get('top', symbol=sym)
            return {'state': 'wallet_card', 'message': top_message(card, sym), 'card': card}
        if re.search(r'\b(holders?|who holds|whales?|top wallets|largest wallets|who owns|owners?)\b', text):
            syms = [s for s in symbols(q) if s not in STABLE] or ([ctx] if ctx else [])
            if not syms:
                return {'state': 'clarify', 'message': 'Which token? Try “NVDA holders”, or open a Robinhood Chain or Arbitrum market first.'}
            market = (chart or {}).get('asset')
            if not (isinstance(market, str) and MARKET.fullmatch(market) and syms[0] == ctx):
                market = None
            try:
                card = get('holders', **({'market': market} if market else {'symbol': syms[0], 'chain':chain}))
            except LookupError as e:
                return {'state': 'message', 'message': str(e) if 'Unknown market' not in str(e) else 'Holders are mapped for reviewed Arbitrum One and Robinhood Chain markets.'}
            if chain and card.get('chain',card.get('network'))!=chain:raise LookupError('Holder evidence for the requested chain is not available. No other network was substituted.')
            return {'state': 'wallet_card', 'message': holders_message(card)+launch_message(card), 'card': card}
    except LookupError as e:
        return {'state': 'message', 'message': str(e)[:300],**({'selection':{'chain':chain,'address':addrs[0]}} if chain and addrs else {})}
    return None
