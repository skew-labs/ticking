"""Carry only a validated client address through trusted local tool calls."""
from contextvars import ContextVar
from ipaddress import ip_address
from urllib.request import Request
_client=ContextVar('em_request_client',default=None)

def request_ip(request):
    peer=request.client.host if request.client else ''
    try:
        peer_ip=ip_address(peer)
        if peer_ip.is_loopback:
            claimed=request.headers.get('x-real-ip','').strip()
            try:return str(ip_address(claimed))
            except ValueError:pass
        return str(peer_ip)
    except ValueError:return 'unknown'

def forwarded_request(url):
    # The only consumers call configured loopback EM tools. A context never
    # forwards cookies, tokens, arbitrary headers or an unvalidated identity.
    from urllib.parse import urlsplit
    host=urlsplit(url).hostname
    trusted=host in ('127.0.0.1','::1','localhost')
    return Request(url,headers={'X-Real-IP':_client.get()} if trusted and _client.get() else {})

def install_identity(app):
    @app.middleware('http')
    async def identity(request,call_next):
        token=_client.set(request_ip(request))
        try:return await call_next(request)
        finally:_client.reset(token)
