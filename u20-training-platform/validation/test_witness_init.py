#!/usr/bin/env python3
"""Exercise fresh witness initialization using only private container tmpfs."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

assert os.geteuid() == 0 and os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
assert Path('/.dockerenv').is_file()
for path in ('/var/lib/slurm', '/var/lib/time-quota-anchor'):
    assert subprocess.check_output(['stat', '-f', '-c', '%T', path], text=True).strip() == 'tmpfs'
state = Path('/var/lib/slurm/ctld')
state.mkdir(mode=0o700)
os.chown(state, 64030, 64030)
wal = state / 'monthly-quota.wal'
checkpoint = state / 'monthly-quota.checkpoint'
header = b'U20MQ1|HEADER|1\n'
digest = hashlib.sha256(header).hexdigest().encode()
for path, content in ((wal, header), (checkpoint, b'U20MQ1|CHECKPOINT|0|' + digest + b'\n')):
    path.write_bytes(content)
    os.chmod(path, 0o600)
    os.chown(path, 64030, 64030)
tool = '/validation/time-quota-anchor-init.py'
def call(*args):
    return subprocess.run([sys.executable, tool, *args], text=True, capture_output=True, timeout=10)

assert call().returncode != 0
assert call('--new-empty-witness').returncode == 0
witness = Path('/var/lib/time-quota-anchor/witness.log')
assert witness.read_bytes() == b'U20MQA1|0|' + digest + b'\n'
assert witness.stat().st_uid == 0 and witness.stat().st_mode & 0o777 == 0o600
assert call('--new-empty-witness').returncode != 0
assert subprocess.run(['runuser', '-u', 'timefixture', '--', 'test', '-r', str(witness)]).returncode != 0
witness.unlink()  # This is a disposable container-private tmpfs.
wal.write_bytes(header + b'U20MQ1|1|bogus\n')
assert call('--new-empty-witness').returncode != 0
assert not witness.exists()
print('PASS: fresh witness, no overwrite, private access and nonempty-ledger refusal')
