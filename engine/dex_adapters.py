"""Pinned execution/account layouts for additional Solana swap programs.

Only a single recognized invocation with the account positions from the official
IDL is eligible for transaction-level vault flow. Route intents, event-like log
strings, deposits, repeated-pool swaps and unverified mint units are not fills.
Source revisions and original IDLs are retained in evidence/dex-sources.
"""
import hashlib,struct
from functools import lru_cache
from server import b58decode,valid_mint

PUMPSWAP='pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
DLMM='LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'
WHIRLPOOL='whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc'
EXTRA_PROGRAMS={PUMPSWAP:'PumpSwap',DLMM:'Meteora DLMM',WHIRLPOOL:'Orca Whirlpool'}
SOURCE_REVISIONS={PUMPSWAP:'8cda1fa30ea658b20909d8aedf002047119388d2',
                  DLMM:'2f540c2107988e0f97efa5917100bb912c96c17a',
                  WHIRLPOOL:'f2a3d13fa04eb15cf5b5a309ef9b226fd5d34e36'}
@lru_cache(maxsize=32)
def discriminator(name):return hashlib.sha256(('global:'+name).encode()).digest()[:8]

def swap_instruction(program,data,accounts,cfg):
    """Return False on future layouts, even if their discriminator is familiar."""
    if program==WHIRLPOOL:
        if len(data)<42 or data[40]>1 or data[41]>1:return False
        if data[:8]==discriminator('swap'):
            if len(data)!=42 or len(accounts)<11 or accounts[2]!=cfg['pool']:return False
            return (accounts[4],accounts[6]) in ((cfg['baseVault'],cfg['quoteVault']),(cfg['quoteVault'],cfg['baseVault']))
        if data[:8]!=discriminator('swap_v2') or len(accounts)<15 or accounts[4]!=cfg['pool'] or len(data)<43 or data[42]>1:return False
        if data[42]==0:
            if len(data)!=43:return False
        else:
            if len(data)<47:return False
            n=struct.unpack_from('<I',data,43)[0]
            if n>32 or len(data)!=47+2*n:return False
            used=set();remaining=0
            for at in range(47,len(data),2):
                kind,length=data[at:at+2]
                if kind not in (0,1,6) or kind==6 and length>3:return False
                if length:
                    if kind in used:return False
                    used.add(kind);remaining+=length
            if remaining>len(accounts)-15:return False
        return (accounts[5],accounts[8],accounts[6],accounts[10]) in ((cfg['mint'],cfg['baseVault'],cfg['quoteMint'],cfg['quoteVault']),(cfg['quoteMint'],cfg['quoteVault'],cfg['mint'],cfg['baseVault']))
    if program==PUMPSWAP:
        plain=('sell','buy_v2','sell_v2','buy_exact_quote_in_v2')
        optional=('buy','buy_exact_quote_in')
        if data[:8] in {discriminator(n) for n in plain}:
            valid=len(data)==24
        elif data[:8] in {discriminator(n) for n in optional}:
            # Old instructions predate track_volume; OptionBool is a u8 bool,
            # never an arbitrary suffix. All remaining accounts are addresses.
            valid=len(data) in (24,25) and (len(data)==24 or data[24]<=1)
        else:return False
        pool,mint_a,mint_b,vault_a,vault_b=0,3,4,7,8
    elif program==DLMM:
        fixed=('swap','swap_exact_out')
        if data[:8] in {discriminator(n) for n in fixed}:valid=len(data)==24
        elif data[:8]==discriminator('swap_with_price_impact'):
            valid=(len(data)==19 and data[16]==0) or (len(data)==23 and data[16]==1)
        elif data[:8] in {discriminator(n) for n in ('swap2','swap_exact_out2','swap_with_price_impact2')}:
            # RemainingAccountsInfo is a bounded Borsh vector of (enum,u8).
            if data[:8]==discriminator('swap_with_price_impact2'):
                if len(data)<17 or data[16]>1:return False
                at=19+4*data[16]
            else:at=24
            if len(data)<at+4:return False
            n=struct.unpack_from('<I',data,at)[0];at+=4
            if n>32:return False
            for _ in range(n):
                if at>=len(data) or data[at]>4:return False
                kind=data[at];at+=1
                if kind==3:at+=1
                if at>=len(data) or data[at]==0:return False
                at+=1
            valid=at==len(data)
        else:return False
        pool,mint_a,mint_b,vault_a,vault_b=0,6,7,2,3
    else:return False
    if not valid or len(accounts)<=max(pool,mint_a,mint_b,vault_a,vault_b) or accounts[pool]!=cfg['pool']:return False
    pair=(accounts[mint_a],accounts[vault_a],accounts[mint_b],accounts[vault_b])
    return pair in ((cfg['mint'],cfg['baseVault'],cfg['quoteMint'],cfg['quoteVault']),
                    (cfg['quoteMint'],cfg['quoteVault'],cfg['mint'],cfg['baseVault']))

