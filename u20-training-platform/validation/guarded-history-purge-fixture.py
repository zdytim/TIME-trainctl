#!/usr/bin/env python3
"""Test guarded offline history deletion in a private container database."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import types

assert Path('/.dockerenv').is_file()
assert os.geteuid() == 0 and os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
def load(name):
    spec=importlib.util.spec_from_file_location(name, '/fixture/'+name+'.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
core=load('core'); guard=load('guarded')
root=Path(tempfile.mkdtemp(prefix='time-purge-fixture-',dir='/var/backups'))
live=root/'live.sqlite3'; backup=root/'backup'; backup.mkdir(mode=0o700)
with sqlite3.connect(live) as db:
    db.executescript('''
    PRAGMA foreign_keys=ON;
    CREATE TABLE jobs(id INTEGER PRIMARY KEY,state TEXT,finished_at TEXT,temporary_application_id INTEGER,owner_uid INTEGER,client_request_id TEXT);
    CREATE TABLE job_request_fingerprints(job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE);
    CREATE TABLE job_request_tombstones(owner_uid INTEGER,client_request_id TEXT,PRIMARY KEY(owner_uid,client_request_id));
    CREATE TABLE applications(bound_job_id INTEGER);
    CREATE TABLE audit_log(entry TEXT);
    INSERT INTO jobs VALUES(1,'COMPLETED','now',NULL,1001,'request-1');
    INSERT INTO job_request_fingerprints VALUES(1);
    INSERT INTO audit_log VALUES('preserve audit');
    ''')
    for name in sorted(core.TABLES - {'jobs','job_request_fingerprints','job_request_tombstones','applications','audit_log'}):
        db.execute('CREATE TABLE "'+name+'"(value TEXT)')
live.chmod(0o600)
with sqlite3.connect(live) as src, sqlite3.connect(backup/'state.sqlite3') as dst: src.backup(dst)
(backup/'state.sqlite3').chmod(0o600)
sha=hashlib.sha256((backup/'state.sqlite3').read_bytes()).hexdigest()
(backup/'manifest.json').write_text(json.dumps({'database':{'sha256':sha}})); (backup/'manifest.json').chmod(0o600)
mock=types.SimpleNamespace(DB=live,requirements=lambda:None)
with sqlite3.connect(live) as db: db.execute("INSERT INTO audit_log VALUES('newer data')")
try: guard.purge_stopped(backup,core,mock)
except ValueError as exc: assert 'differs' in str(exc)
else: raise AssertionError('stale backup accepted')
with sqlite3.connect(live) as db:
    assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    db.execute("DELETE FROM audit_log WHERE entry='newer data'")
assert guard.purge_stopped(backup,core,mock)==1
with sqlite3.connect(live) as db:
    assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    assert db.execute('SELECT owner_uid,client_request_id FROM job_request_tombstones').fetchall()==[(1001,'request-1')]
    assert db.execute('SELECT entry FROM audit_log').fetchall()==[('preserve audit',)]
    try: db.execute("INSERT INTO jobs VALUES(2,'PENDING',NULL,NULL,1001,'request-1')")
    except sqlite3.IntegrityError: pass
    else: raise AssertionError('retired request accepted')
print('PASS: stale backup refused; matching offline snapshot permits atomic purge and preserves audit and replay guard')
