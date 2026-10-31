#!/usr/bin/env python3
"""Exercise history cleanup on a private u20 SQLite backup, never the live DB."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
from urllib.parse import quote

sys.dont_write_bytecode = True
if os.geteuid() != 0 or len(sys.argv) != 2:
    raise SystemExit('root and a root-owned candidate directory are required')
code = Path(sys.argv[1])
live = Path('/var/lib/pro6000d-training/state.sqlite3')
backups = Path('/var/backups/pro6000d-training')
expected = {
    'pro6000d-training-api.py': '24afd69cff5eac856f2ccaaf8a97b95c1823c911c83f40338745cd4ab813e821',
    'controller_bridge.py': '30ac4668f6ea6f0a413f4a4a834b27d42c5db069900600058d18da58e2b04a70',
    'controller_view.py': '10be510b5bee9afaec955293dbff499140e099ff612eed1186e7e96e299a637d',
    'u20-history-purge-core.py': '21b7cd7c990b020c021dc9161ebaa15e007c9f79e39296f201558a7538f9ade5',
    'old.py': '4224e6aa116532585c6c9e837ee102e9d508ef00b04e6e685bd5e6f092e88821',
}


def trusted(path, directory):
    for component in reversed((path,) + tuple(path.parents)):
        st = component.lstat()
        is_dir = component != path or directory
        if (stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022 or
                (not stat.S_ISDIR(st.st_mode) if is_dir else not stat.S_ISREG(st.st_mode))):
            raise ValueError('untrusted path: ' + str(component))
    if not directory and path.lstat().st_nlink != 1:
        raise ValueError('input must have one link: ' + str(path))


def backup(source, dest):
    deadline = time.monotonic() + 30
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline: raise TimeoutError('SQLite backup timed out')
    uri = 'file:' + quote(str(source), safe='/') + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=5) as src, sqlite3.connect(str(dest)) as dst:
        src.backup(dst, pages=256, progress=progress, sleep=.1)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('backup integrity failure')
    os.chmod(dest, 0o600)


def digest_table(db, table):
    schema = tuple(tuple(row) for row in db.execute('PRAGMA table_info("' + table + '")'))
    digest = hashlib.sha256()
    count = 0
    for row in db.execute('SELECT * FROM "' + table + '" ORDER BY rowid'):
        value = repr(tuple(row)).encode('utf-8')
        digest.update(len(value).to_bytes(8, 'big')); digest.update(value); count += 1
    return schema, count, digest.digest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


trusted(code, True); trusted(backups, True); trusted(live, False)
for name, sha in expected.items():
    path = code / name; trusted(path, False)
    if hashlib.sha256(path.read_bytes()).hexdigest() != sha: raise ValueError('candidate digest mismatch: ' + name)
sys.path.insert(0, str(code))
broker = load('shadow_history_broker', code / 'pro6000d-training-api.py')
purge = load('shadow_history_purge', code / 'u20-history-purge-core.py')
os.umask(0o077)
folder = Path(tempfile.mkdtemp(prefix='shadow-history-', dir=str(backups)))
baseline, shadow = folder / 'baseline.sqlite3', folder / 'shadow.sqlite3'
print('Protected snapshot directory:', folder, flush=True)
backup(live, baseline); backup(baseline, shadow)
config = folder / 'shadow-config.json'
config.write_text(json.dumps({'state_db': str(shadow)})); os.chmod(config, 0o600)
service = broker.Service(str(config)); db = service.store.db
unchanged = purge.TABLES - {'jobs', 'job_request_fingerprints', 'job_request_tombstones'}
before = {name: digest_table(db, name) for name in unchanged}
request_ids = [(r[0], r[1]) for r in db.execute('SELECT owner_uid,client_request_id FROM jobs WHERE client_request_id IS NOT NULL')]
job_count = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
deleted = purge.purge(db)
assert deleted == job_count
assert {name: digest_table(db, name) for name in unchanged} == before
assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
assert db.execute('SELECT COUNT(*) FROM job_request_fingerprints').fetchone()[0] == 0
for uid, request_id in request_ids:
    try:
        service.submit({'uid': uid, 'username': 'shadow'}, {'client_request_id': request_id})
        raise AssertionError('purged request was accepted')
    except broker.ApiError as exc:
        if exc.code != 'historical_request_expired': raise
assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
service.store.db.close()
# The previously deployed broker does not consult tombstones. Exercise its
# real insertion path with scheduler operations stubbed; SQLite guards must
# still reject every old ID without writing job or audit rows.
old = load('shadow_old_history_broker', code / 'old.py')
restored = old.Service(str(config))
restored.tick = lambda: None
restored.scheduler_available = lambda: True
restored.admission_available = lambda: True
restored.quota = lambda uid: {'gpu_limit': 1, 'active_job_limit': 8}
restored.active_usage = lambda uid: {'active_jobs': 0}
restored.dispatch_waiting = lambda: None
restored.validate_job = lambda ident, payload, **kwargs: {
    'name':'shadow', 'gpu':1, 'cpu':1, 'memory_mb':64, 'time_minutes':1,
    'qos':'normal', 'workdir':'/tmp', 'environment':'system',
    'command':'true', 'output_path':'/tmp/shadow.log'}
for uid, request_id in request_ids:
    try:
        restored.submit({'uid':uid, 'username':'shadow'}, {'client_request_id':request_id})
        raise AssertionError('old broker accepted purged request')
    except sqlite3.IntegrityError as exc:
        if 'historical request id retired' not in str(exc): raise
assert restored.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
assert {name: digest_table(restored.store.db, name) for name in unchanged} == before
assert restored.store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
restored.store.db.close()
print('PASS: shadow purge removed %d jobs; candidate and old broker rejected %d request IDs; preserved other tables' % (deleted, len(request_ids)))
print('Live database, services and jobs were not modified.')
