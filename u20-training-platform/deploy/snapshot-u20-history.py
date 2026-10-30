#!/usr/bin/env python3
"""Create a protected preliminary snapshot of u20 training history, without deletion.

This is not an atomic cutover snapshot: the live service may change between
copies. Repeat under the reviewed maintenance procedure immediately before
any deletion; never restore over newer data without explicit reconciliation.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

DB = Path('/var/lib/pro6000d-training/state.sqlite3')
JOBCOMP = Path('/var/log/slurm/jobcomp.log')
BACKUPS = Path('/var/backups/pro6000d-training')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def trusted_directory(path):
    for part in reversed((path,) + tuple(path.parents)):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('untrusted backup directory: ' + str(part))


def main():
    if os.geteuid() != 0:
        raise ValueError('root required')
    os.umask(0o077)
    trusted_directory(BACKUPS)
    source_stat = DB.lstat()
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_uid != 0 or source_stat.st_mode & 0o022:
        raise ValueError('untrusted database')
    job_fd = os.open(str(JOBCOMP), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        old = os.fstat(job_fd)
        if not stat.S_ISREG(old.st_mode) or old.st_nlink != 1 or old.st_mode & 0o022:
            raise ValueError('untrusted job completion log')
        folder = Path(tempfile.mkdtemp(prefix='history-precutover-', dir=str(BACKUPS)))
        target_db = folder / 'state.sqlite3'
        deadline = time.monotonic() + 30
        def progress(_status, _remaining, _total):
            if time.monotonic() > deadline:
                raise TimeoutError('SQLite backup timed out')
        with sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True, timeout=5) as src, sqlite3.connect(str(target_db)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=.1)
            if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('snapshot database integrity check failed')
            counts = {name: dst.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                      for name in ('jobs', 'applications', 'audit_log', 'quota_overrides')}
        os.chmod(target_db, 0o600)
        target_log = folder / 'jobcomp.log'
        with target_log.open('xb') as out:
            while True:
                block = os.read(job_fd, 1024 * 1024)
                if not block:
                    break
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        after = os.fstat(job_fd)
        if (old.st_dev, old.st_ino, old.st_size, old.st_mtime_ns, old.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('job completion log changed during copy; snapshot is incomplete')
        manifest = {'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'purpose': 'preliminary only, not an atomic cutover snapshot',
                    'db_sha256': digest(target_db), 'jobcomp_sha256': digest(target_log),
                    'row_counts': counts, 'jobcomp_original_uid': old.st_uid,
                    'jobcomp_original_gid': old.st_gid, 'jobcomp_original_mode': oct(stat.S_IMODE(old.st_mode))}
        with (folder / 'manifest.json').open('x') as out:
            json.dump(manifest, out, indent=2, sort_keys=True)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        directory = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        print('Preliminary protected backup:', folder)
        print('Snapshot counts:', json.dumps(counts, sort_keys=True))
        print('No live history or service was changed; repeat at cutover.')
    finally:
        os.close(job_fd)


if __name__ == '__main__':
    main()
