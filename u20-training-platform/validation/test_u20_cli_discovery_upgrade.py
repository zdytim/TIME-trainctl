#!/usr/bin/env python3
"""Exercise CLI wrapper upgrade and rollback on private temporary files."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types

path=Path(__file__).resolve().parents[1]/'deploy/upgrade-u20-cli-discovery.py'
spec=importlib.util.spec_from_file_location('upgrade',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.pwd.getpwuid=lambda uid:types.SimpleNamespace(pw_name='u20')
m.os.uname=lambda:types.SimpleNamespace(nodename='u20-Z790-S-DDR4')
sha=lambda data:hashlib.sha256(data).hexdigest()
for failure in (False,True):
    with tempfile.TemporaryDirectory() as tmp:
        base=Path(tmp);m.ROOT=base/'time';m.ROOT.mkdir();m.OLD=m.ROOT/'candidate-0858424'
        m.OLD.mkdir();m.NEW=m.ROOT/'candidate-monthly-discovery-1';m.BIN=base/'bin';m.BIN.mkdir()
        original={'time_core.py':b'old core','timectl.py':b'old cli','time_mcp.py':b'old mcp'}
        for name,data in original.items():(m.OLD/name).write_bytes(data);(m.OLD/name).chmod(0o755)
        manifest={'file_count':len(original),'files':[{'path':n,'sha256':sha(d),'mode':'0o755'} for n,d in original.items()]}
        encoded=(json.dumps(manifest)+'\n').encode();(m.OLD/'artifact-manifest.json').write_bytes(encoded);m.OLD_MANIFEST=sha(encoded)
        fixed=base/'fixed';fixed.write_bytes(b'new core');m.NEW_CORE=sha(b'new core')
        wrappers={'timectl':b'old wrapper','time-mcp':b'old mcp wrapper'}
        m.WRAPPERS={name:sha(data) for name,data in wrappers.items()}
        for name,data in wrappers.items():(m.BIN/name).write_bytes(data);(m.BIN/name).chmod(0o755)
        payload={'ok':True,'data':{'monthly_quota':{'server_state':'enforcing','enforcement_ready':True}}}
        m.subprocess.run=lambda *a,**kw:types.SimpleNamespace(stdout=json.dumps(payload))
        original_atomic=m.atomic;calls=[]
        def injected(path,data,mode=0o755):
            calls.append(path.name);original_atomic(path,data,mode)
            if failure and calls==['timectl']:raise OSError('injected post-replace failure')
        m.atomic=injected
        try:
            try:m.main(fixed);raised=False
            except OSError:raised=True
            assert raised==failure
            if failure:
                for name,data in wrappers.items():assert (m.BIN/name).read_bytes()==data
            else:
                assert (m.NEW/'time_core.py').read_bytes()==b'new core'
                assert b'unset TIME_MCP_CONSENT_FD' in (m.BIN/'time-mcp').read_bytes()
                assert str(m.NEW).encode() in (m.BIN/'timectl').read_bytes()
        finally:m.atomic=original_atomic
print('PASS: private CLI capability upgrade and post-replace rollback')
