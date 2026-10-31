#!/usr/bin/env python3
"""On an idle fenced u20, briefly start the candidate and restore stock Slurm.

This does not clear history, enable the backend's monthly UI, or test GPU jobs.
All candidate ledger evidence is retained under the protected snapshot.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import sys
import time
import uuid

EXPECTED = {
    'fence.py': '07e540bc17548df86f4a72050c131814c637c78bcffad9e40cd8f28939b17932',
    'preflight.py': 'd6f21c23e2b0dd86e4ec792f246ec96b8185c58e17884a1d3e9f2af8ea3ec141',
    'base.py': '7611b73148879a4ef831d38a4ceb5384c8cf11239900d0ef751acdde92597978',
    'snapshot.py': 'fab8f321303cf1ffe85b50e39aa3bcd84ad6d0ee9507115a1f2d10aff8985a75',
    'ledger-init.py': '94f5e4f47a062ce7230790db4a0e1730684c17b07a5538ccb571578a6787bfda',
    'anchor-init.py': 'e0c387484ea58904bfdef6a2db56b2ca0bfaaff6b20d60e879fc6402dabcab8b',
    'bridge.py': '30ac4668f6ea6f0a413f4a4a834b27d42c5db069900600058d18da58e2b04a70',
    'unit.conf': 'a1425b14dd252dc8491f6332da2027287992f687f2f7d2d9adc7babb1ff30689',
}
UNIT = Path('/etc/systemd/system/slurmctld.service.d/time-monthly.conf')
STATE = Path('/var/lib/slurm/ctld')
ANCHOR = Path('/var/lib/time-quota-anchor')
CONTROLLER = '/opt/time/slurm19-7a06cfff70a7/sbin/slurmctld'


def load(name, path, data):
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec); module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module


def checked(path, digest):
    for item in reversed((path,) + tuple(path.parents)):
        info = item.lstat(); directory = item != path
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022 or
                (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))):
            raise ValueError('untrusted input: ' + str(item))
    if path.lstat().st_nlink != 1: raise ValueError('unexpected input links')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest: raise ValueError('input digest mismatch: ' + str(path))
    return data


def main(directory):
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('root on u20 required')
    os.umask(0o077)
    contents = {name: checked(directory/name, digest) for name, digest in EXPECTED.items()}
    fence = load('fence', directory/'fence.py', contents['fence.py'])
    preflight = load('preflight', directory/'preflight.py', contents['preflight.py'])
    base = load('base', directory/'base.py', contents['base.py'])
    snapshot = load('snapshot', directory/'snapshot.py', contents['snapshot.py'])
    bridge = load('bridge', directory/'bridge.py', contents['bridge.py'])
    fd = os.open('/run/lock/time-u20-console-upgrade.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        st = os.fstat(lock.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o077:
            raise ValueError('untrusted maintenance lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        preflight.preflight(directory)
        base.trusted_directory(fence.MARKER.parent)
        parent = os.open(str(fence.MARKER.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        marker = (json.dumps({'kind':'candidate-controller-rehearsal', 'nonce':uuid.uuid4().hex})+'\n').encode()
        marker_id = None; backend_stopped = False; controller_stopped = False
        override_installed = False; candidate_start_attempted = False; backup = None
        try:
            marker_id = fence.create_marker(parent, marker)
            backend_stopped = True
            fence.run(['/usr/bin/systemctl', 'stop', fence.SERVICE]); snapshot.inactive(fence.SERVICE)
            if not base.no_scheduler_jobs() or not base.no_active_platform_jobs():
                raise ValueError('jobs appeared during maintenance')
            controller_stopped = True
            fence.run(['/usr/bin/systemctl', 'stop', 'slurmctld.service']); snapshot.inactive('slurmctld.service')
            backup = snapshot.snapshot_locked()
            if os.stat(backup).st_dev != os.stat(STATE).st_dev or os.stat(backup).st_dev != os.stat('/var/lib').st_dev:
                raise ValueError('cannot preserve candidate state by atomic rename across filesystems')
            fence.run(['/usr/sbin/runuser', '-u', 'slurm', '--', '/usr/bin/python3', '-I', str(directory/'ledger-init.py'),
                       '--state-dir', str(STATE), '--new-empty-ledger'])
            fence.run(['/usr/bin/python3', '-I', str(directory/'anchor-init.py'), '--new-empty-witness'])
            unitfd = os.open(UNIT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            override_installed = True
            with os.fdopen(unitfd, 'wb') as out:
                out.write(contents['unit.conf']); out.flush(); os.fchmod(out.fileno(), 0o644); os.fsync(out.fileno())
            fence.run(['/usr/bin/systemctl', 'daemon-reload'])
            candidate_start_attempted = True
            fence.run(['/usr/bin/systemctl', 'start', 'slurmctld.service'])
            status = fence.run(['/usr/bin/systemctl', 'show', 'slurmctld.service', '-p', 'MainPID', '--value'])
            pid = int(status)
            if pid <= 0 or os.readlink('/proc/%d/exe' % pid) != CONTROLLER:
                raise ValueError('candidate controller executable mismatch')
            deadline = time.monotonic() + 20; last = None
            while time.monotonic() < deadline:
                try:
                    value = bridge.ControllerBridge(str(STATE/'time-quota.sock'), pwd.getpwnam('slurm').pw_uid).snapshot(pwd.getpwnam('timzhang').pw_uid)
                    break
                except bridge.ControllerUnavailable as exc:
                    last = exc; time.sleep(.2)
            else: raise ValueError('candidate quota socket unavailable: ' + str(last))
            if (value['tier'] != 'debug' or value['used']['gpu_seconds'] != 0 or
                    value['capacity']['gpu_seconds'] != 605*3600 or value['active_allocation_count'] != 0):
                raise ValueError('candidate fresh quota snapshot inconsistent')
            if not base.no_scheduler_jobs(): raise ValueError('unexpected job during candidate rehearsal')
            print('PASS: systemd candidate started and returned fresh quota snapshot', flush=True)
        finally:
            try:
                if candidate_start_attempted:
                    fence.run(['/usr/bin/systemctl', 'stop', 'slurmctld.service']); snapshot.inactive('slurmctld.service')
                if override_installed:
                    if hashlib.sha256(UNIT.read_bytes()).hexdigest() != EXPECTED['unit.conf']:
                        raise ValueError('candidate override changed; manual recovery required')
                    UNIT.unlink(); fence.run(['/usr/bin/systemctl', 'daemon-reload'])
                if backup is not None:
                    archive = backup/'candidate-probe'; archive.mkdir(mode=0o700)
                    for name in ('monthly-quota.wal', 'monthly-quota.checkpoint'):
                        source = STATE/name
                        if source.exists() or source.is_symlink(): os.rename(source, archive/name)
                    if ANCHOR.exists() or ANCHOR.is_symlink(): os.rename(ANCHOR, archive/'anchor')
                    print('Candidate state preserved: ' + str(archive), flush=True)
                if controller_stopped:
                    fence.run(['/usr/bin/systemctl', 'start', 'slurmctld.service'])
                    if not base.no_scheduler_jobs(): raise ValueError('scheduler not healthy or jobs appeared')
                if marker_id is not None:
                    fence.remove_marker(parent, marker_id, marker); marker_id = None
                if backend_stopped:
                    fence.run(['/usr/bin/systemctl', 'start', fence.SERVICE]); base.health(True)
                print('RECOVERY_OK previous controller and backend active', flush=True)
            except BaseException as recovery:
                if marker_id is None and backend_stopped:
                    try: marker_id = fence.create_marker(parent, marker)
                    except BaseException as exc: print('Unable to restore marker: ' + str(exc), file=sys.stderr)
                print('RECOVERY FAILED; retain marker and inspect: ' + str(recovery), file=sys.stderr, flush=True)
                raise
            finally: os.close(parent)


if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('usage: rehearse-u20-candidate-controller.py ROOT_OWNED_INPUT_DIR')
    def interrupted(_sig, _frame): raise RuntimeError('candidate rehearsal interrupted')
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGHUP, interrupted)
    try: main(Path(sys.argv[1]).absolute())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Candidate rehearsal failed: ' + str(exc), file=sys.stderr); sys.exit(1)
