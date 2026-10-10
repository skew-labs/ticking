"""Cross-process public-provider pacing; every caller shares cooldowns."""
import fcntl,json,time
from pathlib import Path

class ProviderBudget:
 def __init__(self,path,spacing=8):self.path=Path(path);self.spacing=spacing;self._ticket=None
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
   state['next']=now+self.spacing;state['grantAfter']=now+self.spacing;return 0
  return self._update(update)
 def reserve_fair(self,now=None,max_wait=None):
  """Reserve one future turn; a competing reader cannot repeatedly jump it.

  The reservation is process-local, while its occupied time is shared. A crash
  wastes at most one spacing interval. A late previous grant shifts the waiter
  by its actual remaining spacing, and a new cooldown invalidates old tickets.
  """
  now=time.time() if now is None else now
  if max_wait is not None and max_wait<0:raise ValueError('Negative provider wait budget')
  def update(state):
   cooldown=state.get('cooldown',0)
   if self._ticket is None or cooldown>self._ticket[1]:
    due=max(now,state.get('next',0),state.get('grantAfter',0),cooldown)
    # Do not reserve a distant ticket for a one-shot caller that must exit.
    if max_wait is not None and due-now>max_wait:
     self._ticket=None;return due-now
    self._ticket=(due,cooldown);state['next']=due+self.spacing
   due,_=self._ticket
   if now<due:return due-now
   if now<state.get('grantAfter',0):
    # A few microseconds of scheduler drift must not move a reserved turn to
    # the back of the whole queue on every request.
    due=state['grantAfter'];self._ticket=(due,cooldown)
    state['next']=max(state.get('next',0),due+self.spacing);return due-now
   state['grantAfter']=now+self.spacing
   state['next']=max(state.get('next',0),state['grantAfter'])
   self._ticket=None;return 0
  return self._update(update)
 def cooldown(self,until):
  return self._update(lambda s:s.update(cooldown=max(s.get('cooldown',0),until)))
