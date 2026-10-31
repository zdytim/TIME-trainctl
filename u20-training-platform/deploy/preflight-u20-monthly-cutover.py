#!/usr/bin/env python3
"""Read-only, point-in-time preflight; does not authorize or perform cutover."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

PINNED = {
    '/etc/default/slurmctld': '3ee8ddde1357e4e792365b0c81893bd8872b31e5b716c399ca8d7764eb1f7779',
    '/usr/lib/systemd/system/slurmctld.service': 'fb9afb4499856b799022398256dcf0056fc009da3d81aea2c00f87a920cfee55',
    '/etc/systemd/system/slurmctld.service.d/platform.conf': '29397747f41b8d4c96aaa03736197a7342cf405bfe4f70a291c07de7ed9bb889',
    '/etc/slurm-llnl/slurm.conf': '6c6072b71804357a06c270f883981f95b9b2ae4ce35913026b42956d69172c63',
    '/etc/slurm-llnl/time-controller-7a06.conf': '8ce457b2deec7c42438a06db2360d01163e681dea351d05a68bdacc8ab658411',
    '/usr/local/libexec/time-quota-anchor-7a06': 'b705fcb6c16739bd6b283f137399c66cbc0ea642d1e0155110efd4ef8c854ba8',
    '/usr/local/lib/pro6000d-training/pro6000d-training-api.py': '24afd69cff5eac856f2ccaaf8a97b95c1823c911c83f40338745cd4ab813e821',
}
STAGE_SHA = '716acf3321837d6c4057062d2b873b864698ce9146163733ac79f5732869d29d'
UNIT_SHA = 'a1425b14dd252dc8491f6332da2027287992f687f2f7d2d9adc7babb1ff30689'
TERMINAL = ('COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY',
            'NODE_FAIL', 'BOOT_FAIL', 'DEADLINE', 'PREEMPTED', 'EXPIRED')


def checked_file(path, sha=None):
    path = Path(path)
    for item in reversed((path,) + tuple(path.parents)):
        st = item.lstat()
        directory = item != path
        if (stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_gid != 0 or st.st_mode & 0o022 or
                (not stat.S_ISDIR(st.st_mode) if directory else not stat.S_ISREG(st.st_mode))):
            raise ValueError('untrusted path: ' + str(item))
    if path.lstat().st_nlink != 1: raise ValueError('unexpected link count: ' + str(path))
    data = path.read_bytes()
    if sha and hashlib.sha256(data).hexdigest() != sha: raise ValueError('unexpected digest: ' + str(path))
    return data


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15).stdout.strip()


def preflight(input_dir):
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('expected u20 host and root required')
    for path, sha in PINNED.items(): checked_file(path, sha)
    stage = checked_file(input_dir / 'stage.py', STAGE_SHA)
    checked_file(input_dir / 'unit.conf', UNIT_SHA)
    spec = importlib.util.spec_from_loader('time_stage_verify', loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(stage, str(input_dir / 'stage.py'), 'exec'), module.__dict__)
    module.verify_release()
    for path in ('/var/lib/slurm/ctld/monthly-quota.wal', '/var/lib/slurm/ctld/monthly-quota.checkpoint',
                 '/var/lib/time-quota-anchor', '/etc/systemd/system/slurmctld.service.d/time-monthly.conf'):
        p = Path(path)
        if p.exists() or p.is_symlink(): raise ValueError('prior quota state or override exists: ' + path)
    for service in ('slurmctld', 'slurmd', 'pro6000d-training'):
        if run(['/usr/bin/systemctl', 'is-active', service]) != 'active':
            raise ValueError('service is not active: ' + service)
    unit = run(['/usr/bin/systemctl', 'show', 'slurmctld', '-p', 'MainPID', '-p', 'FragmentPath', '-p', 'DropInPaths'])
    fields = dict(line.split('=', 1) for line in unit.splitlines() if '=' in line)
    if (fields.get('FragmentPath') != '/lib/systemd/system/slurmctld.service' or
            fields.get('DropInPaths') != '/etc/systemd/system/slurmctld.service.d/platform.conf'):
        raise ValueError('unexpected controller unit or drop-in')
    pid = int(fields.get('MainPID', '0'))
    if pid <= 0 or os.readlink('/proc/%d/exe' % pid) != '/usr/sbin/slurmctld-wlm':
        raise ValueError('unexpected running controller executable')
    current = run(['/usr/bin/scontrol', 'show', 'config'])
    values = dict(line.split('=', 1) for line in current.splitlines() if '=' in line)
    values = {k.strip(): v.strip() for k, v in values.items()}
    if (values.get('SlurmctldParameters') != '(null)' or values.get('TaskPlugin') != 'task/cgroup' or
            values.get('ProctrackType') != 'proctrack/cgroup'):
        raise ValueError('unexpected active scheduler or device-isolation configuration')
    if run(['/usr/bin/squeue', '-h', '-o', '%i']): raise ValueError('Slurm jobs exist')
    config = json.loads(checked_file('/etc/pro6000d-training/config.json'))
    monthly = config.get('monthly_controller', {})
    if (config.get('state_db') != '/var/lib/pro6000d-training/state.sqlite3' or
            not isinstance(monthly, dict) or any(monthly.get(k) is True for k in
            ('read_enabled', 'grant_enabled', 'activation_verified'))):
        raise ValueError('unexpected platform config or already-enabled monthly controller')
    checked_file('/var/lib/pro6000d-training/state.sqlite3')
    uri = 'file:/var/lib/pro6000d-training/state.sqlite3?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        marks = ','.join('?' for _ in TERMINAL)
        if db.execute('SELECT COUNT(*) FROM jobs WHERE state IS NULL OR state NOT IN (' + marks + ') OR finished_at IS NULL OR temporary_application_id IS NOT NULL', TERMINAL).fetchone()[0]:
            raise ValueError('active, ambiguous or approval-linked platform jobs exist')
        if db.execute('SELECT COUNT(*) FROM applications WHERE bound_job_id IS NOT NULL').fetchone()[0]:
            raise ValueError('approval bound to a job')
        if db.execute("SELECT COUNT(*) FROM monthly_grant_outbox WHERE state IS NULL OR state!='APPLIED'").fetchone()[0]:
            raise ValueError('unresolved monthly grant exists')
        if db.execute('SELECT COUNT(*) FROM job_request_tombstones').fetchone()[0]:
            raise ValueError('existing history tombstones need reconciliation')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise ValueError('database check failed')
        jobs = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
    if run(['/usr/bin/squeue', '-h', '-o', '%i']): raise ValueError('Slurm queue changed during preflight')
    return {'preflight': 'passed', 'controller_pid': pid, 'terminal_history_jobs': jobs,
            'fenced': False, 'activated': False,
            'note': 'point-in-time read-only check; all state must be rechecked under the cutover fence'}


if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('usage: preflight-u20-monthly-cutover.py ROOT_OWNED_INPUT_DIR')
    try: print(json.dumps(preflight(Path(sys.argv[1]).absolute()), sort_keys=True))
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print('Preflight refused: ' + str(exc), file=sys.stderr); sys.exit(1)
