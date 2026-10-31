import importlib.util
from pathlib import Path
import sqlite3
import unittest

path = Path(__file__).resolve().parents[1] / 'tools/u20-history-purge-core.py'
spec = importlib.util.spec_from_file_location('history_purge_core', path)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class HistoryPurgeTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY, owner_uid INTEGER, client_request_id TEXT, state TEXT, finished_at TEXT, temporary_application_id TEXT)')
        self.db.execute('CREATE TABLE applications(id TEXT, bound_job_id TEXT)')
        self.db.execute('CREATE TABLE job_request_fingerprints(job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, request_hash TEXT)')
        self.db.execute('CREATE TABLE job_request_tombstones(owner_uid INTEGER NOT NULL, client_request_id TEXT NOT NULL, PRIMARY KEY(owner_uid,client_request_id))')
        for name in core.TABLES - {'jobs', 'applications', 'job_request_fingerprints', 'job_request_tombstones'}:
            self.db.execute('CREATE TABLE "' + name + '" (value TEXT)')
        self.db.execute("INSERT INTO audit_log VALUES('retain')")
        self.db.execute("INSERT INTO jobs VALUES('job1',1001,'request1','COMPLETED','now',NULL)")
        self.db.execute("INSERT INTO job_request_fingerprints VALUES('job1','hash')")

    def tearDown(self):
        self.db.close()

    def test_tombstones_precede_delete_and_audit_remains(self):
        self.assertEqual(core.purge(self.db), 1)
        self.assertEqual(self.db.execute('SELECT * FROM jobs').fetchall(), [])
        self.assertEqual(self.db.execute('SELECT * FROM job_request_fingerprints').fetchall(), [])
        self.assertEqual(self.db.execute('SELECT * FROM job_request_tombstones').fetchall(), [(1001, 'request1')])
        self.assertEqual(self.db.execute('SELECT * FROM audit_log').fetchall(), [('retain',)])

    def test_refuses_active_or_ambiguous_jobs(self):
        for state, finished, application in (('RUNNING', 'now', None), ('COMPLETED', None, None),
                                              ('COMPLETED', 'now', 'approval')):
            self.db.execute('UPDATE jobs SET state=?,finished_at=?,temporary_application_id=?',
                            (state, finished, application))
            with self.assertRaises(ValueError): core.purge(self.db)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM job_request_tombstones').fetchone()[0], 0)

    def test_refuses_bound_approvals_or_unexpected_schema(self):
        self.db.execute("INSERT INTO applications VALUES('approval','job1')")
        with self.assertRaises(ValueError): core.purge(self.db)
        self.db.execute('DELETE FROM applications')
        self.db.execute('CREATE TABLE unexpected(value TEXT)')
        with self.assertRaises(ValueError): core.purge(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_refuses_triggers_and_disabled_foreign_keys(self):
        self.db.execute('CREATE TRIGGER unexpected AFTER DELETE ON jobs BEGIN DELETE FROM audit_log; END')
        with self.assertRaises(ValueError): core.purge(self.db)
        self.db.execute('DROP TRIGGER unexpected')
        self.db.execute('PRAGMA foreign_keys=OFF')
        with self.assertRaises(ValueError): core.purge(self.db)


if __name__ == '__main__':
    unittest.main()
