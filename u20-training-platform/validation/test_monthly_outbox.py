"""Isolated broker outbox checks; never open a live database or socket.

The operator must explicitly set TIME_VALIDATION_BROKER_DIR to an assembled
candidate directory. Fake controller and SQLite :memory: are the only state.
Run only after receiving the user's explicit permission to run tests.
"""
import importlib.util
import os
import pathlib
import sqlite3
import sys
import unittest


def load_candidate():
    location = os.environ.get("TIME_VALIDATION_BROKER_DIR")
    if not location:
        raise RuntimeError("TIME_VALIDATION_BROKER_DIR must name an isolated assembled candidate")
    root = pathlib.Path(location).resolve(strict=True)
    target = root / "pro6000d-training-api.py"
    if not target.is_file() or target.is_symlink():
        raise RuntimeError("candidate broker source not found")
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("time_validation_broker", str(target))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStore:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE applications(id TEXT PRIMARY KEY,status TEXT NOT NULL,updated_at TEXT);
          CREATE TABLE monthly_grant_outbox(application_id TEXT PRIMARY KEY,target_uid INTEGER,
             month INTEGER,gpu_seconds INTEGER,actor_uid INTEGER,state TEXT,created_at TEXT,updated_at TEXT);
          CREATE TABLE audit_log(application_id TEXT, result TEXT);
          CREATE TABLE notifications(uid INTEGER, title TEXT);
          INSERT INTO applications VALUES('app-123','GRANT_PENDING','before');
          INSERT INTO monthly_grant_outbox VALUES('app-123',1001,202610,36000,1000,'PENDING','before','before');
        """)

    def audit(self, uid, name, action, kind, aid, detail, result):
        self.db.execute("INSERT INTO audit_log VALUES(?,?)", (aid, result))


class FakeController:
    def __init__(self, module):
        self.module = module
        self.grants = {}
        self.lose_reply = False
        self.attempts = []

    def grant(self, uid, month, seconds, grant_id, actor):
        payload = (uid, month, seconds, actor)
        self.attempts.append((grant_id, payload))
        if grant_id in self.grants and self.grants[grant_id] != payload:
            raise ValueError("conflicting grant")
        self.grants[grant_id] = payload
        if self.lose_reply:
            self.lose_reply = False
            raise self.module.ControllerOutcomeUnknown("injected lost reply")
        return {"applied": True, "grant_id": grant_id}


class MonthlyOutboxValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_candidate()

    def setUp(self):
        self.service = self.module.Service.__new__(self.module.Service)
        self.service.store = FakeStore()
        self.controller = FakeController(self.module)
        self.service.monthly_controller_bridge = lambda: self.controller
        def require_admin(ident):
            if not ident.get("is_admin"):
                raise self.module.ApiError(403, "forbidden", "admin required")
        self.service.require_admin = require_admin
        self.service.add_notification = lambda uid, kind, title, body: self.service.store.db.execute(
            "INSERT INTO notifications VALUES(?,?)", (uid, title))
        self.admin = {"uid": 1000, "username": "admin", "is_admin": True}

    def state(self):
        return self.service.store.db.execute("SELECT state FROM monthly_grant_outbox").fetchone()[0]

    def test_lost_reply_preserves_intent_and_retry_is_idempotent(self):
        self.controller.lose_reply = True
        with self.assertRaises(self.module.ApiError) as error:
            self.service.finish_monthly_grant(self.admin, "app-123")
        self.assertEqual(error.exception.code, "grant_unconfirmed")
        self.assertEqual(self.state(), "PENDING")
        result = self.service.finish_monthly_grant(self.admin, "app-123")
        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(self.state(), "APPLIED")
        self.assertEqual(len(self.controller.grants), 1)
        self.assertEqual(self.controller.attempts[0], self.controller.attempts[1])

    def test_database_failure_after_ack_keeps_retryable_original_intent(self):
        db = self.service.store.db
        db.execute("CREATE TRIGGER fail_approval BEFORE UPDATE OF status ON applications BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.finish_monthly_grant(self.admin, "app-123")
        self.assertEqual(self.state(), "PENDING")
        db.execute("DROP TRIGGER fail_approval")
        self.service.finish_monthly_grant(self.admin, "app-123")
        self.assertEqual(self.state(), "APPLIED")
        self.assertEqual(len(self.controller.grants), 1)
        self.assertEqual(self.controller.attempts[0], self.controller.attempts[1])

    def test_non_admin_does_not_reach_controller(self):
        with self.assertRaises(self.module.ApiError):
            self.service.finish_monthly_grant({"uid": 1001, "username": "user", "is_admin": False}, "app-123")
        self.assertEqual(self.controller.attempts, [])
        self.assertEqual(self.state(), "PENDING")

    def tearDown(self):
        self.service.store.db.close()


if __name__ == "__main__":
    unittest.main()
