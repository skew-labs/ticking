"""Verify Privy proofs against this app's public JWKS; never trust browser profiles."""
import json
import os
import threading
import time
import httpx
import jwt
from fastapi import HTTPException

_lock = threading.Lock()
_cache = {'app': None, 'at': 0, 'keys': {}}

def app_id():
    return os.environ.get('TICKING_PRIVY_APP_ID', '')

def signing_key(kid):
    app = app_id()
    if not app: raise HTTPException(503, 'Social login is not configured yet.')
    with _lock:
        age = time.monotonic() - _cache['at']
        if _cache['app'] != app or age > 3600 or (kid not in _cache['keys'] and age > 30):
            try:
                r = httpx.get(f'https://auth.privy.io/api/v1/apps/{app}/jwks.json', timeout=8)
                r.raise_for_status()
                keys = {k['kid']: jwt.PyJWK.from_dict(k).key for k in r.json()['keys']
                        if k.get('kty') == 'EC' and k.get('crv') == 'P-256' and k.get('alg', 'ES256') == 'ES256'}
                if not keys: raise ValueError('Empty key set')
            except Exception:
                raise HTTPException(503, 'Login verification is temporarily unavailable. Please retry.') from None
            _cache.update(app=app, at=time.monotonic(), keys=keys)
        if kid not in _cache['keys']: raise HTTPException(401, 'Login proof is invalid. Please sign in again.')
        return _cache['keys'][kid]

def decode(token):
    try:
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'ES256' or not isinstance(header.get('kid'), str): raise ValueError()
        claims = jwt.decode(token, signing_key(header['kid']), algorithms=['ES256'],
                            audience=app_id(), issuer='privy.io', leeway=5,
                            options={'require': ['sub', 'iss', 'aud', 'iat', 'exp']})
        if not isinstance(claims['sub'], str) or not claims['sub'].startswith('did:privy:') or len(claims['sub']) > 200:
            raise ValueError()
        return claims
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, 'Login proof is invalid or expired. Please sign in again.') from None

def verify(access_token, identity_token):
    access, identity = decode(access_token), decode(identity_token)
    if not isinstance(access.get('sid'), str) or not access['sid'] or 'linked_accounts' in access or access['sub'] != identity['sub']:
        raise HTTPException(401, 'Login proofs do not match. Please sign in again.')
    try:
        accounts = json.loads(identity['linked_accounts'])
        if not isinstance(accounts, list): raise ValueError()
        profile = []
        for a in accounts:
            kind = a.get('type')
            if kind == 'google_oauth' and a.get('email'):
                profile.append({'type': 'google', 'label': str(a['email'])[:180], 'name': str(a.get('name') or '')[:60]})
            elif kind == 'wallet' and a.get('address') and a.get('wallet_client_type') != 'privy':
                profile.append({'type': 'wallet', 'label': str(a['address'])[:100], 'chain': str(a.get('chain_type', ''))[:30]})
        if not profile: raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(401, 'A verified Google account or external wallet is required.') from None
    return access['sub'], profile
