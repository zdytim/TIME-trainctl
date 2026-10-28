#!/usr/bin/env python3
"""Incremental code upgrade only. Monthly enforcement and SPANK stay disabled.

Run locally on the destination as root, after review. Never modifies Slurm,
PAM, accounts, sockets/ports, service definitions, or the plugstack.
"""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time

SERVICE = 'pro6000d-training.service'
CONFIG = Path('/etc/pro6000d-training/config.json')
LIB = Path('/usr/local/lib/pro6000d-training')
CLIENT = Path('/usr/local/bin/pro6000d-training-client')
UI = Path('/usr/share/cockpit/pro6000d-training')


def run(argv, **kwargs):
    timeout = kwargs.pop('timeout', 40)
    return subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, **kwargs)


def replace_file(target, content, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.platform-upgrade-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, target)
    finally:
        if os.path.exists(name): os.unlink(name)


def health(admin, client, expected_gpu, require_monthly=False, timeout=40):
    user = pwd.getpwnam(admin)
    request = json.dumps({'method':'GET', 'path':'/api/v1/state'}) + '\n'
    result = run(['setpriv', '--reuid='+str(user.pw_uid),
                  '--regid='+str(user.pw_gid), '--init-groups', str(client)],
                 input=request, timeout=timeout)
    response = json.loads(result.stdout)
    data = response.get('data', {})
    if not response.get('ok') or not data.get('scheduler_available'):
        raise RuntimeError('Real admin state read / scheduler health failed')
    if len(data.get('gpus', {}).get('devices', [])) != expected_gpu:
        raise RuntimeError('GPU inventory differs from preserved configuration')
    if require_monthly:
        monthly = data.get('monthly_quota')
        if not isinstance(monthly, dict) or monthly.get('enabled') is not False:
            raise RuntimeError('Upgraded API must expose monthly_quota with enabled=false')


def ready_health(admin, client, expected_gpu, require_monthly=False):
    deadline = time.monotonic() + 10
    last_error = 'No response'
    while time.monotonic() < deadline:
        try:
            health(admin, client, expected_gpu, require_monthly,
                   timeout=max(.01, deadline-time.monotonic()))
            return
        except subprocess.CalledProcessError as exc:
            # A client can race RuntimeDirectory/socket creation after restart.
            # Semantic API/GPU/monthly errors from health() are not retried.
            last_error = (exc.stdout or '') + (exc.stderr or '')
        except subprocess.TimeoutExpired:
            last_error = 'State client timed out before the readiness deadline'
        remaining = deadline-time.monotonic()
        if remaining > 0: time.sleep(min(.5, remaining))
    raise RuntimeError('Backend not ready within 10 seconds: '+last_error[-1500:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--admin', required=True, help='Existing administrator: ubuntu or u20')
    args = parser.parse_args()
    if os.geteuid() != 0: raise SystemExit('root required')
    lock = open('/run/lock/pro6000d-platform-upgrade.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    root = Path(__file__).resolve().parent.parent
    source = root/'backend'
    for name in ('pro6000d-training-api.py', 'pro6000d-training-client.py', 'monthly_quota.py'):
        if not (source/name).is_file(): raise SystemExit('Missing source: '+name)
    config = json.loads(CONFIG.read_text())
    monthly = config.get('monthly_quota', {})
    if not isinstance(monthly, dict) or monthly.get('enabled'):
        raise SystemExit('Refusing staged upgrade of already-enabled monthly enforcement')
    if Path('/run/pro6000d-training/gate.sock').exists():
        raise SystemExit('Gate service present; review running permits before backend restart')
    # A separately installed required plugin cannot be inferred reliably from a
    # socket. Require operator review of PlugStackConfig per MONTHLY-GATE.md.
    dbpath = Path(config.get('state_db', '/var/lib/pro6000d-training/state.sqlite3'))
    if not dbpath.is_file(): raise SystemExit('Existing database required')
    health(args.admin, CLIENT, int(config['capacity']['gpu']))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup = Path('/var/backups/pro6000d-training')/('staged-upgrade-'+stamp)
    backup.mkdir(parents=True, mode=0o700)
    manifest = {'complete':False, 'database_restored':False, 'files':[],
                'note':'Database backup is for recovery evidence; rollback preserves live DB.'}
    def save():
        replace_file(backup/'manifest.json', (json.dumps(manifest, indent=2)+'\n').encode(), 0o600)
    def snapshot(path):
        if path.is_symlink(): raise RuntimeError('Refusing symlink target: '+str(path))
        record = {'path':str(path), 'existed':path.exists()}
        if path.exists():
            dest = backup/'files'/str(path).lstrip('/')
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        manifest['files'].append(record)
        save()
    # SQLite's online API includes committed WAL data, unlike copying only .db.
    # Bound retries if another writer prevents completion.
    started = time.monotonic()
    def progress(status, remaining, total):
        if time.monotonic()-started > 40:
            raise RuntimeError('SQLite online backup timed out')
    with sqlite3.connect(str(dbpath)) as live, sqlite3.connect(str(backup/'state.sqlite3')) as copy:
        live.backup(copy, pages=256, progress=progress, sleep=.1)
    os.chmod(backup/'state.sqlite3', 0o600)
    changed = []
    for file in sorted(source.glob('*.py')):
        destination = CLIENT if file.name == 'pro6000d-training-client.py' else LIB/file.name
        changed.append((destination, file.read_bytes(), 0o755 if destination == CLIENT else 0o644))
    ui_source = root/'cockpit-plugin/pro6000d-training'
    if not (ui_source/'manifest.json').is_file(): raise RuntimeError('Missing Cockpit manifest')
    for file in sorted(ui_source.rglob('*')):
        if file.is_symlink(): raise RuntimeError('Unexpected source symlink')
        if file.is_file(): changed.append((UI/file.relative_to(ui_source), file.read_bytes(), 0o644))
    config['monthly_quota'] = {**monthly, 'enabled':False}
    changed.append((CONFIG, (json.dumps(config, indent=2, ensure_ascii=False)+'\n').encode(), 0o640))
    for destination, _, _ in changed: snapshot(destination)
    print('Backup: '+str(backup), flush=True)
    try:
        # Per-file atomic replacement; backend imports only on the later restart.
        # Existing harmless extra UI files are preserved, never recursively deleted.
        for destination, content, mode in changed: replace_file(destination, content, mode)
        run(['systemctl', 'restart', SERVICE])
        run(['systemctl', 'is-active', '--quiet', SERVICE])
        ready_health(args.admin, CLIENT, int(config['capacity']['gpu']), require_monthly=True)
    except Exception:
        for record in reversed(manifest['files']):
            path = Path(record['path'])
            if record['existed']:
                saved = backup/'files'/str(path).lstrip('/')
                replace_file(path, saved.read_bytes(), stat.S_IMODE(saved.stat().st_mode))
            elif path.exists(): path.unlink()
        # Deliberately do not restore database: new committed usage/grants must
        # survive. Schema migrations must remain additive/backward-compatible.
        try:
            run(['systemctl', 'restart', SERVICE])
            ready_health(args.admin, CLIENT, int(config['capacity']['gpu']))
            manifest['rollback'] = 'code restored; old backend healthy; live DB preserved'
        except Exception as rollback_error:
            manifest['rollback'] = 'code restored; old backend health FAILED: '+str(rollback_error)
        save()
        raise
    manifest['complete'] = True
    save()
    print('Code upgraded; monthly enforcement remains disabled. Slurm was not changed.')


if __name__ == '__main__': main()
