"""Anchored, all-or-nothing source patches shared by the first-class patch sets.

Each replacement's anchor must occur exactly once in the pristine file; nothing is written
unless every anchor in every file matches. `--check` only validates.
"""
import hashlib
import shutil
import sys
from pathlib import Path


def apply(root, patches, new_files=(), check=False):
    root = Path(root)
    staged, errors = {}, []
    for name, edits in patches.items():
        path = root / name
        text = path.read_text()
        for old, new in edits:
            n = text.count(old)
            if n != 1:
                errors.append('%s: anchor matched %d times: %r' % (name, n, old[:90]))
                continue
            text = text.replace(old, new)
        staged[name] = text
    if errors:
        sys.exit('patch refused (nothing written):\n  ' + '\n  '.join(errors))
    if check:
        print('check ok: %d files, %d edits' % (len(staged), sum(len(e) for e in patches.values())))
        return
    for name, text in staged.items():
        (root / name).write_text(text)
    for src, dest in new_files:
        shutil.copy2(src, root / dest)
    print('applied: ' + ', '.join(sorted(staged) + [d for _, d in new_files]))
    for name in sorted(staged) + [d for _, d in new_files]:
        print('  %s  %s' % (hashlib.sha256((root / name).read_bytes()).hexdigest()[:16], name))
