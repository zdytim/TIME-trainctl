#!/usr/bin/env python3
"""Use copies of a protected u20 snapshot, never the live database."""
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import types

assert Path('/.dockerenv').is_file()
assert os.geteuid()==0 and os.environ.get('TIME_ISOLATED_VALIDATION')=='1'
def load(name):
    spec=importlib.util.spec_from_file_location(name,'/fixture/'+name+'.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
core=load('core'); guard=load('guarded')
root=Path(tempfile.mkdtemp(prefix='time-real-history-fixture-',dir='/var/backups'))
backup=root/'backup'; backup.mkdir(mode=0o700)
for name in ('manifest.json','state.sqlite3'):
    shutil.copyfile('/snapshot/'+name,backup/name); (backup/name).chmod(0o600)
live=root/'live.sqlite3'; shutil.copyfile(backup/'state.sqlite3',live); live.chmod(0o600)
with sqlite3.connect(live) as db:
    tables=core.TABLES
    before={name:db.execute('SELECT COUNT(*) FROM "'+name+'"').fetchone()[0] for name in tables}
assert before['jobs']==7
mock=types.SimpleNamespace(DB=live,requirements=lambda:None)
assert guard.purge_stopped(backup,core,mock)==before['jobs']
with sqlite3.connect(live) as db:
    after={name:db.execute('SELECT COUNT(*) FROM "'+name+'"').fetchone()[0] for name in tables}
    assert after['jobs']==0
    for name in tables - {'jobs','job_request_fingerprints','job_request_tombstones'}:
        assert before[name]==after[name], name
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert after['job_request_tombstones']>0
print('PASS: guarded purge of private copy of u20 snapshot preserves non-job table counts and referential integrity')
