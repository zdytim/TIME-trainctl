#!/usr/bin/env python3
"""Cut over an idle u20 to the monthly controller, keeping UI staged.

This clears only verified old platform history and matching jobcomp records.
After submissions are reopened, failures never trigger an old-data restore.
Separate live GPU checks and a later reviewed UI enablement remain required.
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
    'purge-core.py': '2cca11c8834204bba72dfaaf4fd4350812fe1051a60f3ea3478c81789f2333e9',
    'purge-guard.py': '4da681fb59e721fe4d5045e206e2621c2ee66afed69d9dad4451ab502f7c7be4',
    'log-guard.py': '01899a74911c42e8bab02677fcd66083341b09ee73c8c9286575206bf578f5d3',
    'history.py': 'ceb89acc9c06c83b3ca461932610707650bd02f8a55689e1247ab2eee49ed4e4',
    'history-rollback.py': 'af15145ce2af026eb1dd7af7193b5810d334cdf80d0dc0f6eb9751c1433a393d',
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
    core = load('core', directory/'purge-core.py', contents['purge-core.py'])
    guard = load('guard', directory/'purge-guard.py', contents['purge-guard.py'])
    log = load('log', directory/'log-guard.py', contents['log-guard.py'])
    history = load('history', directory/'history.py', contents['history.py'])
    rollback = load('history_rollback', directory/'history-rollback.py', contents['history-rollback.py'])
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
        override_installed = False; candidate_start_attempted = False; backup = None; exposed = False
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
            result = history.apply(backup, snapshot, guard, core, log)
            print('History migrated: %s jobs, %s jobcomp records' %
                  (result['deleted_jobs'], result['cleared_jobcomp_records']), flush=True)
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
            print('PASS: candidate started and returned fresh quota snapshot', flush=True)
            # Persist the point beyond which new work can enter. From this
            # point no automatic rollback to the pre-cutover data is safe.
            history.journal_append(backup/'cutover.jsonl', {'phase':'opening_submissions'}, create=True)
            exposed = True
            fence.remove_marker(parent, marker_id, marker); marker_id = None
            fence.run(['/usr/bin/systemctl', 'start', fence.SERVICE]); base.health(True)
            history.journal_append(backup/'cutover.jsonl', {'phase':'backend_staged_healthy'})
            print('CUTOVER_STAGED_OK candidate active; UI remains staged; backup=' + str(backup), flush=True)
        except BaseException:
            try:
                if exposed:
                    # Existing or newly submitted jobs must remain on the new
                    # controller and their accounting must never be overwritten.
                    if marker_id is None:
                        marker_id = fence.create_marker(parent, marker)
                    fence.run(['/usr/bin/systemctl', 'stop', fence.SERVICE])
                    print('CUTOVER NEEDS REVIEW: candidate and ledger retained; backend fenced', file=sys.stderr, flush=True)
                    raise
                if candidate_start_attempted:
                    fence.run(['/usr/bin/systemctl', 'stop', 'slurmctld.service']); snapshot.inactive('slurmctld.service')
                if override_installed:
                    if hashlib.sha256(UNIT.read_bytes()).hexdigest() != EXPECTED['unit.conf']:
                        raise ValueError('candidate override changed; manual recovery required')
                    UNIT.unlink(); fence.run(['/usr/bin/systemctl', 'daemon-reload'])
                if backup is not None and (backup/'history-migration.jsonl').exists():
                    rollback.restore(backup, snapshot, core, guard, history)
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
            raise
        finally: os.close(parent)


if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('usage: activate-u20-monthly.py ROOT_OWNED_INPUT_DIR')
    def interrupted(_sig, _frame): raise RuntimeError('monthly cutover interrupted')
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGHUP, interrupted)
    try: main(Path(sys.argv[1]).absolute())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Monthly cutover failed: ' + str(exc), file=sys.stderr); sys.exit(1)
