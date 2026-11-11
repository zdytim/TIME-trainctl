#!/usr/bin/env python3
"""Hash every file, mode and internal symlink in a staged Slurm installation."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def release_tree(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir(): raise ValueError('release tree is not a directory')
    entries=[]
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        for name in sorted(dirs + files):
            path=Path(current)/name
            info=path.lstat(); mode=stat.S_IMODE(info.st_mode)
            rel=path.relative_to(root).as_posix()
            if '\n' in rel or '\x00' in rel: raise ValueError('invalid artifact path')
            if stat.S_ISLNK(info.st_mode):
                target=os.readlink(path)
                resolved=path.resolve(strict=True)
                if root not in resolved.parents: raise ValueError('external release symlink')
                entries.append({'path':rel,'kind':'symlink','target':target})
            elif stat.S_ISDIR(info.st_mode):
                if mode & 0o7022: raise ValueError('unsafe release directory mode')
                entries.append({'path':rel,'kind':'directory','mode':mode})
            elif stat.S_ISREG(info.st_mode):
                if mode & 0o7022: raise ValueError('unsafe release file mode')
                if hasattr(os,'listxattr') and 'security.capability' in os.listxattr(path,follow_symlinks=False):
                    raise ValueError('release file has capabilities')
                h=hashlib.sha256()
                with path.open('rb') as f:
                    for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
                entries.append({'path':rel,'kind':'file','mode':mode,'sha256':h.hexdigest()})
            else: raise ValueError('special file in release tree')
    entries.sort(key=lambda entry:entry['path'])
    return (json.dumps(entries,sort_keys=True,separators=(',',':'))+'\n').encode()

if __name__ == '__main__':
    if len(sys.argv)!=2: raise SystemExit('usage: release_tree.py DIRECTORY')
    sys.stdout.buffer.write(release_tree(sys.argv[1]))
