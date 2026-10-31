#!/usr/bin/env python3
"""Exercise marker writes on private temporary files, never live scheduler data."""
import hashlib
import fcntl
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

tools=Path(__file__).resolve().parents[1]/'tools'
def load(name):
    p=tools/name; spec=importlib.util.spec_from_file_location(name,p)
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
writer=load('time-quota-marker-write.py')
candidate=load('time-grant-repair-candidate.py')


class MarkerWrite(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name)
        self.wal=b'U20MQ1|HEADER|1\nU20MQ1|1|GRANT|1001|202610|3600|0|0123456789abcdef0123456789abcdef|uid-1000\n'
        self.head=self.wal.splitlines(keepends=True)[0]; h=hashlib.sha256(self.head).hexdigest().encode()
        self.cp=b'U20MQ1|CHECKPOINT|0|'+h+b'\n'; self.witness=b'U20MQA1|0|'+h+b'\n'
        self.paths=[root/'wal',root/'cp',root/'witness']
        for p,data in zip(self.paths,(self.wal,self.cp,self.witness)): p.write_bytes(data); p.chmod(0o600)
        self.fds=[os.open(self.paths[0],os.O_RDONLY),os.open(self.paths[1],os.O_RDWR),os.open(self.paths[2],os.O_RDWR|os.O_APPEND)]
        self.row={'application_id':'0123456789abcdef0123456789abcdef','target_uid':1001,'month':202610,'gpu_seconds':3600,'actor_uid':1000,'state':'PENDING'}
        self.app={'id':self.row['application_id'],'owner_uid':1001,'kind':'monthly_gpu_hours','status':'GRANT_PENDING','approved_json':'{"month":202610,"gpu_hours":1}'}

    def tearDown(self):
        for fd in self.fds: os.close(fd)
        self.tmp.cleanup()

    def run_write(self, cp=None, wit=None):
        cp=self.cp if cp is None else cp; wit=self.witness if wit is None else wit
        plan=candidate.candidate(self.wal,cp,wit,self.row,self.app)
        writer.write_markers(*self.fds,(self.wal,cp,wit),plan,os.getuid(),os.getuid())
        return plan

    def test_writes_exact_candidate_and_leaves_wal_untouched(self):
        plan=self.run_write()
        self.assertEqual(self.paths[0].read_bytes(),self.wal)
        self.assertEqual(self.paths[1].read_bytes(),plan['checkpoint'])
        self.assertEqual(self.paths[2].read_bytes(),plan['witness'])

    def test_concurrent_content_change_is_refused(self):
        self.paths[1].write_bytes(b'unexpected')
        with self.assertRaises(ValueError): self.run_write()
        self.assertEqual(self.paths[1].read_bytes(),b'unexpected')
        self.assertEqual(self.paths[2].read_bytes(),self.witness)

    def test_append_failure_leaves_recoverable_checkpoint_then_retry(self):
        real_write=os.write
        def fail_witness(fd,data):
            if fd==self.fds[2]: raise OSError('injected append failure')
            return real_write(fd,data)
        with patch.object(writer.os,'write',side_effect=fail_witness):
            with self.assertRaises(OSError): self.run_write()
        cp=self.paths[1].read_bytes(); self.assertNotEqual(cp,self.cp)
        self.assertEqual(self.paths[2].read_bytes(),self.witness)
        plan=self.run_write(cp,self.witness)
        self.assertEqual(self.paths[2].read_bytes(),plan['witness'])

    def test_witness_without_append_flag_is_refused(self):
        replacement=os.open(self.paths[2],os.O_RDWR)
        try:
            plan=candidate.candidate(self.wal,self.cp,self.witness,self.row,self.app)
            with self.assertRaises(ValueError):
                writer.write_markers(self.fds[0],self.fds[1],replacement,(self.wal,self.cp,self.witness),plan,os.getuid(),os.getuid())
            self.assertEqual(self.paths[1].read_bytes(),self.cp)
        finally: os.close(replacement)

    def test_locked_witness_prevents_all_writes(self):
        other=os.open(self.paths[2],os.O_RDONLY)
        try:
            fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): self.run_write()
            self.assertEqual(self.paths[1].read_bytes(),self.cp)
            self.assertEqual(self.paths[2].read_bytes(),self.witness)
        finally: os.close(other)

    def test_torn_witness_is_rejected_on_retry(self):
        actual=os.write; calls=[0]
        def torn(fd,data):
            if fd != self.fds[2]: return actual(fd,data)
            calls[0]+=1
            if calls[0]==1: return actual(fd,data[:4])
            raise OSError('injected torn witness')
        with patch.object(writer.os,'write',side_effect=torn):
            with self.assertRaises(OSError): self.run_write()
        with self.assertRaises(ValueError):
            self.run_write(self.paths[1].read_bytes(),self.paths[2].read_bytes())


if __name__=='__main__': unittest.main()
