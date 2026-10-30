#!/usr/bin/env python3
"""Real broker methods, private SQLite and isolated candidate controller only."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

assert os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
assert pathlib.Path('/.dockerenv').is_file()
assert os.geteuid() == 0
root = pathlib.Path(os.environ['TIME_VALIDATION_BROKER_DIR']).resolve(strict=True)
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('time_validation_broker', root / 'pro6000d-training-api.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def request_for(month):
    return {'method': 'POST', 'path': '/api/v1/applications', 'body': {
        'kind': 'monthly_gpu_hours', 'reason': 'Need more GPU training time',
        'requested': {'gpu_hours': 10, 'month': month, 'client_request_id': 'fixture-approval-1'},
    }}

if len(sys.argv) > 1 and sys.argv[1] == 'prepare':
    config_path = pathlib.Path(sys.argv[2])
    service = module.Service(str(config_path))
    real_bridge = service.monthly_controller_bridge()
    month = real_bridge.snapshot(1001)['month']
    request = request_for(month)
    app = service.handle(1001, request)['application']
    aid = app['id']
    assert app['status'] == 'PENDING'
    assert service.handle(1001, request)['application']['id'] == aid
    decision = {'method': 'POST', 'path': '/api/v1/applications/%s/decision' % aid,
                'body': {'action': 'approve_adjusted', 'approved': {'gpu_hours': 7},
                         'decision_reason': 'Approved seven hours'}}
    try:
        service.handle(1001, decision)
        raise AssertionError('training user must not approve')
    except module.ApiError as exc:
        assert exc.code == 'admin_required', exc

    class LostReply:
        def grant(self, *args):
            real_bridge.grant(*args)
            raise module.ControllerOutcomeUnknown('injected loss after controller commit')

    service.monthly_controller_bridge = lambda: LostReply()
    try:
        service.handle(0, decision)
        raise AssertionError('lost reply must leave application pending')
    except module.ApiError as exc:
        assert exc.code == 'grant_unconfirmed', exc
    row = service.store.db.execute('SELECT status FROM applications WHERE id=?', (aid,)).fetchone()
    assert row['status'] == 'GRANT_PENDING'
    assert real_bridge.snapshot(1001)['granted']['gpu_seconds'] == 7 * 3600
    # Abruptly terminate this broker-like process without closing SQLite.
    print(aid, flush=True)
    os._exit(75)

if len(sys.argv) > 1 and sys.argv[1] == 'recover':
    config_path, aid = pathlib.Path(sys.argv[2]), sys.argv[3]
    service = module.Service(str(config_path))
    reconcile = {'method': 'POST', 'path': '/api/v1/applications/%s/grant-reconcile' % aid}
    assert service.handle(0, reconcile)['application']['status'] == 'APPROVED'
    snapshot = service.monthly_controller_bridge().snapshot(1001)
    assert service.handle(1001, request_for(snapshot['month']))['application']['id'] == aid
    assert snapshot['granted']['gpu_seconds'] == 7 * 3600
    assert service.store.db.execute('SELECT state FROM monthly_grant_outbox WHERE application_id=?', (aid,)).fetchone()['state'] == 'APPLIED'
    assert service.store.db.execute('SELECT count(*) FROM audit_log WHERE object_id=?', (aid,)).fetchone()[0] >= 3
    assert service.store.db.execute('SELECT count(*) FROM notifications WHERE recipient_uid=1001').fetchone()[0] >= 1
    service.store.db.close()
    print('PASS: fresh process reconciled the adjusted grant once')
    sys.exit(0)

with tempfile.TemporaryDirectory(prefix='time-approval-', dir='/tmp') as tmp:
    config_path = pathlib.Path(tmp) / 'config.json'
    db_path = pathlib.Path(tmp) / 'state.sqlite3'
    config_path.write_text(json.dumps({
        'state_db': str(db_path), 'training_group': 'timefixture', 'admin_group': 'root',
        'monthly_controller': {'grant_enabled': True, 'read_enabled': True, 'activation_verified': True, 'controller_user': 'slurm',
                               'socket_path': '/tmp/time-isolated/state/time-quota.sock'},
    }))
    prepared = subprocess.run([sys.executable, __file__, 'prepare', str(config_path)],
                              text=True, capture_output=True, timeout=15)
    assert prepared.returncode == 75, (prepared.returncode, prepared.stdout, prepared.stderr)
    aid = prepared.stdout.strip()
    assert len(aid) == 32 and all(c in '0123456789abcdef' for c in aid), aid
    recovered = subprocess.run([sys.executable, __file__, 'recover', str(config_path), aid],
                               text=True, capture_output=True, timeout=15)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    print(recovered.stdout.strip())
