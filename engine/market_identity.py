"""Untrusted display metadata eligibility; mint/pool verification is separate."""
import re

def retained_identity(mint,name,symbol,source):
    if not isinstance(mint,str) or not name or not symbol:return None
    if str(name).strip()==mint or str(symbol).strip()==mint:return None
    # A launchpad address suffix is not proof of issuance or program identity.
    # Strip controls, including bidi formatting, without rewriting the mint.
    clean=lambda s,n:re.sub(r'[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]','',str(s)).strip()[:n]
    name,symbol=clean(name,100),clean(symbol,20)
    if not name or not symbol or name==mint or symbol==mint:return None
    if re.search(r'\btest\b|placeholder|lp token|\bstock\b|\bequity\b',name+' '+symbol,re.I):return None
    return dict(id=None,name=name,symbol=symbol,platforms={'solana':mint},identity_source='retained_'+str(source))

def display_identities(pairs,requested):
    """Public names are hints only; never import their price, pool, or activity."""
    if not isinstance(pairs,list) or len(pairs)>3000:raise ValueError('Bounded pair list required')
    wanted=set(requested);out={}
    for pair in pairs:
        if not isinstance(pair,dict) or pair.get('chainId')!='solana':continue
        for side in ('baseToken','quoteToken'):
            token=pair.get(side)
            if not isinstance(token,dict):continue
            mint=token.get('address')
            if mint not in wanted or mint in out:continue
            row=retained_identity(mint,token.get('name'),token.get('symbol'),'dexscreener_display_hint')
            if row:out[mint]=row
    return out
