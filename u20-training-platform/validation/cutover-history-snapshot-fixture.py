#!/usr/bin/env python3
"""Exercise split history migration and interruption evidence on private copies."""
import importlib.util
import json
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
    obj=importlib.util.module_from_spec(spec); spec.loader.exec_module(obj); return obj
snapshot=load('snapshot'); history=load('history'); core=load('core'); log=load('log'); migration=load('migration'); rollback=load('rollback')
root=Path(tempfile.mkdtemp(prefix='time-history-migration-',dir='/var/backups'))
def setup(name):
    base=root/name; base.mkdir(mode=0o700); backup=base/'backup'; backup.mkdir(mode=0o700)
    for file in ('manifest.json','state.sqlite3','jobcomp.log'):
        shutil.copyfile('/snapshot/'+file,backup/file); (backup/file).chmod(0o600)
    db=base/'live.sqlite3'; shutil.copyfile(backup/'state.sqlite3',db); db.chmod(0o600)
    jobcomp=base/'jobcomp'; shutil.copyfile(backup/'jobcomp.log',jobcomp); jobcomp.chmod(0o640)
    return backup,types.SimpleNamespace(DB=db,JOBCOMP=jobcomp,identity=snapshot.identity,requirements=lambda:None)

backup,source=setup('success')
result=migration.apply(backup,source,history,core,log)
assert result['deleted_jobs']==7 and result['cleared_jobcomp_records']==7
phases=[json.loads(line)['phase'] for line in (backup/'history-migration.jsonl').read_text().splitlines()]
assert phases==['database_start','database_complete','jobcomp_start','jobcomp_complete']
assert source.JOBCOMP.stat().st_size==0
with sqlite3.connect(source.DB) as db: assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
assert rollback.restore(backup,source,core,history,migration)['database_restored']
assert source.JOBCOMP.read_bytes()==(backup/'jobcomp.log').read_bytes()
with sqlite3.connect(source.DB) as db:
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==7
    assert history.logical_digest(db)==history.logical_digest(sqlite3.connect(backup/'state.sqlite3'))
backup,source=setup('partial')
with source.JOBCOMP.open('ab') as out: out.write(b'JobId=9999999 external\n')
try: migration.apply(backup,source,history,core,log)
except ValueError as exc: assert 'changed' in str(exc)
else: raise AssertionError('changed log accepted')
phases=[json.loads(line)['phase'] for line in (backup/'history-migration.jsonl').read_text().splitlines()]
assert phases==['database_start','database_complete','jobcomp_start']
assert source.JOBCOMP.stat().st_size>0
try: migration.apply(backup,source,history,core,log)
except ValueError as exc: assert 'reconcile' in str(exc)
else: raise AssertionError('partial migration retried')
# An unknown changed log must prevent database restoration as well.
try: rollback.restore(backup,source,core,history,migration)
except ValueError as exc: assert 'jobcomp' in str(exc)
else: raise AssertionError('rollback accepted newer log')
with sqlite3.connect(source.DB) as db: assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
# Restore only after the log is reconciled back to the backed-up state.
source.JOBCOMP.write_bytes((backup/'jobcomp.log').read_bytes())
assert rollback.restore(backup,source,core,history,migration)['jobcomp_restored']
with sqlite3.connect(source.DB) as db: assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==7
backup,source=setup('new-database-data')
migration.apply(backup,source,history,core,log)
with sqlite3.connect(source.DB) as db:
    db.execute("INSERT INTO job_request_tombstones(owner_uid,client_request_id) VALUES(9999,'newer')")
try: rollback.restore(backup,source,core,history,migration)
except ValueError as exc: assert 'database' in str(exc)
else: raise AssertionError('rollback accepted newer database data')
assert source.JOBCOMP.stat().st_size==0
backup,source=setup('database-commit-before-journal')
migration.apply(backup,source,history,core,log)
journal=backup/'history-migration.jsonl'
journal.write_text(journal.read_text().splitlines()[0]+'\n')
source.JOBCOMP.write_bytes((backup/'jobcomp.log').read_bytes())
assert rollback.restore(backup,source,core,history,migration)['database_restored']
with sqlite3.connect(source.DB) as db: assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==7
print('PASS: private snapshot migration, rollback, and rejection of newer data')
