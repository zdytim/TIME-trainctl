#!/usr/bin/env python3
"""Briefly fence an idle u20 and capture a stopped snapshot, then restore service.

This does not activate a new controller, delete history or restore a backup.
The marker remains in place if the existing services cannot be restored.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import uuid

INPUTS = {
    'rehearsal.py': '07e540bc17548df86f4a72050c131814c637c78bcffad9e40cd8f28939b17932',
    'preflight.py': 'd6f21c23e2b0dd86e4ec792f246ec96b8185c58e17884a1d3e9f2af8ea3ec141',
    'base.py': '7611b73148879a4ef831d38a4ceb5384c8cf11239900d0ef751acdde92597978',
    'snapshot.py': 'fab8f321303cf1ffe85b50e39aa3bcd84ad6d0ee9507115a1f2d10aff8985a75',
}


def main(directory):
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('root and the expected u20 host are required')
    # Authenticate the bootstrap module itself without importing user-owned code.
    import hashlib
    for item in reversed((directory,) + tuple(directory.parents)):
        st = item.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_gid != 0 or st.st_mode & 0o022:
            raise ValueError('untrusted input directory: ' + str(item))
    path = directory / 'rehearsal.py'; st = path.lstat()
    data = path.read_bytes()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_gid != 0 or
            st.st_mode & 0o022 or st.st_nlink != 1 or hashlib.sha256(data).hexdigest() != INPUTS['rehearsal.py']):
        raise ValueError('untrusted rehearsal input')
    import importlib.util
    spec = importlib.util.spec_from_loader('time_rehearsal', loader=None)
    rehearsal = importlib.util.module_from_spec(spec)
    exec(compile(data, str(path), 'exec'), rehearsal.__dict__)
    loaded = {name: rehearsal.checked(directory / name, sha) for name, sha in INPUTS.items()}
    preflight = rehearsal.load('time_preflight', directory / 'preflight.py', loaded['preflight.py'])
    base = rehearsal.load('time_base', directory / 'base.py', loaded['base.py'])
    snapshot = rehearsal.load('time_snapshot', directory / 'snapshot.py', loaded['snapshot.py'])
    lockfd = os.open('/run/lock/time-u20-console-upgrade.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, 'a') as lock:
        st = os.fstat(lock.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o077:
            raise ValueError('untrusted maintenance lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        preflight.preflight(directory)
        base.trusted_directory(rehearsal.MARKER.parent)
        if rehearsal.MARKER.parent.stat().st_mode & 0o077:
            raise ValueError('marker parent must be private')
        parent = os.open(str(rehearsal.MARKER.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        content = (json.dumps({'kind': 'stopped-snapshot-rehearsal', 'nonce': uuid.uuid4().hex}) + '\n').encode()
        identity = None
        controller_stop_attempted = backend_stop_attempted = False
        try:
            identity = rehearsal.create_marker(parent, content)
            backend_stop_attempted = True
            rehearsal.run(['/usr/bin/systemctl', 'stop', rehearsal.SERVICE])
            snapshot.inactive(rehearsal.SERVICE)
            if not base.no_scheduler_jobs() or not base.no_active_platform_jobs():
                raise ValueError('jobs appeared during maintenance entry')
            controller_stop_attempted = True
            rehearsal.run(['/usr/bin/systemctl', 'stop', 'slurmctld.service'])
            snapshot.inactive('slurmctld.service')
            snapshot.snapshot_locked()
        finally:
            try:
                if controller_stop_attempted:
                    rehearsal.run(['/usr/bin/systemctl', 'start', 'slurmctld.service'])
                    if not base.no_scheduler_jobs(): raise ValueError('scheduler not healthy or jobs appeared')
                if identity is not None:
                    rehearsal.remove_marker(parent, identity, content)
                    identity = None
                if backend_stop_attempted:
                    rehearsal.run(['/usr/bin/systemctl', 'start', rehearsal.SERVICE])
                    base.health(True)
                print('RECOVERY_OK previous controller and backend active', flush=True)
            except BaseException as recovery:
                if identity is None and backend_stop_attempted:
                    try:
                        identity = rehearsal.create_marker(parent, content)
                    except BaseException as marker_error:
                        print('Unable to restore maintenance marker: ' + str(marker_error), file=sys.stderr, flush=True)
                print('RECOVERY FAILED; inspect services and marker: ' + str(recovery), file=sys.stderr, flush=True)
                raise
            finally:
                os.close(parent)


if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('usage: rehearse-u20-stopped-snapshot.py ROOT_OWNED_INPUT_DIR')
    def interrupted(_sig, _frame): raise RuntimeError('snapshot rehearsal interrupted')
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGHUP, interrupted)
    try: main(Path(sys.argv[1]).absolute())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Stopped snapshot rehearsal failed: ' + str(exc), file=sys.stderr); sys.exit(1)
