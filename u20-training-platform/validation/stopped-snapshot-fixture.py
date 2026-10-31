#!/usr/bin/env python3
"""Exercise snapshot and independent reconstruction in an isolated container."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import types

assert Path('/.dockerenv').is_file()
assert os.geteuid() == 0 and os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
spec = importlib.util.spec_from_file_location('snapshot', '/fixture/snapshot.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
base = Path('/var/backups/time-snapshot-fixture'); base.mkdir(mode=0o700)
m.ROOT = base / 'backups'; m.ROOT.mkdir(mode=0o700)
m.MARKER = base / 'marker'; m.MARKER.write_text('fixture\n'); m.MARKER.chmod(0o600)
m.STATE = base / 'state'; m.STATE.mkdir(mode=0o750)
(m.STATE / 'hash.1').mkdir(mode=0o750)
(m.STATE / 'job_state').write_bytes(b'job sequence and state')
(m.STATE / 'hash.1' / 'script').write_bytes(b'private fixture job')
m.JOBCOMP = base / 'jobcomp'; m.JOBCOMP.write_text('completed fixture\n')
m.CONFIGS = tuple(base / ('config%d' % i) for i in range(3))
for i, item in enumerate(m.CONFIGS): item.write_text('fixture config %d\n' % i)
m.DB = base / 'database';
with sqlite3.connect(m.DB) as db:
    db.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, state TEXT)')
    db.execute('INSERT INTO jobs VALUES (11, ?)', ('COMPLETED',))
m.DB.chmod(0o600)
m.os.uname = lambda: types.SimpleNamespace(nodename='u20-Z790-S-DDR4')
states = {'slurmctld.service': 'inactive', 'pro6000d-training.service': 'inactive'}
def inactive(name):
    if states[name] != 'inactive': raise ValueError('fixture service active')
m.inactive = inactive

m.main()
snapshots = list(m.ROOT.glob('cutover-stopped-*'))
assert len(snapshots) == 1
folder = snapshots[0]; manifest = json.loads((folder / 'manifest.json').read_text())
for relative, entry in manifest['state'].items():
    if entry['type'] == 'file':
        assert hashlib.sha256((folder / 'slurm-state' / relative).read_bytes()).hexdigest() == entry['sha256']
for name, entry in manifest['files'].items():
    assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == entry['sha256']
dbcopy = folder / 'state.sqlite3'
assert hashlib.sha256(dbcopy.read_bytes()).hexdigest() == manifest['database']['sha256']
with sqlite3.connect('file:' + str(dbcopy) + '?mode=ro', uri=True) as db:
    assert db.execute('SELECT id,state FROM jobs').fetchall() == [(11, 'COMPLETED')]
# Reconstruct into private scratch, never the original paths.
restore = base / 'restore'; restore.mkdir(mode=0o700)
for relative, entry in manifest['state'].items():
    path = restore / relative
    if entry['type'] == 'directory': path.mkdir(parents=True, exist_ok=True)
    else: path.write_bytes((folder / 'slurm-state' / relative).read_bytes())
assert (restore / 'job_state').read_bytes() == (m.STATE / 'job_state').read_bytes()
assert (restore / 'hash.1' / 'script').read_bytes() == (m.STATE / 'hash.1' / 'script').read_bytes()
states['slurmctld.service'] = 'active'
try: m.main()
except ValueError: pass
else: raise AssertionError('active controller accepted')
assert len(list(m.ROOT.glob('cutover-stopped-*'))) == 1
states['slurmctld.service'] = 'inactive'
(m.STATE / 'unsafe').symlink_to('/etc/passwd')
try: m.main()
except ValueError: pass
else: raise AssertionError('symlink accepted')
partial = [p for p in m.ROOT.glob('cutover-stopped-*') if p != folder]
assert len(partial) == 1 and not (partial[0] / 'manifest.json').exists()
print('PASS: isolated complete snapshot, independent reconstruction, active-service refusal and partial snapshot handling')
