"""Cross-process public-provider pacing; every caller shares cooldowns."""
import fcntl,json,time
from pathlib import Path

class ProviderBudget:
 def __init__(self,path,spacing=8):self.path=Path(path);self.spacing=spacing
 def _update(self,fn):
  self.path.parent.mkdir(parents=True,exist_ok=True)
  with self.path.open('a+') as f:
   fcntl.flock(f,fcntl.LOCK_EX);f.seek(0)
   text=f.read();state=json.loads(text) if text else {}
   result=fn(state);f.seek(0);f.truncate();json.dump(state,f);f.flush()
   return result
 def reserve(self,now=None):
  now=time.time() if now is None else now
  def update(state):
   wait=max(state.get('next',0),state.get('cooldown',0))-now
   if wait>0:return wait
   state['next']=now+self.spacing;return 0
  return self._update(update)
 def cooldown(self,until):
  return self._update(lambda s:s.update(cooldown=max(s.get('cooldown',0),until)))
