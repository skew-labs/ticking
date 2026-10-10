"""Find the resumable archive suffix without statting every completed segment."""
import os
from pathlib import Path
from archive_segment import exists as segment_exists

def retained_tail_paths(folder,segment=None):
    root=Path(folder)
    if not root.exists():return []
    with os.scandir(root) as entries:
        names=sorted(e.name for e in entries if len(e.name)==8 and e.name.isascii() and e.name.isdigit()
                     and (segment is None or e.name>=segment) and e.is_dir(follow_symlinks=False))
    # A new consumer has always started with the two most recent segments.
    # A recovered cursor must retain every subsequent segment, even if behind.
    if segment is None:
        paths=[]
        for name in reversed(names):
            p=root/name/'inputs.sli'
            if segment_exists(p):paths.append(p)
            if len(paths)==2:break
        return paths[::-1]
    return [p for name in names if segment_exists(p:=root/name/'inputs.sli')]
