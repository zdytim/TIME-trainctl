#!/usr/bin/env python3
"""Simulate UI activation boundaries without touching host services."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types

assert Path('/.dockerenv').is_file()
assert os.geteuid()==0 and os.environ.get('TIME_ISOLATED_VALIDATION')=='1'
spec=importlib.util.spec_from_file_location('candidate','/fixture/candidate.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
root=Path(tempfile.mkdtemp(prefix='time-ui-fixture-',dir='/var/backups'))
state=types.SimpleNamespace(failure='',backend=True,atomic_calls=0)
c.CONFIG=root/'config.json'
c.checked=lambda path,digest:b'fixture'
c.os.uname=lambda:types.SimpleNamespace(nodename='u20-Z790-S-DDR4')
c.os.readlink=lambda path:c.CONTROLLER
c.pwd.getpwnam=lambda name:types.SimpleNamespace(pw_uid=1001 if name=='timzhang' else (1000 if name=='u20' else 64030))

class Fence:
    MARKER=root/'marker';SERVICE='backend.service'
    @staticmethod
    def create_marker(parent,content):
        assert not Fence.MARKER.exists();Fence.MARKER.write_bytes(content);Fence.MARKER.chmod(0o600);return 1
    @staticmethod
    def remove_marker(parent,identity,content):
        assert identity==1 and Fence.MARKER.read_bytes()==content;Fence.MARKER.unlink()
    @staticmethod
    def run(args):
        if args[0]=='/usr/bin/scontrol':return 'SlurmctldParameters     = MonthlyGpuQuota'
        if args[1]=='show':return '123'
        if args[1]=='stop':state.backend=False
        elif args[1]=='start':
            if state.failure=='backend-start':raise RuntimeError('injected backend failure')
            state.backend=True
        else:raise AssertionError(args)
        return ''
class Base:
    BACKUPS=root/'backups'
    @staticmethod
    def trusted_directory(path):pass
    @staticmethod
    def trusted_metadata(path):pass
    @staticmethod
    def no_scheduler_jobs():return state.failure!='jobs'
    @staticmethod
    def no_active_platform_jobs():return True
    @staticmethod
    def health(staged):assert staged and state.backend
    @staticmethod
    def atomic(path,data,mode):
        state.atomic_calls+=1
        path.write_bytes(data);path.chmod(mode)
        if state.failure=='atomic-after-write' and state.atomic_calls==1:raise OSError('injected fsync failure')
    @staticmethod
    def api(uid,path):
        if state.failure=='backend-health':raise ValueError('injected health failure')
        if path.endswith('capabilities'):return {'monthly_requests':{'available':True}}
        return {'identity':{'uid':uid},'scheduler_available':True,
                'monthly_quota':{'enforcement_ready':True,'mode':'enforcing'}}
class Bridge:
    class ControllerBridge:
        def __init__(self,path,uid):pass
        def snapshot(self,uid):
            return {'wal_sequence':3,'used':{'gpu_seconds':0,'nanoseconds':900000000},'active_allocation_count':0}
class View:
    @staticmethod
    def snapshot_view(before,verified):
        return {'current_tier':'debug','tiers':[{'limit_gpu_seconds':x} for x in (18000,360000,1800000)]}
Base.BACKUPS.mkdir(mode=0o700)
modules={'fence':Fence,'base':Base,'bridge':Bridge,'view':View}
c.load=lambda name,path,data:modules[name]
for failure in ('','jobs','atomic-after-write','backend-start','backend-health'):
    Fence.MARKER.unlink(missing_ok=True);c.CONFIG.write_text('{}\n');c.CONFIG.chmod(0o640)
    state.failure=failure;state.backend=True;state.atomic_calls=0
    try:c.main(root);raised=False
    except (ValueError,RuntimeError,OSError) as exc:
        if not failure: print('Unexpected activation failure:',repr(exc),flush=True)
        raised=True
    assert raised==bool(failure),failure
    if failure in ('backend-start','backend-health'):
        assert Fence.MARKER.exists() and not state.backend
        assert json.loads(c.CONFIG.read_text())['monthly_controller']['activation_verified'] is True
    elif failure:
        assert not Fence.MARKER.exists() and state.backend and json.loads(c.CONFIG.read_text())=={}
    else:
        assert not Fence.MARKER.exists() and state.backend
        assert json.loads(c.CONFIG.read_text())['monthly_controller']['activation_verified'] is True
    print('PASS: simulated monthly UI activation '+(failure or 'success'))
