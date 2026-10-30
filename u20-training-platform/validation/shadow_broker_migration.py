#!/usr/bin/env python3
"""Check old/new broker compatibility on a protected SQLite backup, never live DB."""
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
assert os.geteuid() == 0
assert os.environ.get('TIME_SHADOW_MIGRATION') == '1'
assert len(sys.argv) == 2
code_dir = Path(sys.argv[1])
live_db = Path('/var/lib/pro6000d-training/state.sqlite3')
backup_root = Path('/var/backups/pro6000d-training')
expected = {
    'old.py': '65d213100a0e0ebd3f9fe87b51d1625f8539acf954ca572b43fa48a07c43173c',
    'new.py': '7372e1d9ad73f6899aa3f2e79e24aae594e42717ba941a6044ddfb31831ea55c',
    'controller_bridge.py': '30ac4668f6ea6f0a413f4a4a834b27d42c5db069900600058d18da58e2b04a70',
    'controller_view.py': '10be510b5bee9afaec955293dbff499140e099ff612eed1186e7e96e299a637d',
}

def trusted(path, directory=False):
    st = path.lstat()
    assert not stat.S_ISLNK(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022, path
    assert stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode), path

trusted(code_dir, True)
trusted(backup_root, True)
trusted(live_db)
for name, digest in expected.items():
    path = code_dir / name
    trusted(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
sys.path.insert(0, str(code_dir))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def backup(source, destination):
    deadline = time.monotonic() + 30
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise TimeoutError('SQLite backup exceeded 30 seconds')
    uri = 'file:' + quote(str(source), safe='/') + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=5) as src, sqlite3.connect(str(destination)) as dst:
        src.backup(dst, pages=256, progress=progress, sleep=.1)
    os.chmod(destination, 0o600)

tables = ('settings', 'quota_overrides', 'jobs', 'applications', 'notifications',
          'audit_log', 'rule_previews', 'quota_previews')
def inventory(db):
    result = {}
    for table in tables:
        # Table names are the fixed constants above; no untrusted SQL input.
        cols = tuple(tuple(row) for row in db.execute('PRAGMA table_info("%s")' % table))
        if not cols:
            raise RuntimeError('expected table absent: ' + table)
        digest = hashlib.sha256()
        count = 0
        for row in db.execute('SELECT * FROM "%s" ORDER BY rowid' % table):
            encoded = repr(tuple(row)).encode('utf-8')
            digest.update(len(encoded).to_bytes(8, 'big'))
            digest.update(encoded)
            count += 1
        result[table] = (cols, count, digest.digest())
    return result

folder = Path(tempfile.mkdtemp(prefix='shadow-broker-9566337-', dir=backup_root))
baseline, shadow = folder / 'baseline.sqlite3', folder / 'shadow.sqlite3'
print('Protected snapshot directory:', folder, flush=True)
backup(live_db, baseline)
backup(baseline, shadow)
with sqlite3.connect(str(baseline)) as db:
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RuntimeError('baseline SQLite integrity check failed')
    before = inventory(db)
config = folder / 'shadow-config.json'
config.write_text(json.dumps({'state_db': str(shadow)}))
os.chmod(config, 0o600)
new = load('shadow_new_broker', code_dir / 'new.py')
candidate = new.Service(str(config))
candidate.store.db.close()
old = load('shadow_old_broker', code_dir / 'old.py')
restored = old.Service(str(config))
after = inventory(restored.store.db)
assert before == after, 'existing table contents or schemas changed'
assert restored.store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
restored.store.db.close()
print('PASS: candidate migration and old broker reopen preserved existing tables')
print('Row counts:', json.dumps({k: v[1] for k, v in before.items()}, sort_keys=True))
print('Live database, services and running jobs were not modified.')
