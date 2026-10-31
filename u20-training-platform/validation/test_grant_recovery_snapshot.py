#!/usr/bin/env python3
"""Read-only recovery snapshot on private temporary files and SQLite."""
import hashlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

tools=Path(__file__).resolve().parents[1]/'tools'
spec=importlib.util.spec_from_file_location('grant_snapshot',tools/'time-grant-recovery-snapshot.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class GrantSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name).resolve()
        self.state=self.root/'state'; self.anchor=self.root/'anchor'; self.dbdir=self.root/'db'; self.backups=self.root/'backups'
        for p in (self.state,self.anchor,self.dbdir,self.backups): p.mkdir(mode=0o700)
        self.ident='0123456789abcdef0123456789abcdef'; header=b'U20MQ1|HEADER|1\n'
        self.wal=header+('U20MQ1|1|GRANT|1001|202610|3600|0|'+self.ident+'|uid-1000\n').encode()
        h=hashlib.sha256(header).hexdigest().encode()
        for p,data in ((self.state/'monthly-quota.wal',self.wal),
                       (self.state/'monthly-quota.checkpoint',b'U20MQ1|CHECKPOINT|0|'+h+b'\n'),
                       (self.anchor/'witness.log',b'U20MQA1|0|'+h+b'\n')):
            p.write_bytes(data); p.chmod(0o600)
        self.db=self.dbdir/'state.sqlite3'
        with sqlite3.connect(str(self.db)) as c:
            c.execute('CREATE TABLE monthly_grant_outbox (application_id TEXT, target_uid INTEGER, month INTEGER, gpu_seconds INTEGER, actor_uid INTEGER, state TEXT)')
            c.execute('CREATE TABLE applications (id TEXT, owner_uid INTEGER, kind TEXT, status TEXT, approved_json TEXT)')
            c.execute('INSERT INTO monthly_grant_outbox VALUES (?,?,?,?,?,?)',(self.ident,1001,202610,3600,1000,'PENDING'))
            c.execute('INSERT INTO applications VALUES (?,?,?,?,?)',(self.ident,1001,'monthly_gpu_hours','GRANT_PENDING',json.dumps({'month':202610,'gpu_hours':1})))
        self.db.chmod(0o600)

    def tearDown(self): self.tmp.cleanup()
    def collect(self, check=lambda:None):
        return module.collect(self.state,self.anchor,self.db,self.backups,os.getuid(),os.getuid(),check)

    def test_backup_contains_verified_sources_and_candidate(self):
        out=self.collect(); directory=Path(out['backup'])
        self.assertEqual((directory/'monthly-quota.wal').read_bytes(),self.wal)
        self.assertTrue(out['intent_matches']); self.assertFalse(out['repair_authorized'])
        self.assertEqual(out['observation'],'wal_one_event_ahead')
        self.assertTrue((directory/'state.sqlite3').is_file())
        self.assertEqual(os.stat(directory).st_mode & 0o777,0o700)
        self.assertEqual(self.state.joinpath('monthly-quota.wal').read_bytes(),self.wal)

    def test_mismatched_grant_does_not_produce_authorized_candidate(self):
        with sqlite3.connect(str(self.db)) as c: c.execute('UPDATE monthly_grant_outbox SET gpu_seconds=7200')
        with self.assertRaises(ValueError): self.collect()

    def test_unsafe_database_symlink_is_rejected(self):
        moved=self.dbdir/'real'; self.db.rename(moved); self.db.symlink_to(moved)
        with self.assertRaises((OSError,ValueError)): self.collect()

    def test_active_service_aborts_before_snapshot(self):
        def fail(): raise RuntimeError('service active')
        with self.assertRaises(RuntimeError): self.collect(fail)
        self.assertEqual(list(self.backups.iterdir()),[])

    def test_witness_lock_prevents_snapshot(self):
        fd=os.open(self.anchor/'witness.log',os.O_RDONLY)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): self.collect()
            self.assertEqual(list(self.backups.iterdir()),[])
        finally: os.close(fd)

    def test_changed_marker_is_detected_after_database_backup(self):
        calls=[0]
        def mutate():
            calls[0]+=1
            if calls[0]==3: (self.state/'monthly-quota.wal').write_bytes(self.wal+b'changed')
        with self.assertRaises(ValueError): self.collect(mutate)
        for backup in self.backups.iterdir(): self.assertFalse((backup/'preview.json').exists())


if __name__=='__main__': unittest.main()
