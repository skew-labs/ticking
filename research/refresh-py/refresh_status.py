#!/usr/bin/env python3
"""Finalize an interrupted refresh after systemd has reaped its entire cgroup."""
import fcntl,json,os,time
from pathlib import Path

def finalize(state, service_result, exit_code, exit_status):
    state=Path(state)
    with (state/'refresh.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'changed':False,'reason':'refresh still holds its lock'}
        path=state/'status.json'
        if not path.exists():return {'changed':False,'reason':'no status'}
        raw=path.read_bytes();doc=json.loads(raw)
        if doc.get('result')!='running':return {'changed':False,'reason':'already finalized'}
        pid=doc.get('pid')
        if isinstance(pid,int):
            try:
                cmd=Path('/proc',str(pid),'cmdline').read_bytes()
                if b'/refresh.py' in cmd:return {'changed':False,'reason':'refresh process still exists'}
            except FileNotFoundError:pass
        receipt={'service_result':service_result,'exit_code':exit_code,'exit_status':exit_status}
        for scope in [doc.get('global',{}),*doc.get('chains',{}).values()]:
            for step in scope.get('steps',{}).values():
                if step.get('status')=='running':
                    step.update(status='interrupted',reason='Refresh process stopped before this step completed')
        doc.update(result='failed',finished_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),termination=receipt)
        tmp=path.with_suffix('.json.tmp');tmp.write_text(json.dumps(doc,indent=1));os.replace(tmp,path)
        with (state/'status_history.jsonl').open('a') as f:
            f.write(json.dumps({'run_id':doc['run_id'],'result':'failed','finished_utc':doc['finished_utc'],'termination':receipt,
              'chains':{c:{k:s.get('status') for k,s in v.get('steps',{}).items()} for c,v in doc.get('chains',{}).items()}})+'\n')
        return {'changed':True,'run_id':doc['run_id'],'result':'failed'}

if __name__=='__main__':
    print(json.dumps(finalize('/srv/skew/em-refresh/state',os.environ.get('SERVICE_RESULT','unknown'),os.environ.get('EXIT_CODE','unknown'),os.environ.get('EXIT_STATUS','unknown'))))
