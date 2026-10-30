#!/usr/bin/env python3
"""Compare candidate and captured old broker on a private disposable SQLite DB."""
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

assert os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
assert pathlib.Path('/.dockerenv').is_file()
candidate_dir = pathlib.Path(os.environ['TIME_VALIDATION_BROKER_DIR']).resolve(strict=True)
old_path = pathlib.Path(os.environ['TIME_VALIDATION_OLD_BROKER']).resolve(strict=True)
sys.path.insert(0, str(candidate_dir))

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

old = load('time_old_broker', old_path)
new = load('time_new_broker', candidate_dir / 'pro6000d-training-api.py')

with tempfile.TemporaryDirectory(prefix='time-broker-rollback-', dir='/tmp') as tmp:
    config = pathlib.Path(tmp) / 'config.json'
    config.write_text(json.dumps({'state_db': str(pathlib.Path(tmp) / 'state.sqlite3')}))
    before = old.Service(str(config))
    before.store.db.close()
    current = new.Service(str(config))
    columns = {row[1] for row in current.store.db.execute('PRAGMA table_info(jobs)')}
    assert 'client_request_hash' not in columns
    assert 'job_request_fingerprints' in {r[0] for r in current.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # Keep the real request transaction and SQLite store while avoiding all
    # scheduler, GPU and filesystem side effects in this focused fixture.
    current.tick = lambda: None
    current.scheduler_available = lambda: True
    current.admission_available = lambda: True
    current.quota = lambda uid: {'gpu_limit': 1, 'active_job_limit': 8}
    current.active_usage = lambda uid: {'active_jobs': 0}
    current.dispatch_waiting = lambda: None
    current.validate_job = lambda ident, payload, **kwargs: {
        'name': 'fixture', 'gpu': 1, 'cpu': 1, 'memory_mb': 64, 'time_minutes': 1,
        'qos': 'normal', 'workdir': '/tmp', 'environment': 'system',
        'command': payload['command'], 'output_path': '/tmp/log'}
    ident = {'uid': 1001, 'username': 'timefixture'}
    payload = {'client_request_id': 'fixture-request-1', 'command': 'python train.py'}
    job = current.submit(ident, payload)
    assert current.submit(ident, payload)['id'] == job['id']
    try:
        current.submit(ident, {**payload, 'command': 'python other.py'})
        raise AssertionError('different request must conflict')
    except new.ApiError as exc:
        assert exc.code == 'idempotency_conflict', exc
    db = current.store.db
    db.execute("CREATE TRIGGER fail_fingerprint BEFORE INSERT ON job_request_fingerprints BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    try:
        current.submit(ident, {**payload, 'client_request_id': 'fixture-request-2'})
        raise AssertionError('injected fingerprint failure must fail')
    except sqlite3.DatabaseError:
        pass
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    db.execute('DROP TRIGGER fail_fingerprint')
    current.store.db.close()

    restored = old.Service(str(config))
    old_row = restored.store.db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
    assert old_row is not None and 'client_request_hash' not in restored.job_json(old_row)
    assert restored.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
    restored.store.db.close()

    resumed = new.Service(str(config))
    assert resumed.submit(ident, payload)['id'] == job['id']
    resumed.store.db.close()
    with sqlite3.connect(json.loads(config.read_text())['state_db']) as db:
        db.execute('ALTER TABLE jobs ADD COLUMN client_request_hash TEXT')
    try:
        new.Service(str(config))
        raise AssertionError('legacy hash column needs an offline migration')
    except RuntimeError as exc:
        assert 'offline migration' in str(exc), exc
print('PASS: fingerprints stay private across old-broker rollback and atomic failures')
