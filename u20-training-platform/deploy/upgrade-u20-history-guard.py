#!/usr/bin/env python3
"""Install the tombstone-aware broker with the reviewed guarded rollout.

This does not delete history or enable monthly quota enforcement.
"""
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import signal
import sqlite3
import stat
import sys

HASHES = {
    'rollout.py': 'f63b459698170cc48cc2ce7c17b2103e4cb6f0b931ef883f4e2665736b21eff3',
    'base.py': '7611b73148879a4ef831d38a4ceb5384c8cf11239900d0ef751acdde92597978',
    'broker.py': '24afd69cff5eac856f2ccaaf8a97b95c1823c911c83f40338745cd4ab813e821',
}
OLD = '4224e6aa116532585c6c9e837ee102e9d508ef00b04e6e685bd5e6f092e88821'


def trusted_directory(directory):
    for path in reversed((directory,) + tuple(directory.parents)):
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError('untrusted directory: ' + str(path))


def load_checked(directory, name):
    path = directory/name
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o022:
        raise ValueError('untrusted file: ' + str(path))
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != HASHES[name]:
        raise ValueError('unexpected file version: ' + str(path))
    return data


def module_from_bytes(name, data, path):
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module


def main(directory):
    if os.geteuid() != 0: raise ValueError('root required')
    trusted_directory(directory)
    data = {name:load_checked(directory, name) for name in HASHES}
    rollout = module_from_bytes('time_gpu_rollout', data['rollout.py'], directory/'rollout.py')
    base = module_from_bytes('time_base_rollout', data['base.py'], directory/'base.py')
    rollout.OLD = OLD
    rollout.NEW = HASHES['broker.py']
    original_health = base.health
    def health_with_history_schema(staged):
        original_health(staged)
        if staged:
            with sqlite3.connect('file:' + str(base.DATABASE) + '?mode=ro', uri=True, timeout=5) as db:
                names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'job_request_tombstones' not in names:
                    raise ValueError('candidate history-guard table is missing')
    base.health = health_with_history_schema
    def interrupted(_sig, _frame): raise RuntimeError('upgrade interrupted')
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGHUP, interrupted)
    fd = os.open('/run/lock/time-u20-console-upgrade.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC, 0o600)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o077:
        raise ValueError('untrusted upgrade lock')
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        rollout.main(base, directory/'broker.py')


if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('usage: upgrade-u20-history-guard.py ROOT_OWNED_INPUT_DIR')
    main(Path(sys.argv[1]).absolute())
