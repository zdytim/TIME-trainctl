#!/usr/bin/env python3
"""Exercise the installed maintenance fence and restore the existing backend.

This does not stop Slurm, alter controller state, or delete history. An
unrecoverable interruption deliberately leaves the durable marker in place.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import uuid

MARKER = Path('/var/lib/pro6000d-training/time-monthly-maintenance')
SERVICE = 'pro6000d-training.service'
INPUTS = {
    'preflight.py': 'd6f21c23e2b0dd86e4ec792f246ec96b8185c58e17884a1d3e9f2af8ea3ec141',
    'base.py': '7611b73148879a4ef831d38a4ceb5384c8cf11239900d0ef751acdde92597978',
}


def checked(path, expected):
    for item in reversed((path,) + tuple(path.parents)):
        st = item.lstat(); directory = item != path
        if (stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_gid != 0 or st.st_mode & 0o022 or
                (not stat.S_ISDIR(st.st_mode) if directory else not stat.S_ISREG(st.st_mode))):
            raise ValueError('untrusted input: ' + str(item))
    if path.lstat().st_nlink != 1: raise ValueError('input has unexpected links')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected: raise ValueError('input digest mismatch')
    return data


def load(name, path, data):
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec); module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def service_state():
    data = run(['/usr/bin/systemctl', 'show', SERVICE, '-p', 'ActiveState', '-p', 'MainPID'])
    return dict(line.split('=', 1) for line in data.splitlines() if '=' in line)


def create_marker(parent, contents):
    fd = os.open(MARKER.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    info = os.fstat(fd)
    identity = (info.st_dev, info.st_ino, info.st_uid, info.st_gid)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(contents); out.flush(); os.fchmod(out.fileno(), 0o600); os.fsync(out.fileno())
        os.fsync(parent)
    except BaseException:
        current = os.stat(MARKER.name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or
                (current.st_dev, current.st_ino, current.st_uid, current.st_gid) != identity):
            raise ValueError('partial maintenance marker changed; manual recovery required')
        os.unlink(MARKER.name, dir_fd=parent); os.fsync(parent)
        raise
    return identity


def remove_marker(parent, identity, contents):
    fd = os.open(MARKER.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o077 or
                (st.st_dev, st.st_ino, st.st_uid, st.st_gid) != identity or
                os.read(fd, len(contents) + 1) != contents):
            raise ValueError('maintenance marker changed; refusing removal')
        current = os.stat(MARKER.name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (st.st_dev, st.st_ino):
            raise ValueError('maintenance marker path changed')
        os.unlink(MARKER.name, dir_fd=parent); os.fsync(parent)
    finally:
        os.close(fd)


def main(directory):
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('root and the expected u20 host are required')
    modules = {name: checked(directory / name, sha) for name, sha in INPUTS.items()}
    preflight = load('time_fence_preflight', directory/'preflight.py', modules['preflight.py'])
    base = load('time_fence_base', directory/'base.py', modules['base.py'])
    lock_fd = os.open('/run/lock/time-u20-console-upgrade.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    lock_info = os.fstat(lock_fd)
    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != 0 or lock_info.st_nlink != 1 or lock_info.st_mode & 0o077:
        os.close(lock_fd); raise ValueError('untrusted maintenance lock')
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        preflight.preflight(directory)
        base.trusted_directory(MARKER.parent)
        if MARKER.parent.stat().st_mode & 0o077: raise ValueError('marker parent must be private')
        parent = os.open(str(MARKER.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        contents = (json.dumps({'kind':'maintenance-rehearsal', 'nonce':uuid.uuid4().hex}) + '\n').encode()
        identity = None; stop_attempted = False; marker_removed = False
        try:
            identity = create_marker(parent, contents)
            stop_attempted = True
            run(['/usr/bin/systemctl', 'stop', SERVICE])
            state = service_state()
            if state.get('ActiveState') != 'inactive' or state.get('MainPID') != '0':
                raise ValueError('backend did not stop')
            if not base.no_scheduler_jobs() or not base.no_active_platform_jobs():
                raise ValueError('jobs appeared during maintenance entry')
            # Condition-skipped start can exit successfully: explicitly verify
            # that no backend process was started while the marker exists.
            run(['/usr/bin/systemctl', 'start', SERVICE])
            state = service_state()
            if state.get('ActiveState') != 'inactive' or state.get('MainPID') != '0':
                raise ValueError('maintenance condition failed to block backend startup')
            print('PASS: durable marker blocked real backend startup', flush=True)
            remove_marker(parent, identity, contents); marker_removed = True
            run(['/usr/bin/systemctl', 'start', SERVICE])
            base.health(True)
            if not base.no_scheduler_jobs(): raise ValueError('scheduler changed during rehearsal')
            print('MAINTENANCE_REHEARSAL_OK backend_healthy=true marker_absent=true', flush=True)
        except BaseException:
            try:
                if identity is None and (MARKER.exists() or MARKER.is_symlink()):
                    raise ValueError('unexpected or partial maintenance marker requires inspection')
                if identity is not None and not marker_removed:
                    remove_marker(parent, identity, contents); marker_removed = True
                if stop_attempted:
                    run(['/usr/bin/systemctl', 'start', SERVICE])
                    base.health(True)
                print('RECOVERY: previous backend active; marker removed', file=sys.stderr, flush=True)
            except BaseException as recovery:
                print('RECOVERY FAILED; inspect backend and maintenance marker: ' + str(recovery), file=sys.stderr, flush=True)
            raise
        finally:
            os.close(parent)


if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('usage: rehearse-u20-maintenance-fence.py ROOT_OWNED_INPUT_DIR')
    def interrupted(_sig, _frame): raise RuntimeError('rehearsal interrupted')
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGHUP, interrupted)
    try: main(Path(sys.argv[1]).absolute())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Maintenance rehearsal failed: ' + str(exc), file=sys.stderr); sys.exit(1)
