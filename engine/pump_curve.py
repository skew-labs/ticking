"""Executed Pump curve legs, tied to finalized curve/mint identities.

The program's TradeEvent supplies the gross curve exchange amount. Balance
changes corroborate token quantity; wallet cash, tips and fees are not prices.
Unrecognized layouts, repeated mint legs and truncated events fail closed.
"""
import base64,hashlib,json,math,re,struct
from pathlib import Path
from server import b58decode

PUMP='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
SOL='So11111111111111111111111111111111111111112'
ATA='ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
LAYOUT=json.loads(Path(__file__).with_name('pump-curve-layout.json').read_text())
DEFS={bytes(i['discriminator']):i for i in LAYOUT['instructions']}
EVENT=bytes(LAYOUT['events'][0]['discriminator'])
EVENT_CPI=bytes.fromhex('e445a52e51cb9a1d')

def on_curve(raw):
    # Solana PDA derivation rejects compressed Edwards points. This is not a
    # signature verifier; test it against the existing official SDK implementation.
    p=2**255-19;y=int.from_bytes(raw,'little')&((1<<255)-1)
    if y>=p:return False
    d=-121665*pow(121666,p-2,p)%p;x2=(y*y-1)*pow(d*y*y+1,p-2,p)%p
    x=pow(x2,(p+3)//8,p)
    if x*x%p!=x2:x=x*pow(2,(p-1)//4,p)%p
    return x*x%p==x2
def pda(program,*seeds):
    from solana_stream import b58encode
    if any(len(s)>32 for s in seeds) or len(seeds)>15:raise ValueError('PDA seed bound')
    for bump in range(255,-1,-1):
        raw=hashlib.sha256(b''.join((*seeds,bytes([bump])))+b58decode(program)+b'ProgramDerivedAddress').digest()
        if not on_curve(raw):return b58encode(raw)
    raise ValueError('No PDA')
def curve_address(mint):return pda(PUMP,b'bonding-curve',b58decode(mint))
def token_account(owner,mint,program):return pda(ATA,b58decode(owner),b58decode(program),b58decode(mint))

def account_config(address,owner,data,mint_accounts):
    from dex_adapters import mint_units
    from solana_stream import b58encode
    if owner!=PUMP or (len(data) not in {49,81,82,83} and not 115<=len(data)<=4096) or data[:8]!=hashlib.sha256(b'account:BondingCurve').digest()[:8] or data[48]>1:raise ValueError('Unknown curve account layout')
    if len(data)>=83 and (data[81]>1 or data[82]>1):raise ValueError('Unknown curve flags')
    if not mint_accounts or len(mint_accounts)!=2:raise ValueError('Curve mint dependencies required')
    base=next((m for m in mint_accounts if curve_address(m)==address),None)
    if not base:raise ValueError('Curve PDA does not belong to the requested mint')
    quote=SOL if len(data)<115 or data[83:115]==bytes(32) else b58encode(data[83:115])
    if quote not in mint_accounts or quote==base:raise ValueError('Unverified curve quote identity')
    bd,qd=mint_units(base,mint_accounts),mint_units(quote,mint_accounts)
    vault=token_account(address,base,mint_accounts[base]['owner'])
    qvault=address if quote==SOL else token_account(address,quote,mint_accounts[quote]['owner'])
    return {'pool':address,'program':PUMP,'mint':base,'quoteMint':quote,'baseVault':vault,'quoteVault':qvault,
            'baseDecimals':bd,'quoteDecimals':qd,'quote':'SOL' if quote==SOL else quote,'venue':'Pump bonding curve'}

def parse_event(raw):
    if len(raw)>8192 or raw[:8]!=EVENT:raise ValueError('Unrecognized curve event')
    at=8
    def field(kind):
        nonlocal at
        from solana_stream import b58encode
        if kind=='pubkey':
            value=raw[at:at+32];at+=32
            if len(value)!=32:raise ValueError('Torn event key')
            return b58encode(value)
        if kind=='bool':
            if at>=len(raw) or raw[at]>1:raise ValueError('Torn event bool')
            value=bool(raw[at]);at+=1;return value
        if isinstance(kind,str) and kind in {'u64','i64','u16','u8'}:
            fmt={'u64':'<Q','i64':'<q','u16':'<H','u8':'<B'}[kind];value=struct.unpack_from(fmt,raw,at)[0];at+=struct.calcsize(fmt);return value
        if kind=='string':
            n=struct.unpack_from('<I',raw,at)[0];at+=4
            if n>64 or at+n>len(raw):raise ValueError('Event string bound')
            value=raw[at:at+n].decode('utf-8');at+=n;return value
        if isinstance(kind,dict) and 'vec' in kind:
            n=struct.unpack_from('<I',raw,at)[0];at+=4
            if n>16:raise ValueError('Event vector bound')
            return [field(kind['vec']) for _ in range(n)]
        if isinstance(kind,dict) and 'defined' in kind:
            return {f['name']:field(f['type']) for f in LAYOUT['types'][kind['defined']['name']]['fields']}
        raise ValueError('Unsupported event field')
    result={}
    for f in LAYOUT['types']['TradeEvent']['fields']:
        if at==len(raw):
            # Earlier official SOL-only event versions ended at field boundaries.
            # Modern quote_mint is required for all non-SOL configurations.
            break
        result[f['name']]=field(f['type'])
    if at!=len(raw) or not {'mint','sol_amount','token_amount','is_buy','user','timestamp','real_token_reserves'}<=result.keys():raise ValueError('Torn curve trade event')
    return result

def decode(tx,cfg,signature,transaction_index=0):
    try:
        meta=tx.get('meta')
        if not meta or 'err' not in meta or meta['err'] is not None:return [],'failed_or_missing_result'
        if type(tx.get('blockTime'))!=int or tx['blockTime']<=0:return [],'missing_chain_time'
        keys=[k['pubkey'] if isinstance(k,dict) else k for k in tx['transaction']['message']['accountKeys']]
        items=[*tx['transaction']['message']['instructions'],*(i for g in meta.get('innerInstructions') or [] for i in g['instructions'])]
        swaps=[]
        for i in items:
            program=i.get('programId') or keys[i['programIdIndex']]
            if program!=PUMP:continue
            data=i.get('data',b'');data=data if isinstance(data,bytes) else b58decode(data)
            definition=DEFS.get(data[:8])
            if not definition:continue
            accounts=[keys[a] if isinstance(a,int) else a for a in i.get('accounts',[])]
            if len(accounts)<len(definition['accounts']):continue
            named=dict(zip(definition['accounts'],accounts));mint=named.get('base_mint',named.get('mint'))
            if mint!=cfg['mint'] or named.get('bonding_curve')!=cfg['pool']:continue
            if named.get('quote_mint',SOL)!=cfg['quoteMint']:return [],'curve_quote_mismatch'
            swaps.append((definition['name'],named))
        if len(swaps)!=1:return [],'ambiguous_curve_leg'
        name,accounts=swaps[0];stack=[];events=[]
        for line in meta.get('logMessages') or []:
            invoke=re.fullmatch(r'Program (\w+) invoke \[(\d+)\]',line)
            if invoke:
                if int(invoke[2])!=len(stack)+1:return [],'invalid_log_stack'
                stack.append(invoke[1]);continue
            finish=re.fullmatch(r'Program (\w+) (success|failed:.*)',line)
            if finish:
                if not stack or stack.pop()!=finish[1] or finish[2]!='success':return [],'invalid_log_completion'
                continue
            if line.startswith('Program data: ') and stack and stack[-1]==PUMP:
                raw=base64.b64decode(line[14:],validate=True)
                if raw[:8]==EVENT:
                    event=parse_event(raw)
                    if event['mint']==cfg['mint']:events.append(event)
        # Anchor self-CPI events carry the same official event bytes. Successful
        # execution authenticates the program's canonical event-authority PDA.
        # Do not let a caller-supplied top-level instruction impersonate it.
        cpi_events=[];authority=pda(PUMP,b'__event_authority')
        for group in meta.get('innerInstructions') or []:
            for i in group['instructions']:
                program=i.get('programId') or keys[i['programIdIndex']]
                if program!=PUMP:continue
                data=i.get('data',b'');data=data if isinstance(data,bytes) else b58decode(data)
                if data[:16]!=EVENT_CPI+EVENT:continue
                cpi_accounts=[keys[a] if isinstance(a,int) else a for a in i.get('accounts',[])]
                if cpi_accounts!=[authority] or type(i.get('stackHeight'))!=int or i['stackHeight']<2:return [],'invalid_curve_event_authority'
                event=parse_event(data[8:])
                if event['mint']==cfg['mint']:cpi_events.append(event)
        if len(events)>1 or len(cpi_events)>1:return [],'missing_or_ambiguous_curve_event'
        if events and cpi_events and events[0]!=cpi_events[0]:return [],'conflicting_curve_events'
        if not cpi_events and stack:return [],'missing_or_ambiguous_curve_event'
        selected=events or cpi_events
        if not selected:return [],'missing_or_ambiguous_curve_event'
        event=selected[0]
        if event['user']!=accounts['user'] or event['is_buy']!=name.startswith('buy') or abs(event['timestamp']-tx['blockTime'])>2:return [],'curve_event_identity_mismatch'
        if cfg['quoteMint']!=SOL:
            if event.get('quote_mint')!=cfg['quoteMint'] or 'quote_amount' not in event:return [],'unknown_quote_event_layout'
            amount=event['quote_amount']
        else:
            if event.get('quote_mint',SOL) not in {SOL,'11111111111111111111111111111111'}:return [],'curve_event_quote_mismatch'
            amount=event.get('quote_amount') or event['sol_amount']
        # A mint/create + buy changes total vault inventory for another reason;
        # it is excluded until an instruction-scoped decoder is available.
        index=keys.index(cfg['baseVault']);balances=[]
        for field_name in ('preTokenBalances','postTokenBalances'):
            rows=[r for r in meta.get(field_name,[]) if r['accountIndex']==index and r['mint']==cfg['mint'] and r['uiTokenAmount']['decimals']==cfg['baseDecimals']]
            if len(rows)!=1:return [],'curve_vault_balance_missing'
            value=rows[0]['uiTokenAmount']['amount']
            if not isinstance(value,str) or not value.isdigit() or int(value)>=2**64:return [],'invalid_curve_token_amount'
            balances.append(int(value))
        if balances[1]-balances[0]!=(-1 if event['is_buy'] else 1)*event['token_amount']:return [],'curve_token_amount_mismatch'
        base=event['token_amount']/10**cfg['baseDecimals'];quote=amount/10**cfg['quoteDecimals']
        if not base>0 or not quote>0 or not math.isfinite(quote/base):return [],'invalid_curve_price'
        return [{'id':signature+':'+cfg['pool'],'time':tx['blockTime'],'slot':int(tx['slot']),'order':transaction_index,
                 'series':'trade','price':quote/base,'volume':quote,'baseVolume':base}],'decoded'
    except (ValueError,TypeError,KeyError,IndexError,struct.error):return [],'unrecognized_curve_result'
