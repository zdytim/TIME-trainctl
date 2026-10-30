#!/usr/bin/env python3
"""Provision a new root-owned witness for a genuinely empty quota ledger.

Never use this to repair, replace or reset an existing witness or ledger.
"""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys

STATE = Path('/var/lib/slurm/ctld')
ANCHOR = Path('/var/lib/time-quota-anchor')
HEADER = b'U20MQ1|HEADER|1\n'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--new-empty-witness', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.new_empty_witness:
        raise ValueError('root and --new-empty-witness are required')
    isolated = os.environ.get('TIME_ISOLATED_VALIDATION') == '1' and Path('/.dockerenv').is_file()
    if not isolated:
        status = subprocess.run(['/usr/bin/systemctl', 'show', 'slurmctld.service', '-p', 'ActiveState', '--value'],
                                text=True, capture_output=True, timeout=5)
        if status.returncode or status.stdout.strip() not in ('inactive', 'failed'):
            raise ValueError('Slurm controller must be stopped and independently reviewed')
    uid = pwd.getpwnam('slurm').pw_uid
    state = STATE.lstat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != uid or state.st_mode & 0o022:
        raise ValueError('untrusted Slurm state directory')
    state_fd = os.open(str(STATE), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    opened = []
    try:
        observed = os.fstat(state_fd)
        if (observed.st_dev, observed.st_ino) != (state.st_dev, state.st_ino):
            raise ValueError('Slurm state directory changed during validation')
        # Lock in the controller order and require exact fresh state. This is
        # not evidence that historical user usage may legitimately be reset.
        for name in ('monthly-quota.wal', 'monthly-quota.checkpoint'):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=state_fd)
            opened.append(fd)
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1 or
                    st.st_mode & 0o077):
                raise ValueError('untrusted ledger file: ' + name)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        digest = hashlib.sha256(HEADER).hexdigest().encode('ascii')
        checkpoint = b'U20MQ1|CHECKPOINT|0|' + digest + b'\n'
        if os.read(opened[0], 256) != HEADER or os.read(opened[1], 256) != checkpoint:
            raise ValueError('only an exactly empty new ledger can be initialized')
        ANCHOR.mkdir(mode=0o700, exist_ok=True)
        st = ANCHOR.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
            raise ValueError('witness directory must be private and root-owned')
        directory = os.open(str(ANCHOR), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open('witness.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            try:
                content = b'U20MQA1|0|' + digest + b'\n'
                view = memoryview(content)
                while view:
                    n = os.write(fd, view)
                    if n <= 0: raise OSError('short witness write')
                    view = view[n:]
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        for fd in reversed(opened): os.close(fd)
        os.close(state_fd)
    print('Fresh witness created; quota controller remains disabled until separately activated.')

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print('Witness initialization failed; preserve any partial file for review: %s' % exc, file=sys.stderr)
        sys.exit(1)
