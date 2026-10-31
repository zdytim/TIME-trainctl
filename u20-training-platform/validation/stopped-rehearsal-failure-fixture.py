#!/usr/bin/env python3
"""Exercise rehearsal error paths with simulated services, only in a container."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import types

assert Path('/.dockerenv').is_file()
assert os.geteuid() == 0 and os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
spec = importlib.util.spec_from_file_location('candidate', '/fixture/rehearse.py')
candidate = importlib.util.module_from_spec(spec); spec.loader.exec_module(candidate)
candidate.os.uname = lambda: types.SimpleNamespace(nodename='u20-Z790-S-DDR4')
root = Path(tempfile.mkdtemp(prefix='time-rehearsal-fixture-', dir='/var/backups'))
marker = root / 'marker'
state = types.ModuleType('time_fixture_state')
state.failure = ''; state.controller = True; state.backend = True; state.snapshot = False; state.marker = marker
import sys
sys.modules['time_fixture_state'] = state

files = {
'rehearsal.py': '''
import importlib.util, os
from pathlib import Path
import time_fixture_state as s
MARKER = s.marker
SERVICE = 'pro6000d-training.service'
def checked(path, sha): return path.read_bytes()
def load(name,path,data):
    spec=importlib.util.spec_from_loader(name,loader=None); obj=importlib.util.module_from_spec(spec)
    exec(compile(data,str(path),'exec'),obj.__dict__); return obj
def create_marker(parent, content):
    if MARKER.exists(): raise ValueError('marker exists')
    MARKER.write_bytes(content); MARKER.chmod(0o600); return 1
def remove_marker(parent, identity, content):
    assert identity==1 and MARKER.read_bytes()==content; MARKER.unlink()
def run(args):
    op, service = args[-2:]
    if (op,service)==('stop',SERVICE): s.backend=False
    elif (op,service)==('stop','slurmctld.service'):
        if s.failure=='stop-controller': raise RuntimeError('injected stop failure')
        s.controller=False
    elif (op,service)==('start','slurmctld.service'):
        if s.failure=='start-controller': raise RuntimeError('injected controller start failure')
        s.controller=True
    elif (op,service)==('start',SERVICE):
        if s.failure=='start-backend': raise RuntimeError('injected backend start failure')
        s.backend=True
    else: raise AssertionError(args)
''',
'preflight.py': 'def preflight(directory): pass\n',
'base.py': '''
import time_fixture_state as s
def trusted_directory(path): pass
def no_scheduler_jobs(): return s.controller and s.failure!='jobs'
def no_active_platform_jobs(): return True
def health(staged): assert staged and s.backend and s.controller
''',
'snapshot.py': '''
import time_fixture_state as s
def inactive(name):
    assert not (s.controller if name=='slurmctld.service' else s.backend)
def snapshot_locked():
    assert not s.controller and not s.backend and s.marker.exists()
    if s.failure=='snapshot': raise RuntimeError('injected snapshot failure')
    s.snapshot=True
''',
}
for name, text in files.items():
    p=root/name; p.write_text(text); p.chmod(0o600)
candidate.INPUTS = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}
for failure in ('', 'snapshot', 'stop-controller', 'start-controller', 'start-backend', 'jobs'):
    state.failure=failure; state.controller=True; state.backend=True; state.snapshot=False
    marker.unlink(missing_ok=True)
    raised = False
    try: candidate.main(root)
    except (RuntimeError, ValueError): raised=True
    assert raised == (failure != ''), failure
    if failure in ('start-controller', 'start-backend'):
        assert marker.exists(), failure
    else:
        assert not marker.exists() and state.controller and state.backend, failure
    if not failure: assert state.snapshot
    print('PASS: simulated rehearsal ' + (failure or 'success'))
