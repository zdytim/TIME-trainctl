#!/usr/bin/env python3
"""Real broker methods, private SQLite and isolated candidate controller only."""
import importlib.util
import json
import os
import pathlib
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

with tempfile.TemporaryDirectory(prefix='time-approval-', dir='/tmp') as tmp:
    config_path = pathlib.Path(tmp) / 'config.json'
    db_path = pathlib.Path(tmp) / 'state.sqlite3'
    config_path.write_text(json.dumps({
        'state_db': str(db_path), 'training_group': 'timefixture', 'admin_group': 'root',
        'monthly_controller': {'grant_enabled': True, 'controller_user': 'slurm',
                               'socket_path': '/tmp/time-isolated/state/time-quota.sock'},
    }))
    service = module.Service(str(config_path))
    real_bridge = service.monthly_controller_bridge()
    month = real_bridge.snapshot(1001)['month']
    request = {'method': 'POST', 'path': '/api/v1/applications', 'body': {
        'kind': 'monthly_gpu_hours', 'reason': 'Need more GPU training time',
        'requested': {'gpu_hours': 10, 'month': month, 'client_request_id': 'fixture-approval-1'},
    }}
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
    service.store.db.close()
    del service

    # Reopen the same private on-disk SQLite DB as a fresh Service instance.
    service = module.Service(str(config_path))
    reconcile = {'method': 'POST', 'path': '/api/v1/applications/%s/grant-reconcile' % aid}
    assert service.handle(0, reconcile)['application']['status'] == 'APPROVED'
    assert service.handle(1001, request)['application']['id'] == aid
    assert service.monthly_controller_bridge().snapshot(1001)['granted']['gpu_seconds'] == 7 * 3600
    assert service.store.db.execute('SELECT state FROM monthly_grant_outbox WHERE application_id=?', (aid,)).fetchone()['state'] == 'APPLIED'
    assert service.store.db.execute('SELECT count(*) FROM audit_log WHERE object_id=?', (aid,)).fetchone()[0] >= 3
    assert service.store.db.execute('SELECT count(*) FROM notifications WHERE recipient_uid=1001').fetchone()[0] >= 1
    service.store.db.close()
print('PASS: real approval handlers persist and reconcile an adjusted grant after reopening SQLite')