def pool_identity(address,owner,data):
    """Account-derived mints/vaults; decimals require separately verified mints."""
    from solana_stream import b58encode
    if not valid_mint(address):raise ValueError('Invalid pool address')
    def key(at):
        raw=data[at:at+32]
        if len(raw)!=32 or raw==bytes(32):raise ValueError('Incomplete pool identity')
        return b58encode(raw)
    if owner==PUMPSWAP:
        if not 211<=len(data)<=512 or data[:8]!=hashlib.sha256(b'account:Pool').digest()[:8]:raise ValueError('Unknown PumpSwap pool account')
        return key(43),key(75),key(139),key(171)
    if owner==DLMM:
        if len(data)!=904 or data[:8]!=hashlib.sha256(b'account:LbPair').digest()[:8]:raise ValueError('Unknown DLMM pair account')
        if data[75]>3 or data[82]>1 or data[86]>1 or not struct.unpack_from('<H',data,80)[0] or data[880]>1 or data[881]>1:raise ValueError('Uninitialized or unsupported DLMM pair')
        return key(88),key(120),key(152),key(184)
    if owner==WHIRLPOOL:
        if len(data)!=653 or data[:8]!=hashlib.sha256(b'account:Whirlpool').digest()[:8]:raise ValueError('Unknown Whirlpool pool account')
        if not struct.unpack_from('<H',data,41)[0] or data[65:81]==bytes(16):raise ValueError('Uninitialized Whirlpool pool')
        return key(101),key(181),key(133),key(213)
    raise ValueError('Unsupported pool layout')

def mint_units(address,accounts):
    import base64
    from solana_history import mint_account
    value=(accounts or {}).get(address)
    if not value or value.get('executable') or value.get('commitment')!='finalized' or not isinstance(value.get('slot'),int) or value['slot']<=0:raise ValueError('Finalized mint units required')
    raw=base64.b64decode(value['data'],validate=True)
    return mint_account(value['owner'],raw)['decimals']

def exclusive_vault_scope(tx,cfg,keys):
    """Reject vault mutations outside the one authenticated DEX invocation.

    CPI stack heights are execution metadata, not inferred from log strings.
    This permits aggregators and multi-hop across different pools, while a
    direct donation, withdrawal or another invocation cannot pollute the price.
    """
    token_programs={'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA','TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}
    groups={}
    for group in tx['meta'].get('innerInstructions') or []:
        if group['index'] in groups:return False
        groups[group['index']]=group['instructions']
    outer=tx['transaction']['message']['instructions']
    if any(not isinstance(i,int) or not 0<=i<len(outer) for i in groups):return False
    transfers=0
    for index,top in enumerate(outer):
        stack=[]
        for at,ix in enumerate([top,*groups.get(index,[])]):
            height=1 if at==0 else ix.get('stackHeight')
            if not isinstance(height,int) or not 1<=height<=16 or at and height<2:return False
            while stack and stack[-1][0]>=height:stack.pop()
            if at and (not stack or height>stack[-1][0]+1):return False
            program=ix.get('programId') or keys[ix['programIdIndex']]
            accounts=[keys[i] if isinstance(i,int) else i for i in ix.get('accounts',[])]
            parsed=ix.get('parsed')
            # jsonParsed Memo instructions contain a string, not an SPL
            # instruction object. They remain stack entries but cannot name
            # token vault transfers. Malformed token parsing fails closed.
            if isinstance(parsed,dict):
                details=parsed.get('info',{})
                if not isinstance(details,dict):return False
                accounts=[details.get('source'),details.get('destination')]
            elif parsed is not None and program in token_programs:return False
            if program in token_programs and {cfg['baseVault'],cfg['quoteVault']}.intersection(accounts):
                if ix.get('parsed'):
                    # jsonParsed receipts retain actual executed transfer
                    # amounts and stack heights. They do not include wire data.
                    parsed=ix['parsed'];info=parsed.get('info',{});kind=parsed.get('type')
                    if kind not in ('transfer','transferChecked','transferCheckedWithFee') or not any(target for _,target in stack) or not {cfg['baseVault'],cfg['quoteVault']}.intersection((info.get('source'),info.get('destination'))):return False
                    transfers+=1;stack.append((height,False));continue
                raw=ix.get('data','');data=raw if isinstance(raw,bytes) else b58decode(raw)
                kind=data[0] if data else -1
                if kind==3:valid=len(data)==9 and len(accounts)>=3;dest=1
                elif kind==12:valid=len(data)==10 and len(accounts)>=4;dest=2
                elif kind==26:valid=len(data)==19 and data[1]==1 and len(accounts)>=4;dest=2
                else:return False
                if not valid or not any(target for _,target in stack) or not {cfg['baseVault'],cfg['quoteVault']}.intersection((accounts[0],accounts[dest])):return False
                transfers+=1
            target=program==cfg['program'] and cfg['pool'] in accounts
            stack.append((height,target))
    return transfers>=2
