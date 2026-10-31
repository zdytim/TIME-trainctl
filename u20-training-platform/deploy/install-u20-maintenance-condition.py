#!/usr/bin/env python3
"""Install an inactive systemd maintenance condition; do not stop the broker."""
import hashlib
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys

UNIT = 'pro6000d-training.service'
BASE = Path('/etc/systemd/system/pro6000d-training.service')
OLD_DROPIN = Path('/etc/systemd/system/pro6000d-training.service.d/slurm.conf')
DEST = Path('/etc/systemd/system/pro6000d-training.service.d/time-maintenance.conf')
MARKER = Path('/var/lib/pro6000d-training/time-monthly-maintenance')
EXPECTED = {
    BASE: 'c138a88c33247daecb046a7059bda89f9b743f3dbf3f47261f49d06e56fe31c3',
    OLD_DROPIN: '07becdd4ba6e0c755569e956931a13bab35774a59fec47a45a79ec28a0bece42',
}
SOURCE_SHA = 'f2d32e2a11e6a15b069fbd7faf5ee41737e520bff245d0c762fb82ebe8b51233'


def trusted(path, directory=False):
    for item in reversed((path,) + tuple(path.parents)):
        st = item.lstat(); is_dir = item != path or directory
        if (stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_gid != 0 or st.st_mode & 0o022 or
                (not stat.S_ISDIR(st.st_mode) if is_dir else not stat.S_ISREG(st.st_mode))):
            raise ValueError('untrusted path: ' + str(item))
    if not directory and path.lstat().st_nlink != 1:
        raise ValueError('unexpected link count: ' + str(path))


def read_pinned(path, sha):
    trusted(path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha: raise ValueError('unexpected file version: ' + str(path))
    return data


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def properties():
    output = run(['/usr/bin/systemctl', 'show', UNIT, '-p', 'MainPID', '-p', 'ActiveState',
                  '-p', 'FragmentPath', '-p', 'DropInPaths'])
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def main(source):
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('root on the expected u20 host is required')
    for path, digest in EXPECTED.items(): read_pinned(path, digest)
    trusted(DEST.parent, True); trusted(MARKER.parent, True)
    data = read_pinned(source, SOURCE_SHA)
    if MARKER.exists() or MARKER.is_symlink(): raise ValueError('maintenance marker already exists')
    if DEST.exists() or DEST.is_symlink(): raise FileExistsError('condition drop-in already exists')
    before = properties()
    if (before.get('ActiveState') != 'active' or not int(before.get('MainPID', '0')) or
            before.get('FragmentPath') != str(BASE) or before.get('DropInPaths') != str(OLD_DROPIN)):
        raise ValueError('unexpected backend service state or drop-in')
    parent = os.open(str(DEST.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    created = False
    identity = None
    try:
        fd = os.open(DEST.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        created = True
        info = os.fstat(fd); identity = (info.st_dev, info.st_ino, info.st_uid, info.st_gid)
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fchmod(out.fileno(), 0o644); os.fsync(out.fileno())
        os.fsync(parent)
        run(['/usr/bin/systemctl', 'daemon-reload'])
        after = properties()
        if (after.get('ActiveState') != 'active' or after.get('MainPID') != before['MainPID'] or
                set(after.get('DropInPaths', '').split()) != {str(OLD_DROPIN), str(DEST)}):
            raise ValueError('systemd did not load the expected inactive maintenance condition')
        if MARKER.exists() or MARKER.is_symlink(): raise ValueError('maintenance marker appeared during installation')
        print('MAINTENANCE_CONDITION_INSTALLED pid=' + before['MainPID'] + ' marker_absent=true')
    except BaseException:
        if created:
            try:
                current = os.stat(DEST.name, dir_fd=parent, follow_symlinks=False)
                if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or
                        (current.st_dev, current.st_ino, current.st_uid, current.st_gid) != identity):
                    raise ValueError('drop-in identity changed during rollback')
                os.unlink(DEST.name, dir_fd=parent); os.fsync(parent)
                run(['/usr/bin/systemctl', 'daemon-reload'])
                print('ROLLBACK: newly created drop-in removed and systemd reloaded', file=sys.stderr)
            except BaseException as rollback_error:
                print('ROLLBACK FAILED: ' + str(rollback_error), file=sys.stderr)
        raise
    finally:
        os.close(parent)


if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('usage: install-u20-maintenance-condition.py ROOT_OWNED_SOURCE')
    def interrupted(_sig, _frame): raise RuntimeError('installation interrupted')
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGHUP, interrupted)
    try: main(Path(sys.argv[1]).absolute())
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
        print('Installation refused or failed: ' + str(exc), file=sys.stderr); sys.exit(1)
