#!/usr/bin/env python3
"""Simulate candidate rehearsal failure paths inside a disposable container."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import types

assert Path('/.dockerenv').is_file()
assert os.geteuid() == 0 and os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
spec = importlib.util.spec_from_file_location('candidate', '/fixture/candidate.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
root = Path(tempfile.mkdtemp(prefix='time-candidate-failure-', dir='/var/backups'))
state = types.SimpleNamespace(failure='', backend=True, controller='stock', snapshots=0, marker=None)
unit_data = b'[Service]\n# fixture only\n'
c.EXPECTED = {name: hashlib.sha256(unit_data).hexdigest() for name in c.EXPECTED}
c.checked = lambda path, digest: unit_data
c.os.uname = lambda: types.SimpleNamespace(nodename='u20-Z790-S-DDR4')
c.os.readlink = lambda path: c.CONTROLLER
c.pwd.getpwnam = lambda name: types.SimpleNamespace(pw_uid=64030 if name=='slurm' else 1001)
c.UNIT = root/'time-monthly.conf'; c.STATE = root/'state'; c.ANCHOR = root/'anchor'
c.STATE.mkdir(mode=0o700)

class Fence:
    MARKER = root/'marker'
    SERVICE = 'pro6000d-training.service'
    @staticmethod
    def create_marker(parent, contents):
        assert not Fence.MARKER.exists(); Fence.MARKER.write_bytes(contents); Fence.MARKER.chmod(0o600); return 1
    @staticmethod
    def remove_marker(parent, identity, contents):
        assert identity==1 and Fence.MARKER.read_bytes()==contents; Fence.MARKER.unlink()
    @staticmethod
    def run(args):
        if args[0]=='/usr/sbin/runuser':
            (c.STATE/'monthly-quota.wal').write_text('candidate fixture WAL')
            if state.failure=='ledger': raise RuntimeError('injected ledger failure')
            (c.STATE/'monthly-quota.checkpoint').write_text('fixture checkpoint'); return ''
        if args[0]=='/usr/bin/python3':
            c.ANCHOR.mkdir(mode=0o700); (c.ANCHOR/'witness.log').write_text('fixture witness')
            if state.failure=='anchor': raise RuntimeError('injected anchor failure')
            return ''
        op=args[1]
        if op=='daemon-reload': return ''
        if op=='show': return '123'
        service=args[-1]
        if op=='stop' and service==Fence.SERVICE: state.backend=False
        elif op=='start' and service==Fence.SERVICE:
            if state.failure=='backend-start': raise RuntimeError('injected backend failure')
            state.backend=True
        elif op=='stop' and service=='slurmctld.service':
            if state.failure=='candidate-stop' and state.controller=='candidate': raise RuntimeError('injected stop failure')
            state.controller='stopped'
        elif op=='start' and service=='slurmctld.service':
            if c.UNIT.exists():
                if state.failure=='candidate-start': raise RuntimeError('injected start failure')
                state.controller='candidate'
            else:
                if state.failure=='stock-start': raise RuntimeError('injected stock failure')
                state.controller='stock'
        else: raise AssertionError(args)
        return ''

class Preflight:
    @staticmethod
    def preflight(directory): assert state.controller=='stock' and state.backend

class Base:
    @staticmethod
    def trusted_directory(path): pass
    @staticmethod
    def no_scheduler_jobs(): return state.controller in ('stock','candidate') and state.failure!='jobs'
    @staticmethod
    def no_active_platform_jobs(): return True
    @staticmethod
    def health(staged): assert staged and state.controller=='stock' and state.backend

class Snapshot:
    @staticmethod
    def inactive(service): assert (not state.backend if service==Fence.SERVICE else state.controller=='stopped')
    @staticmethod
    def snapshot_locked():
        if state.failure=='snapshot': raise RuntimeError('injected snapshot failure')
        assert not state.backend and state.controller=='stopped'
        state.snapshots += 1; p=root/('backup%d'%state.snapshots); p.mkdir(mode=0o700); return p

class Bridge:
    class ControllerUnavailable(Exception): pass
    class ControllerBridge:
        def __init__(self, path, uid): pass
        def snapshot(self, uid):
            if state.failure=='bad-quota': return {'tier':'exhausted','used':{'gpu_seconds':0},'capacity':{'gpu_seconds':0},'active_allocation_count':0}
            return {'tier':'debug','used':{'gpu_seconds':0},'capacity':{'gpu_seconds':605*3600},'active_allocation_count':0}

modules={'fence':Fence,'preflight':Preflight,'base':Base,'snapshot':Snapshot,'bridge':Bridge}
c.load=lambda name,path,data:modules[name]
for failure in ('', 'jobs', 'snapshot', 'ledger', 'anchor', 'candidate-start', 'bad-quota',
                'candidate-stop', 'stock-start', 'backend-start'):
    for p in (c.UNIT, Fence.MARKER): p.unlink(missing_ok=True)
    for p in (c.STATE,c.ANCHOR):
        if p.exists(): shutil.rmtree(p)
    c.STATE.mkdir(mode=0o700)
    state.failure=failure; state.backend=True; state.controller='stock'
    try: c.main(root); raised=False
    except (RuntimeError,ValueError): raised=True
    assert raised == bool(failure), failure
    if failure in ('candidate-stop','stock-start','backend-start'):
        assert Fence.MARKER.exists(), failure
    else:
        assert not Fence.MARKER.exists() and state.backend and state.controller=='stock', failure
        assert not c.UNIT.exists(), failure
    if failure in ('ledger','anchor','candidate-start','bad-quota'):
        assert list(root.glob('backup*/candidate-probe/monthly-quota.wal')), failure
    print('PASS: simulated candidate rehearsal ' + (failure or 'success'))
