#!/usr/bin/env python3
"""Validate shared CLI publication and rollback in a private container."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types

assert Path('/.dockerenv').is_file()
assert os.geteuid()==0 and os.environ.get('TIME_ISOLATED_VALIDATION')=='1'
spec=importlib.util.spec_from_file_location('installer','/fixture/installer.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.os.uname=lambda:types.SimpleNamespace(nodename='u20-Z790-S-DDR4')
m.pwd.getpwnam=lambda name:types.SimpleNamespace(pw_uid=1001,pw_gid=1001,pw_dir='/tmp')
root=Path(tempfile.mkdtemp(prefix='time-shared-cli-fixture-',dir='/var/backups'))
for fail in (False,True):
    parent=root/('failure' if fail else 'success');parent.mkdir(mode=0o755)
    inputs=parent/'inputs';inputs.mkdir(mode=0o755)
    m.LIB=parent/'lib';m.VERSION=m.LIB/'version';m.BIN=parent/'bin';m.BIN.mkdir(mode=0o755)
    data={name:('fixture '+name).encode() for name in m.FILES}
    m.FILES={name:hashlib.sha256(value).hexdigest() for name,value in data.items()}
    for name,value in data.items():(inputs/name).write_bytes(value)
    m.WRAPPERS={'timectl':b'fixture cli\n','time-mcp':b'fixture read-only mcp\n'}
    def run(*args,**kwargs):
        if fail:raise ValueError('injected health failure')
        return types.SimpleNamespace(stdout=json.dumps({'ok':True,'data':{'monthly_quota':{'enforcement_ready':True}}}))
    m.subprocess.run=run
    try:m.main(inputs);raised=False
    except ValueError:raised=True
    assert raised==fail
    if fail:
        assert not (m.BIN/'timectl').exists() and not (m.BIN/'time-mcp').exists()
        assert m.VERSION.exists()
    else:
        assert (m.BIN/'timectl').read_bytes()==m.WRAPPERS['timectl']
        assert (m.BIN/'time-mcp').read_bytes()==m.WRAPPERS['time-mcp']
        for name,value in data.items():assert (m.VERSION/name).read_bytes()==value
print('PASS: shared CLI publication and health-failure wrapper rollback')
