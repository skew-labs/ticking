"""Pinned Raydium, PumpSwap and Meteora DLMM executed-flow decoders.
Vault flow includes pool fees: the pool exchange rate is not wallet net output.
"""
from decimal import Decimal
from server import b58decode
from dex_adapters import EXTRA_PROGRAMS,swap_instruction,exclusive_vault_scope
AMM_V4='675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8'
CPMM='CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C'
CLMM='CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK'
PUMP='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
PROGRAMS={AMM_V4:'Raydium AMM v4',CPMM:'Raydium CPMM',CLMM:'Raydium CLMM',PUMP:'Pump bonding curve',**EXTRA_PROGRAMS}
SOL='So11111111111111111111111111111111111111112'
def pool_config(item,mint):
    if item.get('programId') not in PROGRAMS:raise ValueError('This pool adapter is not connected yet')
    a,b=item['mintA'],item['mintB']
    if mint not in (a['address'],b['address']):raise ValueError('Pool mint identity mismatch')
    side='A' if a['address']==mint else 'B';base,quote=(a,b) if side=='A' else (b,a)
    return {'pool':item['id'],'program':item['programId'],'mint':mint,'quoteMint':quote['address'],'quote':'SOL' if quote['address']==SOL else quote.get('symbol') or quote['address'][:6],'baseDecimals':base['decimals'],'quoteDecimals':quote['decimals'],'baseVault':item['vault'][side],'quoteVault':item['vault']['B' if side=='A' else 'A']}
def decode(tx,config,signature,transaction_index=0):
    if config['program']==PUMP:
        from pump_curve import decode as curve_decode
        return curve_decode(tx,config,signature,transaction_index)
    if not tx or tx.get('meta') is None:return [],'unavailable'
    meta=tx['meta']
    if 'err' not in meta:return [],'execution_status_missing'
    if meta['err'] is not None:return [],'failed'
    keys=[k['pubkey'] if isinstance(k,dict) else k for k in tx['transaction']['message']['accountKeys']]
    instructions=list(tx['transaction']['message']['instructions'])
    for block in meta.get('innerInstructions') or []:instructions.extend(block['instructions'])
    matching=[]
    for instruction in instructions:
        program=instruction.get('programId')
        if program is None and instruction.get('programIdIndex') is not None:program=keys[instruction['programIdIndex']]
        accounts=[keys[a] if isinstance(a,int) else a for a in instruction.get('accounts',[])]
        if program==config['program'] and config['pool'] in accounts:
            try:
                raw=instruction.get('data','');data=raw if isinstance(raw,bytes) else b58decode(raw)
            except (ValueError,TypeError):return [],'invalid_instruction'
            matching.append((data,accounts))
    if len(matching)!=1:return [],'unsupported_or_ambiguous'
    data,accounts=matching[0]
    if config['program']==AMM_V4:
        if len(data)!=17 or data[0] not in (9,11,16,17):return [],'invalid_swap_instruction'
    elif config['program']==CPMM:
        import hashlib
        allowed={hashlib.sha256(('global:'+n).encode()).digest()[:8] for n in ('swap_base_input','swap_base_output')}
        if len(data)!=24 or data[:8] not in allowed:return [],'invalid_swap_instruction'
    elif config['program']==CLMM:
        import hashlib
        allowed={hashlib.sha256(('global:'+n).encode()).digest()[:8] for n in ('swap','swap_v2')}
        if len(data)!=41 or data[:8] not in allowed or data[-1]>1:return [],'invalid_swap_instruction'
    elif config['program'] in EXTRA_PROGRAMS:
        if not swap_instruction(config['program'],data,accounts,config):return [],'invalid_swap_instruction'
        if not exclusive_vault_scope(tx,config,keys):return [],'ambiguous_vault_execution_scope'
    else:return [],'unsupported_program'
    balances=[]
    for vault,mint,decimals in [(config['baseVault'],config['mint'],config['baseDecimals']),(config['quoteVault'],config['quoteMint'],config['quoteDecimals'])]:
        if vault not in keys:return [],'vault_missing'
        index=keys.index(vault);pair=[]
        for name in ['preTokenBalances','postTokenBalances']:
            candidates=[b for b in meta.get(name,[]) if b['accountIndex']==index and b['mint']==mint and b['uiTokenAmount']['decimals']==decimals]
            if len(candidates)!=1:return [],'balance_identity_mismatch'
            raw=candidates[0]['uiTokenAmount']['amount']
            if not isinstance(raw,str) or not raw.isascii() or not raw.isdigit() or not 0<=int(raw)<2**64:return [],'invalid_token_amount'
            pair.append(Decimal(raw)/(10**decimals))
        balances.append(pair[1]-pair[0])
    base,quote=balances
    if base==0 or quote==0 or base*quote>=0:return [],'not_opposite_swap_flow'
    if tx.get('blockTime') is None:return [],'block_time_unavailable'
    return [{'id':signature+':'+config['pool'],'time':int(tx['blockTime']),'slot':int(tx['slot']),'order':transaction_index,'series':'trade','price':float(abs(quote/base)),'volume':float(abs(quote)),'baseVolume':float(abs(base))}],'decoded'
