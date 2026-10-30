#!/usr/bin/env python3
"""Exercise recovery inspection on disposable container-only files."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

assert os.environ.get('TIME_ISOLATED_VALIDATION') == '1' and Path('/.dockerenv').is_file()
assert os.geteuid() == 0
script = sys.argv[1]
base = Path('/tmp/recovery-inspect-fixture')
state, anchor = base / 'state', base / 'anchor'
state.mkdir(parents=True, mode=0o700)
anchor.mkdir(mode=0o700)
os.chown(state, 64030, 64030)
wal = state / 'monthly-quota.wal'
cp = state / 'monthly-quota.checkpoint'
witness = anchor / 'witness.log'
header = b'U20MQ1|HEADER|1\n'
digest = hashlib.sha256(header).hexdigest().encode()
wal.write_bytes(header)
cp.write_bytes(b'U20MQ1|CHECKPOINT|0|' + digest + b'\n')
witness.write_bytes(b'U20MQA1|0|' + digest + b'\n')
for path in (wal, cp):
    path.chmod(0o600)
    os.chown(path, 64030, 64030)
witness.chmod(0o600)

def call():
    return subprocess.run([sys.executable, script, '--state-dir', str(state), '--witness-dir', str(anchor)],
                          capture_output=True, text=True, timeout=10)

result = call()
assert result.returncode == 0 and json.loads(result.stdout)['matching'], result.stderr
before = {p: p.read_bytes() for p in (wal, cp, witness)}
with witness.open('rb') as fd:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert call().returncode == 1
assert all(p.read_bytes() == before[p] for p in before)
witness.write_bytes(b'U20MQA1|0|' + digest + b'\nU20MQA1|1|' + b'a' * 64 + b'\n')
result = call()
assert result.returncode == 2 and not json.loads(result.stdout)['matching']
witness.write_bytes(b'U20MQA1|0|' + digest + b'\nU20MQA1|2|' + b'a' * 64 + b'\n')
assert call().returncode == 1
witness.unlink()
witness.symlink_to(wal)
assert call().returncode == 1
print('PASS: matching inventory, held lock, mismatch, malformed witness and symlink refusal')
