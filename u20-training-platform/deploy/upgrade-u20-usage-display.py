#!/usr/bin/env python3
"""Install the reviewed u20 Cockpit usage-display change without restarting services."""
import hashlib
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile

TARGET = Path('/usr/share/cockpit/pro6000d-training/app.js')
BACKUPS = Path('/var/backups/pro6000d-training')
OLD = '7f77635814abdaf9bb611ba35e87e0fd64619cd58eb66d0349591b0e641f2fb3'
NEW = '49ea825569063d09ed53be85f7363ef3bc15f342951a137f1cdb5c2b34840e09'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def main(source):
    if os.geteuid() != 0 or socket.gethostname() != 'u20-Z790-S-DDR4':
        raise ValueError('run as root on u20')
    for directory in (TARGET.parent, BACKUPS):
        for parent in (directory, *directory.parents):
            s = parent.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
                raise ValueError('untrusted directory: ' + str(parent))
    s = TARGET.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_gid != 0 or s.st_nlink != 1 or s.st_mode & 0o022:
        raise ValueError('untrusted installed file')
    previous = TARGET.read_bytes()
    if digest(previous) != OLD:
        raise ValueError('installed version differs; inspect before retrying')
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('candidate is not a regular file')
        candidate = f.read(2_000_000)
    if digest(candidate) != NEW:
        raise ValueError('candidate hash mismatch')
    backup = Path(tempfile.mkdtemp(prefix='usage-display-', dir=BACKUPS))
    old = backup / 'app.js.before'
    with old.open('xb') as f:
        f.write(previous); f.flush(); os.fsync(f.fileno())
    os.chmod(old, 0o600)
    fd, tmp = tempfile.mkstemp(prefix='.time-ui-', dir=TARGET.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(candidate); f.flush(); os.fchmod(f.fileno(), 0o644); os.fsync(f.fileno())
        if digest(TARGET.read_bytes()) != OLD:
            raise ValueError('installed file changed during upgrade')
        os.replace(tmp, TARGET)
        dfd = os.open(TARGET.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print('INSTALLED backup=' + str(old) + ' sha256=' + digest(TARGET.read_bytes()))

if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: upgrade-u20-usage-display.py CANDIDATE')
    try: main(sys.argv[1])
    except (OSError, ValueError) as exc:
        raise SystemExit('Upgrade refused: ' + str(exc))
