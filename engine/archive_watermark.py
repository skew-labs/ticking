"""Ephemeral hand-off AFTER original fdatasync, never a recovery authority.

The archive reconstructs this marker from validated originals on startup. Its
loss can delay the consumer but cannot authorize skipping a record. No second
fsync is needed: durable history/cursors remain in their existing journals/DB.
"""
import hashlib,json,os,stat
from pathlib import Path
from archive_segment import size as segment_size

def publish(path,journal):
    active=journal.journal
    if active is None:return
    value={'segment':active.directory.name,'offset':os.fstat(active.fd).st_size,'records':journal.count}
    path=Path(path);tmp=path.with_suffix('.next')
    with tmp.open('w') as f:json.dump(value,f,separators=(',',':'))
    os.replace(tmp,path)

def read(path):
    # Native intake publishes the identical ephemeral record on tmpfs after
    # original fdatasync. This avoids creating/unlinking an SSD inode per batch.
    # The ordinary disk marker remains a slower fallback, never a skip authority.
    path=Path(path)
    memory=Path('/dev/shm')/('tick-tail-'+str(os.geteuid())+'-'+hashlib.sha256(str(path.parent.resolve()).encode()).hexdigest()+'.json')
    try:
        fd=os.open(memory,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and not info.st_mode&0o077:
                raw=os.read(fd,1025)
                return validate(raw)
        finally:os.close(fd)
    except (FileNotFoundError,PermissionError,ValueError,OSError):pass
    try:
        with Path(path).open('rb') as f:raw=f.read(1025)
    except FileNotFoundError:return None
    return validate(raw)

def validate(raw):
    if len(raw)>1024:raise ValueError('Durable tail marker bound')
    v=json.loads(raw)
    if not isinstance(v.get('segment'),str) or len(v['segment'])!=8 or not v['segment'].isascii() or not v['segment'].isdigit() or type(v.get('offset'))!=int or not 0<=v['offset']<2**32 or type(v.get('records'))!=int or v['records']<0:raise ValueError('Invalid durable tail marker')
    return v

def end_offset(path,head):
    segment=Path(path).parent.name
    if segment>head['segment']:return 0
    limit=head['offset'] if segment==head['segment'] else segment_size(path)
    if limit>segment_size(path):raise ValueError('Durable marker is ahead of original file')
    return limit
