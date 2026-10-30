#!/usr/bin/env python3
"""Exercise broker outbox against a real controller in a disposable container."""
import os
import pathlib

assert os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
assert pathlib.Path('/.dockerenv').is_file()
assert os.geteuid() == 0

from test_monthly_outbox import FakeStore, load_candidate

module = load_candidate()
bridge = module.ControllerBridge('/tmp/time-isolated/state/time-quota.sock', 64030)
service = module.Service.__new__(module.Service)
service.store = FakeStore()
service.require_admin = lambda ident: None if ident.get('is_admin') else (_ for _ in ()).throw(module.ApiError(403, 'forbidden', 'admin required'))
service.add_notification = lambda uid, kind, title, body: service.store.db.execute(
    'INSERT INTO notifications VALUES(?,?)', (uid, title))
admin = {'uid': 1000, 'username': 'admin', 'is_admin': True}
target_uid = int(os.environ.get('TIME_TEST_TARGET_UID', '1001'))
assert target_uid in (0, 1001)
before = bridge.snapshot(target_uid)
assert before['granted']['gpu_seconds'] == 0, before
service.store.db.execute('UPDATE monthly_grant_outbox SET target_uid=?,month=?', (target_uid, before['month']))

class LostReply:
    def grant(self, *args):
        bridge.grant(*args)
        raise module.ControllerOutcomeUnknown('injected loss after real controller commit')

service.monthly_controller_bridge = lambda: LostReply()
try:
    service.finish_monthly_grant(admin, 'app-123')
    raise AssertionError('lost reply must not be marked approved')
except module.ApiError as exc:
    assert exc.code == 'grant_unconfirmed', exc
assert service.store.db.execute('SELECT state FROM monthly_grant_outbox').fetchone()[0] == 'PENDING'
assert bridge.snapshot(target_uid)['granted']['gpu_seconds'] == 36000
service.monthly_controller_bridge = lambda: bridge
assert service.finish_monthly_grant(admin, 'app-123')['status'] == 'APPROVED'
assert service.store.db.execute('SELECT state FROM monthly_grant_outbox').fetchone()[0] == 'APPLIED'
assert bridge.snapshot(target_uid)['granted']['gpu_seconds'] == 36000
try:
    bridge.grant(target_uid, before['month'], 36001, 'app-123', 'uid-1000')
    raise AssertionError('conflicting grant ID must not succeed')
except module.ControllerOutcomeUnknown:
    pass
assert bridge.snapshot(target_uid)['granted']['gpu_seconds'] == 36000
print('PASS: real controller grant survives lost reply and idempotent broker retry')
service.store.db.close()
