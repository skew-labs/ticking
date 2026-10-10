"""Only locally normalized, content-addressed public token thumbnails."""
import json,re,time
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import FileResponse
from .store import DB_PATH

ROOT=DB_PATH.parent/'token-icons'
_cache=(0,{})
NAME=re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}-[a-f0-9]{16}\.webp')
EXCHANGE_NAME=re.compile(r'exchange-binance-(?:spot|usdm)-[a-z0-9]{2,30}-[a-f0-9]{16}\.webp')

def manifest():
    global _cache
    if time.monotonic()-_cache[0]>30:
        try:
            value=json.loads((ROOT/'manifest.json').read_text())
            if not isinstance(value.get('icons',{}),dict) or not isinstance(value.get('exchangeIcons',{}),dict):raise ValueError('Invalid artwork manifest')
            _cache=(time.monotonic(),value)
        except (OSError,ValueError):
            _cache=(time.monotonic(),_cache[1])
    return _cache[1]

def index():return manifest().get('icons',{})

def exchange_icon_url(source,market):
    if source not in {'binance_spot','binance_usdm'} or not re.fullmatch(r'[A-Z0-9]{2,30}',market or ''):return None
    row=manifest().get('exchangeIcons',{}).get(source+':'+market,{})
    name=row.get('file','');identity=row.get('instrument',{})
    prefix='exchange-'+source.replace('_','-')+'-'+market.lower()+'-'
    if EXCHANGE_NAME.fullmatch(name) and name.startswith(prefix) and identity.get('source')==source and identity.get('market')==market:return '/api/public/token-icons/'+name
    return None

def icon_url(mint):
    name=index().get(mint,{}).get('file','')
    return '/api/public/token-icons/'+name if NAME.fullmatch(name) and name.startswith(mint+'-') else None

def install(app):
    @app.get('/api/public/token-icons/{name}')
    def image(name:str):
        if not (NAME.fullmatch(name) or EXCHANGE_NAME.fullmatch(name)):raise HTTPException(404)
        path=ROOT/name
        if not path.is_file():raise HTTPException(404)
        return FileResponse(path,media_type='image/webp',headers={
            'Cache-Control':'public,max-age=31536000,immutable',
            'X-Content-Type-Options':'nosniff'})
