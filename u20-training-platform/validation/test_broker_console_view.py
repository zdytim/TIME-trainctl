#!/usr/bin/env python3
"""Exercise the candidate broker, console-shaped view and real isolated controller."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

assert os.environ.get('TIME_ISOLATED_VALIDATION') == '1' and Path('/.dockerenv').is_file()
assert os.geteuid() == 0
root = Path(os.environ['TIME_VALIDATION_BROKER_DIR']).resolve(strict=True)
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('broker_view_fixture', root / 'pro6000d-training-api.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory(prefix='broker-console-view-', dir='/tmp') as tmp:
    config = Path(tmp) / 'config.json'
    config.write_text(json.dumps({'state_db': str(Path(tmp) / 'state.sqlite3'),
        'training_group': 'timefixture', 'admin_group': 'root',
        'monthly_controller': {'read_enabled': True, 'grant_enabled': True,
            'activation_verified': True, 'controller_user': 'slurm',
            'socket_path': '/tmp/time-isolated/state/time-quota.sock'}}))
    service = module.Service(str(config))
    # Rendering-unrelated data is stubbed; identity, database and controller are real.
    service.jobs = lambda _ident: []
    service.gpu_inventory = lambda _ident: {'available': False, 'devices': []}
    service.metrics = lambda _ident: {}
    state = service.handle(1001, {'method':'GET', 'path':'/api/v1/state'})
    monthly = state['monthly_quota']
    assert monthly['enforcement_ready'] and monthly['request_available']
    assert monthly['current_tier'] == 'debug' and monthly['used_gpu_seconds'] == 0
    assert len(monthly['tiers']) == 3 and monthly['month'][4] == '-'
    assert state['monthly_accounts'] == []  # no other users' views to a training user
    admin = service.handle(0, {'method':'GET', 'path':'/api/v1/state'})
    assert any(a['uid'] == 1001 and a['current_tier'] == 'debug' for a in admin['monthly_accounts'])
    caps = service.handle(1001, {'method':'GET', 'path':'/api/v1/capabilities'})
    assert caps['monthly_quota']['enforcement_ready'] and caps['monthly_requests']['available']
    month = int(monthly['month'].replace('-', ''))
    request = {'method':'POST', 'path':'/api/v1/applications', 'body':{
        'kind':'monthly_gpu_hours', 'reason':'Need extra GPU training time',
        'requested':{'gpu_hours':7, 'month':month, 'client_request_id':'fixture-console-001'}}}
    application = service.handle(1001, request)['application']
    assert application['status'] == 'PENDING'
    decision = {'method':'POST', 'path':'/api/v1/applications/' + application['id'] + '/decision',
                'body':{'action':'approve', 'decision_reason':'Approved'}}
    assert service.handle(0, decision)['application']['status'] == 'APPROVED'
    assert service.controller_monthly_snapshot(1001)['additional_gpu_seconds'] == 7 * 3600
    service.config['monthly_controller']['activation_verified'] = False
    staged = service.controller_monthly_snapshot(1001)
    assert staged['mode'] == 'staged' and not staged['enforcement_ready'] and not staged['request_available']
    assert service.handle(1001, request)['application']['id'] == application['id']
    new_request = json.loads(json.dumps(request))
    new_request['body']['requested']['client_request_id'] = 'fixture-console-002'
    try:
        service.handle(1001, new_request)
        raise AssertionError('staged controller accepted a new application')
    except module.ApiError as exc:
        assert exc.code == 'monthly_quota_unavailable'
    service.config['monthly_controller']['activation_verified'] = True
    service.config['monthly_controller']['socket_path'] = '/tmp/nonexistent-time-controller.sock'
    unknown = service.controller_monthly_snapshot(1001)
    assert unknown['mode'] == 'unknown' and not unknown['enforcement_ready']
    assert unknown['accounting_status'] == 'unknown' and not unknown['request_available']
    service.store.db.close()
print('PASS: real controller snapshot, admin views, approval, staged and disconnected states')
