"""Meter only new source work; ordinary tools and saved-result explanations stay free."""
import asyncio
import json
import contextvars
import sys
import os
from pathlib import Path
from fastapi import HTTPException

ROOT=Path(os.environ.get('TICKING_CONSOLE_SOURCE','/srv/skew/tick-market-ui-20261007'))
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from server import service_usage

current=contextvars.ContextVar('ticking_service_reservation',default=None)


def begin(request,kind,payload):
    row=service_usage.reserve(service_usage.owner(request),request.headers.get('Idempotency-Key'),kind,payload)
    current.set(row)
    if row['status']=='completed':
        value=service_usage.result(row['user_id'],row['id'])
        value['_service']=service_usage.metadata(row,True)
        return value
    return None


def finish(value,status):
    row=current.get()
    if not row:return value
    if row['status']=='completed':
        current.set(None);return value
    meaningful=status==200 and value.get('state') in {'analysis','asset_research','wallet_research'}
    if value.get('state')=='analysis':
        analysis=value.get('analysis',{})
        # Missing input is not a completed analysis. A supported WAIT is useful
        # work and may consume an included unit; it never implies an entry.
        meaningful=meaningful and bool(analysis.get('stance')) and not any(s in str(analysis['stance']).lower() for s in ['insufficient','unavailable'])
    if value.get('state')=='asset_research':
        research=value.get('research',{})
        meaningful=meaningful and bool(research.get('issues') or research.get('headlines'))
    if value.get('state')=='wallet_research':
        meaningful=meaningful and value.get('report',{}).get('status') not in {'unsupported','unavailable'}
    if not meaningful:
        service_usage.release(row['id']);current.set(None);return value
    try:done=service_usage.complete(row['id'],value)
    except BaseException:
        service_usage.release(row['id']);current.set(None);raise
    current.set(None)
    return {**value,'_service':service_usage.metadata(done)}


async def execute(request,handler,reply):
    token=current.set(None)
    try:
        try:response=await handler(request)
        except HTTPException as e:response=reply({'error':e.detail,'code':'service_access'},e.status_code)
        row=current.get()
        if row and row['status']=='reserved' and await request.is_disconnected():
            service_usage.release(row['id']);current.set(None);return response
        value=json.loads(response.body)
        return reply(finish(value,response.status_code),response.status_code)
    except HTTPException as e:return reply({'error':e.detail,'code':'service_access'},e.status_code)
    finally:
        row=current.get()
        if row and row['status']=='reserved':await asyncio.to_thread(service_usage.release,row['id'])
        current.reset(token)
