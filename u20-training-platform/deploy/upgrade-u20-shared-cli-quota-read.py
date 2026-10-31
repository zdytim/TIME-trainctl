#!/usr/bin/env python3
"""Correct shared CLI's monthly read capability, retaining a root-only backup."""
import hashlib
import os
from pathlib import Path
import stat
import tempfile

TARGET = Path('/usr/local/lib/time-cli/0858424-monthly-discovery-1/time_core.py')
BACKUPS = Path('/var/backups/pro6000d-training')
OLD = '972d895cf3a9d7b177a2c4f4668c2e92973b19c42dc61f5d92a6c8fb8d82d83c'
NEEDLE = b'"client_support": "unsupported", "accounting": "unknown",'
REPLACEMENT = b'"client_support": "read", "accounting": "unknown",'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def main():
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('root on u20 required')
    for directory in (TARGET.parent, BACKUPS):
        for parent in (directory, *directory.parents):
            s = parent.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
                raise ValueError('untrusted directory: '+str(parent))
    s = TARGET.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_gid != 0 or s.st_nlink != 1 or s.st_mode & 0o022:
        raise ValueError('untrusted file')
    old = TARGET.read_bytes()
    if digest(old) != OLD or old.count(NEEDLE) != 1:
        raise ValueError('unexpected shared CLI version')
    new = old.replace(NEEDLE, REPLACEMENT, 1)
    backup = Path(tempfile.mkdtemp(prefix='shared-cli-quota-read-', dir=BACKUPS)) / 'time_core.py.before'
    with backup.open('xb') as f:
        f.write(old); f.flush(); os.fsync(f.fileno())
    os.chmod(backup, 0o600)
    fd, tmp = tempfile.mkstemp(prefix='.time-core-', dir=TARGET.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(new); f.flush(); os.fchmod(f.fileno(), 0o644); os.fsync(f.fileno())
        if digest(TARGET.read_bytes()) != OLD:
            raise ValueError('file changed during installation')
        os.replace(tmp, TARGET)
        dfd = os.open(TARGET.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print('INSTALLED backup='+str(backup)+' sha256='+digest(TARGET.read_bytes()))

if __name__ == '__main__':
    try: main()
    except (OSError, ValueError) as exc:
        raise SystemExit('Upgrade refused: '+str(exc))
