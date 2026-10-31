#!/usr/bin/env python3
"""Exercise jobcomp cleanup using private copies of a u20 snapshot."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import types

assert Path('/.dockerenv').is_file()
assert os.geteuid()==0 and os.environ.get('TIME_ISOLATED_VALIDATION')=='1'
def load(name):
    spec=importlib.util.spec_from_file_location(name,'/fixture/'+name+'.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
guard=load('guarded'); snapshot=load('snapshot')
root=Path(tempfile.mkdtemp(prefix='time-jobcomp-fixture-',dir='/var/backups'))
backup=root/'backup'; backup.mkdir(mode=0o700)
for name in ('manifest.json','state.sqlite3','jobcomp.log'):
    shutil.copyfile('/snapshot/'+name,backup/name); (backup/name).chmod(0o600)
live=root/'jobcomp.log'; shutil.copyfile(backup/'jobcomp.log',live); live.chmod(0o640)
mock=types.SimpleNamespace(JOBCOMP=live,identity=snapshot.identity,requirements=lambda:None)
original=live.read_bytes(); manifest=(backup/'manifest.json').read_bytes()
with live.open('ab') as out: out.write(b'JobId=9999999 other\n')
try: guard.clear_stopped(backup,mock)
except ValueError as exc: assert 'changed' in str(exc)
else: raise AssertionError('changed live jobcomp accepted')
assert live.read_bytes()!=original
live.write_bytes(original)
bad=original+b'JobId=9999999 unexpected\n'
(backup/'jobcomp.log').write_bytes(bad)
m=json.loads(manifest); m['files']['jobcomp.log']['sha256']=hashlib.sha256(bad).hexdigest()
(backup/'manifest.json').write_text(json.dumps(m))
try: guard.clear_stopped(backup,mock)
except ValueError as exc: assert 'unmatched' in str(exc)
else: raise AssertionError('unrelated log record accepted')
assert live.read_bytes()==original
(backup/'jobcomp.log').write_bytes(original); (backup/'manifest.json').write_bytes(manifest)
oldmode=live.stat().st_mode & 0o777
count=guard.clear_stopped(backup,mock)
assert count==len(original.splitlines()) and live.read_bytes()==b'' and live.stat().st_mode & 0o777==oldmode
assert (backup/'jobcomp.log').read_bytes()==original
print('PASS: changed or unrelated jobcomp refused; backed-up matching log cleared in private fixture')
