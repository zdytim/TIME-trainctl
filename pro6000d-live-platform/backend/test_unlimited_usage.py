import os
import sqlite3
import unittest
import importlib.util
import json
from types import SimpleNamespace

from controller_bridge import ControllerBridge, ControllerUnavailable
from monthly_reservation import ReservationCoordinator, ReservationUncertain

spec = importlib.util.spec_from_file_location('training_api', os.path.join(os.path.dirname(__file__), 'pro6000d-training-api.py'))
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.service = api.Service.__new__(api.Service)
        self.service.config = {'capacity': {'gpu': 8, 'cpu': 8, 'memory_mb': 65536}}
        self.service.qos = {'normal': {'max_minutes': 1440}, 'debug': {'max_minutes': 60}}
        self.service.user = lambda uid: SimpleNamespace(pw_uid=uid)
        self.service.safe_user_path = lambda path, user: path
        self.service.monthly_controller_snapshot = lambda uid: {'enforcement_ready': True, 'selectable_tiers_ready': True}
        self.payload = {'name': 'train', 'command': 'true', 'workdir': '/tmp', 'selected_tier': 'important'}

    def test_cli_explicit_duration_and_qos(self):
        for fields in ({'time_minutes': 60}, {'qos': 'normal'}, {'time_minutes': 60, 'qos': 'normal'}, {'time_minutes': 60, 'qos': 'debug'}):
            with self.assertRaises(api.ApiError) as error:
                self.service.validate_job({'uid': 1}, {**self.payload, **fields})
            self.assertEqual(error.exception.code, 'legacy_time_fields')
        job = self.service.validate_job({'uid': 1}, {**self.payload, 'time_minutes': 60, 'qos': 'debug'}, policy_version=1)
        self.assertEqual((job['time_minutes'], job['qos'], job['selected_tier']), (60, 'debug', 'important'))

    def test_ui_defaults_and_selected_tier(self):
        job = self.service.validate_job({'uid': 1}, self.payload)
        self.assertEqual((job['time_minutes'], job['qos'], job['selected_tier']), (4294967295, 'long', 'important'))
        with self.assertRaises(api.ApiError):
            self.service.validate_job({'uid': 1}, {**self.payload, 'selected_tier': 'invalid'})

    def test_unknown_controller_is_closed(self):
        self.service.monthly_controller_snapshot = lambda uid: {}
        with self.assertRaises(api.ApiError):
            self.service.validate_job({'uid': 1}, self.payload)

    def test_sbatch_real_argv_policy2_and_legacy(self):
        self.service.config['unlimited_qos'] = 'long'
        job = self.service.validate_job({'uid': 1}, self.payload)
        argv = self.service.sbatch_argv({'id': 'a'*32, 'policy_version': 2}, job, SimpleNamespace(pw_uid=1), 2, '/tmp/job')
        self.assertEqual(argv[argv.index('--time') + 1], '0')
        self.assertEqual(argv[argv.index('--qos') + 1], 'long')
        self.assertIn('--no-requeue', argv)
        self.assertIn('pro6000d-job:' + 'a'*32 + ':tier=important', argv)
        legacy = self.service.validate_job({'uid': 1}, {**self.payload, 'time_minutes': 60, 'qos': 'debug'}, policy_version=1)
        argv = self.service.sbatch_argv({'id': 'a'*32, 'policy_version': 1}, legacy, SimpleNamespace(pw_uid=1), 2, '/tmp/job')
        self.assertEqual(argv[argv.index('--time') + 1], '60')
        self.assertNotIn('--no-requeue', argv)
        self.assertNotIn('--qos', argv)
        self.service.config['slurm_account'] = 'training'
        argv = self.service.sbatch_argv({'id': 'a'*32, 'policy_version': 1}, legacy, SimpleNamespace(pw_uid=1), 2, '/tmp/job')
        self.assertEqual(argv[argv.index('--qos') + 1], 'debug')

    def test_policy2_snapshot_keeps_approved_qos(self):
        self.service.config['unlimited_qos'] = 'long'
        approved = self.service.validate_job({'uid': 1}, self.payload, temporary=True)
        self.service.config['unlimited_qos'] = 'another'
        restored = self.service.validate_job({'uid': 1}, approved, temporary=True, policy_version=2)
        self.assertEqual(restored['qos'], 'long')

    def test_application_creation_and_adjusted_approval_versions(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE applications (id TEXT PRIMARY KEY, owner_uid INTEGER, owner_name TEXT, kind TEXT, status TEXT, requested_json TEXT, reason TEXT, created_at TEXT, updated_at TEXT, policy_version INTEGER NOT NULL DEFAULT 1, approved_json TEXT, decision_reason TEXT, expires_at TEXT)")
        self.service.store = SimpleNamespace(db=db, audit=lambda *args: None)
        self.service.config['temporary_authorizations'] = {'enabled': True}
        self.service.admin_uids = lambda: []
        self.service.require_admin = lambda ident: None
        self.service.identity = lambda uid: {'uid': uid}
        self.service.add_notification = lambda *args: None
        user = {'uid': 1, 'username': 'user'}
        for request, expected_policy, expected_minutes, expected_qos in (
                (self.payload, 2, 4294967295, 'long'),
                ({**self.payload, 'time_minutes': 60, 'qos': 'debug'}, 1, 60, 'debug')):
            if expected_policy == 1:
                db.execute("INSERT INTO applications(id,owner_uid,owner_name,kind,status,requested_json,reason,created_at,updated_at,policy_version) VALUES(?,?,?,?,?,?,?,?,?,?)", ('legacy', 1, 'user', 'temporary_job', 'PENDING', json.dumps(request), 'test reason', 'old', 'old', 1))
                application = dict(db.execute('SELECT * FROM applications WHERE id=?', ('legacy',)).fetchone())
            else:
                application = self.service.create_application(user, {'kind': 'temporary_job', 'requested': request, 'reason': 'test reason'})
            self.assertEqual(application['policy_version'], expected_policy)
            if expected_policy == 2:
                for tamper in ({'qos': 'normal'}, {'time_minutes': 60}, {'time_minutes': '4294967295'}):
                    with self.assertRaises(api.ApiError) as error:
                        self.service.decide_application(user, application['id'], {'action': 'approve_adjusted', 'decision_reason': 'adjust resources', 'approved': tamper})
                    self.assertEqual(error.exception.code, 'immutable_policy_fields')
                    self.assertEqual(db.execute('SELECT status FROM applications WHERE id=?', (application['id'],)).fetchone()[0], 'PENDING')
            adjustment = {'gpu': 2} if expected_policy == 2 else {'gpu': 2, 'time_minutes': 30, 'qos': 'normal'}
            decision = self.service.decide_application(user, application['id'], {'action': 'approve_adjusted', 'decision_reason': 'adjust resources', 'approved': adjustment})
            approved = json.loads(decision['approved_json'])
            if expected_policy == 1:
                expected_minutes, expected_qos = 30, 'normal'
            self.assertEqual((approved['gpu'], approved['time_minutes'], approved['qos']), (2, expected_minutes, expected_qos))
            self.assertEqual(decision['policy_version'], expected_policy)


class Bridge(ControllerBridge):
    def __init__(self, replies):
        super().__init__('/unused', 1)
        self.expected_pid = 123
        self.replies = list(replies)
        self.commands = []

    def _exchange(self, message, mutation=False, required_pid=None):
        self.commands.append(message)
        return self.replies.pop(0)


class BrokerTests(unittest.TestCase):
    def test_capability_no_fallback(self):
        bridge = Bridge(['OK ACTUAL_USAGE_UNLIMITED2 UNSUPPORTED'])
        with self.assertRaises(ControllerUnavailable):
            bridge.reserve(1, 'a' * 32, 'normal', 1, 4294967295, policy_version=2)
        self.assertEqual(bridge.commands, ['ACTUAL_USAGE_UNLIMITED2 0'])

    def test_legacy_and_actual_reserve(self):
        bridge = Bridge(['OK 3600000000000', 'OK ACTUAL_USAGE_UNLIMITED2 SUPPORTED', 'OK 1000000000'])
        bridge.reserve(1, 'a' * 32, 'normal', 1, 60)
        bridge.reserve(1, 'b' * 32, 'normal', 1, 4294967295, policy_version=2)
        self.assertTrue(bridge.commands[0].startswith('RESERVE1 '))
        self.assertTrue(bridge.commands[2].startswith('RESERVE2 '))

    def test_terminal_receipt_strict(self):
        identity = '1 ' + 'a' * 32 + ' 42 11 22'
        for suffix in ('0 0', '3 0', '2 9'):
            bridge = Bridge(['OK ' + identity + ' ' + suffix])
            bridge.terminal_binding_receipt(1, 'a' * 32, 42, 11, 22)
            self.assertEqual(bridge.commands[0], 'BIND2_TERMINAL_GET ' + identity)
        for suffix in ('0 1', '3 1', '2 0', '1 0', '2 18446744073709551616'):
            with self.assertRaises(ControllerUnavailable):
                Bridge(['OK ' + identity + ' ' + suffix]).terminal_binding_receipt(1, 'a' * 32, 42, 11, 22)
        with self.assertRaises(ControllerUnavailable):
            Bridge(['OK ' + identity.replace(' 11 ', ' 12 ') + ' 3 0']).terminal_binding_receipt(1, 'a' * 32, 42, 11, 22)

    def test_old_hold_migrates_to_policy1(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE monthly_dispatch_holds(job_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, tier TEXT NOT NULL, gpu INTEGER NOT NULL, minutes INTEGER NOT NULL, month INTEGER NOT NULL, state TEXT NOT NULL, scheduler_id INTEGER)')
        db.execute('INSERT INTO monthly_dispatch_holds VALUES(?,?,?,?,?,?,?,?)', ('a' * 32, 1, 'normal', 1, 60, 202601, 'HELD', None))
        ReservationCoordinator(db, Bridge([]))
        self.assertEqual(db.execute('SELECT policy_version FROM monthly_dispatch_holds').fetchone()[0], 1)

    def test_terminal_receipt_unavailable_never_cancels(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        db.row_factory = sqlite3.Row
        bridge = Bridge(['OK FOUND 2 202601 2 1 4294967295 1000000000 42 0 1 0', 'ERROR'])
        coordinator = ReservationCoordinator(db, bridge)
        db.execute("INSERT INTO monthly_dispatch_holds(job_id,uid,tier,gpu,minutes,month,state,scheduler_id,binding_version,policy_version,bind_submit,bind_sluid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ('a'*32,1,'normal',1,4294967295,202601,'BOUND',42,2,2,'11','22'))
        with self.assertRaises(ReservationUncertain):
            coordinator.finish('a'*32, 1, 42)
        self.assertFalse(any(command.startswith('CANCEL_BOUND') for command in bridge.commands))
        self.assertEqual(db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0], 'BOUND')

    def test_cancel_bound_recovery_and_closed_recheck(self):
        db = sqlite3.connect(':memory:', isolation_level=None)
        db.row_factory = sqlite3.Row
        marker = 'a' * 32
        identity = '1 ' + marker + ' 42 11 22'
        bound = 'OK FOUND 2 202601 2 1 4294967295 1000000000 42 0 1 0'
        cancelled = 'OK FOUND 2 202601 2 1 4294967295 1000000000 42 0 6 0'
        bridge = Bridge([bound, 'OK ' + identity + ' 0 0', 'OK', cancelled,
                         'OK ' + identity + ' 3 0', cancelled, 'OK ' + identity + ' 3 0'])
        coordinator = ReservationCoordinator(db, bridge)
        db.execute("INSERT INTO monthly_dispatch_holds(job_id,uid,tier,gpu,minutes,month,state,scheduler_id,binding_version,policy_version,bind_submit,bind_sluid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (marker,1,'normal',1,4294967295,202601,'BOUND',42,2,2,'11','22'))
        coordinator.finish(marker, 1, 42)
        coordinator.finish(marker, 1, 42)
        self.assertEqual(db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0], 'CLOSED')
        self.assertEqual(sum(command.startswith('CANCEL_BOUND2 ') for command in bridge.commands), 1)


if __name__ == '__main__':
    unittest.main()
