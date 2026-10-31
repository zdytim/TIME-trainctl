#!/usr/bin/env python3
"""Private-database integration check; no scheduler or GPU access."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

candidate = Path(os.environ['TIME_VALIDATION_BROKER_DIR']).resolve(strict=True)
sys.path.insert(0, str(candidate))
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

broker = load('history_broker', candidate / 'pro6000d-training-api.py')
purge = load('history_purge', Path(__file__).resolve().parents[1] / 'tools/u20-history-purge-core.py')
with tempfile.TemporaryDirectory(prefix='time-history-fixture-') as tmp:
    config = Path(tmp) / 'config.json'
    config.write_text(json.dumps({'state_db': str(Path(tmp) / 'state.sqlite3')}))
    service = broker.Service(str(config))
    service.tick = lambda: None
    service.scheduler_available = lambda: True
    service.admission_available = lambda: True
    service.quota = lambda uid: {'gpu_limit': 1, 'active_job_limit': 8}
    service.active_usage = lambda uid: {'active_jobs': 0}
    service.dispatch_waiting = lambda: None
    service.validate_job = lambda ident, payload, **kwargs: {
        'name': 'fixture', 'gpu': 1, 'cpu': 1, 'memory_mb': 64, 'time_minutes': 1,
        'qos': 'normal', 'workdir': '/tmp', 'environment': 'system',
        'command': payload['command'], 'output_path': '/tmp/log'}
    user = {'uid': 1001, 'username': 'fixture'}
    payload = {'client_request_id': 'fixture-request-1', 'command': 'python train.py'}
    job = service.submit(user, payload)
    assert service.submit(user, payload)['id'] == job['id']
    service.store.db.execute("UPDATE jobs SET state='COMPLETED',finished_at='now' WHERE id=?", (job['id'],))
    audit_count = service.store.db.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]
    assert purge.purge(service.store.db) == 1
    assert service.store.db.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0] == audit_count
    for request in (payload, {**payload, 'command': 'python different.py'}):
        try:
            service.submit(user, request)
            raise AssertionError('purged request ID must never create a new job')
        except broker.ApiError as exc:
            assert exc.code == 'historical_request_expired', exc
    assert service.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    other = {'uid': 1002, 'username': 'other'}
    assert service.submit(other, payload)['id'] != job['id']
    service.store.db.close()
print('PASS: history purge preserves request IDs and blocks replay without deleting audit')
