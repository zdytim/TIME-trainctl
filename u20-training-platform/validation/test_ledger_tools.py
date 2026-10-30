#!/usr/bin/env python3
"""Exercise ledger initialization and inspection only in a disposable container."""
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import sys

assert os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
assert pathlib.Path('/.dockerenv').is_file()
assert os.geteuid() == 0
init, inspect = sys.argv[1:]
base = pathlib.Path('/run/ledger-tool-check')
base.mkdir(mode=0o700)
os.chown(base, 64030, 64030)

def service_user():
    os.setgroups([])
    os.setgid(64030)
    os.setuid(64030)

def call(script, *args):
    return subprocess.run([sys.executable, script, *map(str, args)], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          preexec_fn=service_user, timeout=10)

state = base / 'state'
state.mkdir(mode=0o700)
os.chown(state, 64030, 64030)
result = call(init, '--state-dir', state)
assert result.returncode != 0 and not list(state.iterdir()), result.stderr
result = call(init, '--state-dir', state, '--new-empty-ledger')
assert result.returncode == 0, result.stderr
wal = state / 'monthly-quota.wal'
checkpoint = state / 'monthly-quota.checkpoint'
assert wal.read_bytes() == b'U20MQ1|HEADER|1\n'
assert checkpoint.read_bytes() == b'U20MQ1|CHECKPOINT|0|' + hashlib.sha256(wal.read_bytes()).hexdigest().encode() + b'\n'
assert all(p.stat().st_uid == 64030 and p.stat().st_mode & 0o777 == 0o600
           for p in (wal, checkpoint))
result = call(init, '--state-dir', state, '--new-empty-ledger')
assert result.returncode != 0 and wal.read_bytes() == b'U20MQ1|HEADER|1\n'
result = call(inspect, '--state-dir', state)
assert result.returncode == 0, result.stderr
info = json.loads(result.stdout)
assert {x['name'] for x in info['files']} == {wal.name, checkpoint.name}
assert info['recovery_readiness'] == 'not assessed'
with wal.open('rb') as locked:
    fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    result = call(inspect, '--state-dir', state)
    assert result.returncode != 0 and not result.stdout

unsafe = base / 'unsafe'
unsafe.mkdir(mode=0o777)
os.chown(unsafe, 64030, 64030)
unsafe.chmod(0o777)
result = call(init, '--state-dir', unsafe, '--new-empty-ledger')
assert result.returncode != 0 and not list(unsafe.iterdir())
link = base / 'link'
link.symlink_to(state, target_is_directory=True)
result = call(init, '--state-dir', link, '--new-empty-ledger')
assert result.returncode != 0
print('PASS: fresh private ledger, overwrite rejection, offline lock and unsafe paths')
