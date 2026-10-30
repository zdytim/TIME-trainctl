#!/usr/bin/env python3
"""Upgrade the u20 broker and Cockpit UI with monthly enforcement disabled.

Only the exact reviewed u20 baseline and candidate are accepted. Existing
jobs and the live database are never rolled back or deleted by this script.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time

SERVICE = 'pro6000d-training.service'
LIB = Path('/usr/local/lib/pro6000d-training')
UI = Path('/usr/share/cockpit/pro6000d-training')
CONFIG = Path('/etc/pro6000d-training/config.json')
DATABASE = Path('/var/lib/pro6000d-training/state.sqlite3')
BACKUPS = Path('/var/backups/pro6000d-training')
CLIENT = Path('/usr/local/bin/pro6000d-training-client')
LOCK = Path('/run/lock/time-u20-console-upgrade.lock')
OLD = {'broker.py': (LIB / 'pro6000d-training-api.py', '65d213100a0e0ebd3f9fe87b51d1625f8539acf954ca572b43fa48a07c43173c'),
       'app.js': (UI / 'app.js', 'cf9f2b5bed41e944552c9d9e06a24ffd2eb4b6cc3e8c8fa26ed253cc6a23e067'),
       'index.html': (UI / 'index.html', 'aaba539e8b8253580623e0bfc083fee75005aa23f1b361ee5959b236991fa9f6'),
       'styles.css': (UI / 'styles.css', '23106d77623ca6f6143f8e8c7bbd10098467a1895c55ff547d959e65984fd656')}
NEW = {'broker.py': (LIB / 'pro6000d-training-api.py', '7372e1d9ad73f6899aa3f2e79e24aae594e42717ba941a6044ddfb31831ea55c'),
       'controller_bridge.py': (LIB / 'controller_bridge.py', '30ac4668f6ea6f0a413f4a4a834b27d42c5db069900600058d18da58e2b04a70'),
       'controller_view.py': (LIB / 'controller_view.py', '10be510b5bee9afaec955293dbff499140e099ff612eed1186e7e96e299a637d'),
       'app.js': (UI / 'app.js', '7f77635814abdaf9bb611ba35e87e0fd64619cd58eb66d0349591b0e641f2fb3'),
       'index.html': (UI / 'index.html', 'c94c0ad64450bb20e014a19e65b6a849983590d5ec571be22b33f2353af0d8e6'),
       'styles.css': (UI / 'styles.css', 'f9ef0f3c17b98b8185f6b3456c27a9b4514417021f449ac1bddbe1114440ac26')}
TERMINAL = ('COMPLETED', 'FAILED', 'CANCELLED', 'CANCELED', 'TIMEOUT', 'NODE_FAIL',
            'OUT_OF_MEMORY', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'REJECTED', 'EXPIRED')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def trusted_directory(path):
    for part in reversed((path,) + tuple(path.parents)):
        st = part.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError('untrusted directory: ' + str(part))


def trusted_metadata(path, require_root_group=True):
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or
            (require_root_group and st.st_gid != 0) or st.st_nlink != 1 or st.st_mode & 0o022):
        raise ValueError('untrusted file: ' + str(path))


def trusted_file(path, expected=None):
    trusted_metadata(path)
    data = path.read_bytes()
    if expected is not None and digest(data) != expected:
        raise ValueError('unexpected file version: ' + str(path))
    return data


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=kwargs.pop('timeout', 30), **kwargs)


def no_scheduler_jobs():
    return not run(['/usr/bin/squeue', '-h', '-o', '%i']).stdout.strip()


def no_active_platform_jobs():
    uri = 'file:' + str(DATABASE) + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        placeholders = ','.join('?' for _ in TERMINAL)
        return db.execute('SELECT COUNT(*) FROM jobs WHERE state NOT IN (' + placeholders + ') OR state IS NULL',
                          TERMINAL).fetchone()[0] == 0


def atomic(path, data, mode=0o644):
    fd, name = tempfile.mkstemp(prefix='.time-upgrade-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def backup_database(destination):
    deadline = time.monotonic() + 30
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline: raise TimeoutError('database backup timed out')
    with sqlite3.connect('file:' + str(DATABASE) + '?mode=ro', uri=True, timeout=5) as source, sqlite3.connect(str(destination)) as target:
        source.backup(target, pages=256, progress=progress, sleep=.1)
        if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('database backup integrity check failed')
    os.chmod(destination, 0o600)


def api(uid, path):
    user = pwd.getpwuid(uid)
    result = run(['/usr/bin/setpriv', '--reuid=' + str(uid), '--regid=' + str(user.pw_gid),
                  '--init-groups', str(CLIENT)], input=json.dumps({'method': 'GET', 'path': path}) + '\n', timeout=8)
    payload = json.loads(result.stdout)
    if not payload.get('ok'): raise ValueError('API request failed for uid %d' % uid)
    return payload['data']


def health(staged):
    deadline = time.monotonic() + 20
    last = None
    while time.monotonic() < deadline:
        try:
            run(['/usr/bin/systemctl', 'is-active', '--quiet', SERVICE], timeout=4)
            for username in ('u20', 'timzhang'):
                uid = pwd.getpwnam(username).pw_uid
                data = api(uid, '/api/v1/state')
                if data['identity']['uid'] != uid or not data.get('scheduler_available'):
                    raise ValueError('identity or scheduler unavailable for ' + username)
                if staged:
                    monthly = data.get('monthly_quota', {})
                    if monthly.get('enforcement_ready') is not False or monthly.get('mode') != 'staged':
                        raise ValueError('monthly gate must remain staged')
                    capabilities = api(uid, '/api/v1/capabilities')
                    if capabilities.get('monthly_requests', {}).get('available') is not False:
                        raise ValueError('monthly requests must remain unavailable')
            return
        except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
            last = exc
            time.sleep(.5)
    raise RuntimeError('service health check timed out: ' + str(last))


def main(code_dir):
    if os.geteuid() != 0: raise ValueError('root required')
    os.umask(0o077)
    for directory in (code_dir, LIB, UI, CONFIG.parent, DATABASE.parent, BACKUPS): trusted_directory(directory)
    config = json.loads(trusted_file(CONFIG))
    settings = config.get('monthly_controller', {})
    if not isinstance(settings, dict) or any(settings.get(k) is True for k in ('read_enabled', 'grant_enabled', 'activation_verified')):
        raise ValueError('monthly controller must remain disabled during staged upgrade')
    if Path(config.get('state_db', str(DATABASE))) != DATABASE: raise ValueError('unexpected database path')
    trusted_metadata(DATABASE, require_root_group=False)
    trusted_metadata(CLIENT)
    before = {name: trusted_file(path, sha) for name, (path, sha) in OLD.items()}
    payload = {name: trusted_file(code_dir / name, sha) for name, (_path, sha) in NEW.items()}
    for name in ('controller_bridge.py', 'controller_view.py'):
        target = NEW[name][0]
        if target.exists() or target.is_symlink(): raise ValueError('new destination already exists: ' + str(target))
    if not no_scheduler_jobs() or not no_active_platform_jobs():
        raise ValueError('active or unconfirmed jobs exist; refusing to stop the backend')
    health(False)
    backup = Path(tempfile.mkdtemp(prefix='u20-console-staged-', dir=str(BACKUPS)))
    for name, data in before.items(): atomic(backup / name, data, 0o600)
    record = {'status': 'preparing', 'backup': str(backup), 'rollback': 'not required',
              'note': 'live database is never restored by automatic rollback'}
    def save(): atomic(backup / 'result.json', (json.dumps(record, indent=2) + '\n').encode(), 0o600)
    save()
    touched = []
    stopped = False
    try:
        stopped = True
        run(['/usr/bin/systemctl', 'stop', SERVICE])
        if not no_scheduler_jobs() or not no_active_platform_jobs():
            raise ValueError('jobs appeared while backend was stopping')
        backup_database(backup / 'state.sqlite3')
        for name, (path, _sha) in NEW.items():
            touched.append(name)
            atomic(path, payload[name])
        run(['/usr/bin/systemctl', 'start', SERVICE])
        stopped = False
        health(True)
        record['status'] = 'staged-upgrade-healthy'
        save()
        print('Staged broker/UI upgrade healthy; monthly gate disabled. Backup:', backup)
    except BaseException as exc:
        record['status'] = 'failed: ' + str(exc)
        try:
            if touched:
                run(['/usr/bin/systemctl', 'stop', SERVICE])
                stopped = True
                for name in reversed(touched):
                    path = NEW[name][0]
                    if not path.exists() and not path.is_symlink() and name not in before:
                        continue
                    current = trusted_file(path)
                    if name in before and digest(current) == OLD[name][1]:
                        continue  # interrupted before replacing this file
                    if digest(current) != NEW[name][1]:
                        raise ValueError('refusing to overwrite unexpected file during rollback: ' + str(path))
                    if name in before: atomic(path, before[name])
                    else: path.unlink()
            if stopped:
                run(['/usr/bin/systemctl', 'start', SERVICE])
            health(False)
            record['rollback'] = 'old code restored; live database preserved; old service healthy'
        except BaseException as rollback_error:
            record['rollback'] = 'FAILED: ' + str(rollback_error)
        save()
        print('Upgrade did not complete. Backup:', backup, 'Rollback:', record['rollback'], flush=True)
        raise


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2: raise SystemExit('usage: upgrade-u20-console-staged.py ROOT_OWNED_INPUT_DIR')
    def interrupted(_signum, _frame): raise RuntimeError('upgrade interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    lock_fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    lock_stat = os.fstat(lock_fd)
    if (not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != 0 or
            lock_stat.st_nlink != 1 or lock_stat.st_mode & 0o077):
        os.close(lock_fd)
        raise SystemExit('untrusted upgrade lock')
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main(Path(sys.argv[1]).absolute())
