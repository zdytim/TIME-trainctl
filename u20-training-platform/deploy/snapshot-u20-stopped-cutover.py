#!/usr/bin/env python3
"""Capture an offline cutover snapshot; never stop, restart or alter services.

The caller must first fence submissions, verify there are no active jobs, and
stop the backend and controller. A snapshot is not an automatic rollback.
"""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile

ROOT = Path('/var/backups/pro6000d-training')
MARKER = Path('/var/lib/pro6000d-training/time-monthly-maintenance')
DB = Path('/var/lib/pro6000d-training/state.sqlite3')
STATE = Path('/var/lib/slurm/ctld')
JOBCOMP = Path('/var/log/slurm/jobcomp.log')
CONFIGS = (Path('/etc/slurm-llnl/slurm.conf'),
           Path('/etc/pro6000d-training/config.json'),
           Path('/etc/systemd/system/slurmctld.service.d/platform.conf'))


def identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def snapshot_file(source, destination):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('not a single-link regular file: ' + str(source))
        digest = hashlib.sha256()
        out_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(out_fd, 'wb') as out:
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                out.write(block)
                digest.update(block)
            out.flush()
            os.fsync(out.fileno())
        after = os.fstat(fd)
        current = os.lstat(source)
        if identity(before) != identity(after) or identity(before) != identity(current):
            raise ValueError('source changed while backing up: ' + str(source))
        return {'sha256': digest.hexdigest(), 'uid': before.st_uid, 'gid': before.st_gid,
                'mode': stat.S_IMODE(before.st_mode), 'size': before.st_size}
    finally:
        os.close(fd)


def snapshot_tree(source, target, entries, prefix=''):
    before = os.lstat(source)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError('state directory is not a directory: ' + str(source))
    target.mkdir(mode=0o700)
    entries[prefix or '.'] = {'type': 'directory', 'uid': before.st_uid,
                             'gid': before.st_gid, 'mode': stat.S_IMODE(before.st_mode)}
    names = sorted(os.listdir(source))
    for name in names:
        src, dst = source / name, target / name
        rel = (prefix + '/' + name).lstrip('/')
        value = os.lstat(src)
        if stat.S_ISDIR(value.st_mode):
            snapshot_tree(src, dst, entries, rel)
        elif stat.S_ISREG(value.st_mode):
            entries[rel] = {'type': 'file', **snapshot_file(src, dst)}
        else:
            raise ValueError('unsupported state entry: ' + str(src))
    if identity(before) != identity(os.lstat(source)) or names != sorted(os.listdir(source)):
        raise ValueError('state directory changed while backing up: ' + str(source))
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def inactive(name):
    proc = subprocess.run(['/usr/bin/systemctl', 'show', name, '-p', 'ActiveState', '-p', 'MainPID'],
                          check=True, text=True, capture_output=True, timeout=15)
    fields = dict(line.split('=', 1) for line in proc.stdout.splitlines() if '=' in line)
    if fields != {'MainPID': '0', 'ActiveState': 'inactive'}:
        raise ValueError(name + ' is not stopped')


def requirements():
    info = MARKER.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError('maintenance marker is missing or untrusted')
    for service in ('slurmctld.service', 'pro6000d-training.service'):
        inactive(service)


def main():
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('root on u20 is required')
    os.umask(0o077)
    for parent in reversed((ROOT,) + tuple(ROOT.parents)):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('untrusted backup path: ' + str(parent))
    lockfd = os.open('/run/lock/time-u20-console-upgrade.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, 'a') as lock:
        st = os.fstat(lock.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o077:
            raise ValueError('untrusted maintenance lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        snapshot_locked()


def snapshot_locked():
    """Caller must hold the validated exclusive maintenance lock."""
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('root on u20 is required')
    for parent in reversed((ROOT,) + tuple(ROOT.parents)):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('untrusted backup path: ' + str(parent))
    requirements()
    folder = Path(tempfile.mkdtemp(prefix='cutover-stopped-', dir=str(ROOT)))
    print('Snapshot location (retain on failure): ' + str(folder), flush=True)
    metadata = {'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'note': 'offline snapshot; active-job exclusion and restore require independent checks',
                'state': {}, 'files': {}}
    snapshot_tree(STATE, folder / 'slurm-state', metadata['state'])
    metadata['files']['jobcomp.log'] = snapshot_file(JOBCOMP, folder / 'jobcomp.log')
    for n, path in enumerate(CONFIGS):
        metadata['files']['config-%d' % n] = {'source': str(path),
                                                **snapshot_file(path, folder / ('config-%d' % n))}
    dbstat = os.lstat(DB)
    if not stat.S_ISREG(dbstat.st_mode) or dbstat.st_uid != 0 or dbstat.st_mode & 0o077:
        raise ValueError('untrusted live database')
    with sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True, timeout=5) as src, \
            sqlite3.connect(str(folder / 'state.sqlite3')) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('database snapshot integrity check failed')
    if identity(dbstat) != identity(os.lstat(DB)):
        raise ValueError('database changed while backing up')
    dbfd = os.open(folder / 'state.sqlite3', os.O_RDONLY | os.O_NOFOLLOW)
    try: os.fsync(dbfd)
    finally: os.close(dbfd)
    metadata['database'] = {'sha256': hashlib.sha256((folder / 'state.sqlite3').read_bytes()).hexdigest(),
                            'uid': dbstat.st_uid, 'gid': dbstat.st_gid,
                            'mode': stat.S_IMODE(dbstat.st_mode)}
    requirements()
    with (folder / 'manifest.json').open('x') as out:
        json.dump(metadata, out, sort_keys=True, indent=2); out.write('\n'); out.flush(); os.fsync(out.fileno())
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)
    print('SNAPSHOT_COMPLETE ' + str(folder))
    return folder


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print('Snapshot failed; retain partial files: ' + str(exc), file=sys.stderr); sys.exit(1)
