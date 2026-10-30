#!/usr/bin/env python3
"""Open the root-owned quota witness, then exec a trusted Slurm controller.

This is a candidate launcher; it does not create, repair or reset the witness.
"""
import fcntl
import os
from pathlib import Path
import stat
import sys

WITNESS = Path('/var/lib/time-quota-anchor/witness.log')

def trusted_directory(path):
    path = Path(os.path.abspath(path))
    for part in reversed((path,) + tuple(path.parents)):
        st = part.lstat()
        if (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or
                st.st_uid != 0 or st.st_mode & 0o022):
            raise ValueError('untrusted directory: ' + str(part))

def main():
    if os.geteuid() != 0 or len(sys.argv) < 2:
        raise ValueError('root and an absolute controller path are required')
    controller = Path(sys.argv[1])
    if not controller.is_absolute() or controller.name != 'slurmctld':
        raise ValueError('expected an absolute slurmctld path')
    trusted_directory(controller.parent)
    st = controller.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022):
        raise ValueError('controller must be a root-owned regular file')
    trusted_directory(WITNESS.parent)
    directory = os.open(str(WITNESS.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(WITNESS.name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or
            st.st_mode & 0o077 or st.st_size == 0):
        raise ValueError('witness must be a nonempty, private, single-link root file')
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(fd, True)
    env = os.environ.copy()
    env['TIME_QUOTA_WITNESS_FD'] = str(fd)
    os.execve(str(controller), sys.argv[1:], env)

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        print('Quota witness launcher refused startup: %s' % exc, file=sys.stderr)
        sys.exit(1)
