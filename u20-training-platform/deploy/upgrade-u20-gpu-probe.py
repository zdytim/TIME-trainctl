#!/usr/bin/env python3
"""Install the pinned non-root GPU probe on u20; preserve the live database."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import sys
import tempfile

OLD = '7372e1d9ad73f6899aa3f2e79e24aae594e42717ba941a6044ddfb31831ea55c'
NEW = 'a50efdf27a153d150440907bc728b5cc2e446fddcb408bed4532f26fae0129c8'
BASE = '7611b73148879a4ef831d38a4ceb5384c8cf11239900d0ef751acdde92597978'
ACL = 'f21d51f036ae052b675327cc5fc81b26742d7dceb87f1a34a7574555ef504fe9'
BROKER = Path('/usr/local/lib/pro6000d-training/pro6000d-training-api.py')
CONFIG = Path('/etc/pro6000d-training/config.json')
HELPER = Path('/usr/local/libexec/pro6000d-gpu-acl')


def sha(data): return hashlib.sha256(data).hexdigest()


def updated_config(raw, username, uid):
    value = json.loads(raw)
    if not isinstance(value, dict) or uid <= 0 or not username:
        raise ValueError('non-root GPU probe account required')
    if value.get('gpu_probe_user', username) != username:
        raise ValueError('refusing to overwrite an existing probe account')
    settings = value.get('monthly_controller', {})
    if not isinstance(settings, dict) or any(settings.get(k) is True for k in ('read_enabled', 'grant_enabled', 'activation_verified')):
        raise ValueError('monthly controller must remain disabled')
    value['gpu_probe_user'] = username
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def atomic(path, data, mode):
    fd, name = tempfile.mkstemp(prefix='.time-probe-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fchmod(out.fileno(), mode); os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def restore_known(path, old, new, mode):
    current = path.read_bytes()
    if current == old: return
    if current != new: raise ValueError('refusing to overwrite unexpected file: ' + str(path))
    atomic(path, old, mode)


def main(base, candidate_path):
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('root and the expected u20 host are required')
    for directory in (BROKER.parent, CONFIG.parent, base.BACKUPS, candidate_path.parent):
        base.trusted_directory(directory)
    old_broker = base.trusted_file(BROKER, OLD)
    new_broker = base.trusted_file(candidate_path, NEW)
    old_config = base.trusted_file(CONFIG)
    if Path(json.loads(old_config).get('state_db', str(base.DATABASE))) != base.DATABASE:
        raise ValueError('unexpected database path')
    base.trusted_file(HELPER, ACL)
    user = pwd.getpwnam('u20')
    new_config = updated_config(old_config, user.pw_name, user.pw_uid)
    if not base.no_scheduler_jobs() or not base.no_active_platform_jobs():
        raise ValueError('active or unconfirmed jobs; upgrade refused')
    backup = Path(tempfile.mkdtemp(prefix='u20-gpu-probe-', dir=str(base.BACKUPS)))
    atomic(backup/'broker.before', old_broker, 0o600)
    atomic(backup/'config.before', old_config, 0o600)
    record = {'status':'preparing', 'rollback':'not required'}
    def save(): atomic(backup/'result.json', (json.dumps(record, indent=2)+'\n').encode(), 0o600)
    save()
    stopped = False
    try:
        base.run(['/usr/bin/systemctl','stop',base.SERVICE]); stopped = True
        if not base.no_scheduler_jobs() or not base.no_active_platform_jobs():
            raise ValueError('jobs appeared during shutdown')
        base.backup_database(backup/'state.sqlite3')
        atomic(CONFIG, new_config, 0o640)
        atomic(BROKER, new_broker, 0o644)
        base.run([str(HELPER)])
        base.run(['/usr/bin/systemctl','start',base.SERVICE]); stopped = False
        base.health(True)
        for name in ('u20', 'timzhang'):
            state = base.api(pwd.getpwnam(name).pw_uid, '/api/v1/state')
            if not state['gpus']['available'] or len(state['gpus']['devices']) != 1:
                raise ValueError('GPU monitoring unavailable for ' + name)
        for device in ('/dev/nvidia0', '/dev/nvidiactl', '/dev/nvidia-uvm'):
            acl = base.run(['/usr/bin/getfacl','-cp',device]).stdout.splitlines()
            if 'other::---' not in acl: raise ValueError('GPU device permissions changed: ' + device)
        record['status'] = 'healthy; monthly gate remains disabled'; save()
        print('PROBE_UPGRADE_OK backup=' + str(backup))
    except BaseException as exc:
        record['status'] = 'failed: ' + str(exc)
        try:
            if not stopped: base.run(['/usr/bin/systemctl','stop',base.SERVICE]); stopped = True
            restore_known(BROKER, old_broker, new_broker, 0o644)
            restore_known(CONFIG, old_config, new_config, 0o640)
            base.run(['/usr/bin/systemctl','start',base.SERVICE]); stopped = False
            base.health(False)
            base.run([str(HELPER)])
            record['rollback'] = 'old broker and config restored; database retained'
        except BaseException as rollback_error:
            record['rollback'] = 'FAILED: ' + str(rollback_error)
        save()
        print('PROBE_UPGRADE_FAILED backup=' + str(backup) + ' rollback=' + record['rollback'], flush=True)
        raise


if __name__ == '__main__':
    if len(sys.argv) != 3: raise SystemExit('usage: upgrade-u20-gpu-probe.py BASE_SCRIPT CANDIDATE')
    base_path, candidate_path = map(lambda s: Path(s).absolute(), sys.argv[1:])
    # Only import a root-owned helper after checking its exact committed bytes.
    for directory in reversed((base_path.parent,) + tuple(base_path.parent.parents)):
        st = directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise SystemExit('untrusted base helper directory')
    st = base_path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or
            st.st_mode & 0o022 or sha(base_path.read_bytes()) != BASE):
        raise SystemExit('untrusted base upgrade helper')
    spec = importlib.util.spec_from_file_location('time_upgrade_base', str(base_path))
    base = importlib.util.module_from_spec(spec); spec.loader.exec_module(base)
    def interrupted(_sig, _frame): raise RuntimeError('upgrade interrupted')
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGHUP, interrupted)
    lock_fd = os.open('/run/lock/time-u20-console-upgrade.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC, 0o600)
    st = os.fstat(lock_fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o077:
        raise SystemExit('untrusted upgrade lock')
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        main(base, candidate_path)
