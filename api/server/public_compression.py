"""Compress public catalog reads; private keys, sessions and invoices bypass it."""
from starlette.middleware.gzip import GZipMiddleware

class PublicMarketCompression:
    def __init__(self,app):
        self.app=app
        self.compressed=GZipMiddleware(app,minimum_size=1024,compresslevel=3)
    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        public=(path=='/api/public/markets' or path.startswith('/api/public/markets/'))
        chosen=self.compressed if scope['type']=='http' and scope.get('method')=='GET' and public else self.app
        await chosen(scope,receive,send)
