"""Cached display projection from owned, attributed history; no request-time scans."""
import json,math,time
from pathlib import Path
PATH=Path('./engine/data/reviewed-pool-prices/sparklines.json')
_checked=0.;_rows={}
def reference(mint,now):
 global _checked,_rows
 if time.monotonic()-_checked>15:
  _checked=time.monotonic()
  try:
   with PATH.open('rb') as f:raw=f.read(2*1024*1024+1)
   if len(raw)>2*1024*1024:raise ValueError('Oversized projection')
   parsed=json.loads(raw)['assets']
   if not isinstance(parsed,dict) or len(parsed)>512:raise ValueError('Invalid projection')
   _rows=parsed
  except (OSError,ValueError,KeyError,TypeError):_rows={}
 row=_rows.get(mint,{})
 if not isinstance(row,dict):return None
 points=row.get('points',[]);times=row.get('times',[])
 if row.get('mint')!=mint or not isinstance(points,list) or not isinstance(times,list) or not 2<=len(points)<=168 or len(times)!=len(points):return None
 if row.get('source')!='ticking_attributed_token_reference' or not isinstance(row.get('quoteMint'),str):return None
 if any(type(p) not in (int,float) or not math.isfinite(p) or p<=0 for p in points):return None
 if any(type(t)!=int or not now-7*86400-3600<=t<=now for t in times) or times!=sorted(set(times)):return None
 if row.get('actualStart')!=times[0] or row.get('actualEnd')!=times[-1]:return None
 return row
