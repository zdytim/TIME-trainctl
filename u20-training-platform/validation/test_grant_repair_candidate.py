#!/usr/bin/env python3
"""Build candidate bytes for interrupted GRANTs without touching disk state."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

tools = Path(__file__).resolve().parents[1]/'tools'
sys.path.insert(0, str(tools))
spec = importlib.util.spec_from_file_location('grant_candidate', tools/'time-grant-repair-candidate.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


def digest(b): return hashlib.sha256(b).hexdigest().encode()


class GrantCandidate(unittest.TestCase):
    def setUp(self):
        self.h = b'U20MQ1|HEADER|1\n'
        self.id = '0123456789abcdef0123456789abcdef'
        self.event = ('U20MQ1|1|GRANT|1001|202610|3600|0|'+self.id+'|uid-1000\n').encode()
        self.wal = self.h+self.event
        self.cp0 = b'U20MQ1|CHECKPOINT|0|'+digest(self.h)+b'\n'
        self.cp1 = b'U20MQ1|CHECKPOINT|1|'+digest(self.wal)+b'\n'
        self.witness = b'U20MQA1|0|'+digest(self.h)+b'\n'
        self.row = {'application_id':self.id,'target_uid':1001,'month':202610,'gpu_seconds':3600,'actor_uid':1000,'state':'PENDING'}
        self.app = {'id':self.id,'owner_uid':1001,'kind':'monthly_gpu_hours','status':'GRANT_PENDING','approved_json':json.dumps({'month':202610,'gpu_hours':1})}

    def test_both_interrupted_write_shapes(self):
        for cp in (self.cp0, self.cp1):
            with self.subTest(cp=cp):
                result=module.candidate(self.wal,cp,self.witness,self.row,self.app)
                self.assertEqual(result['checkpoint'],self.cp1)
                self.assertEqual(result['witness'],self.witness+b'U20MQA1|1|'+digest(self.wal)+b'\n')
                self.assertFalse(result['repair_authorized'])

    def test_mismatch_and_wrong_history_refused(self):
        with self.assertRaises(ValueError):
            module.candidate(self.wal,self.cp0,self.witness,dict(self.row,gpu_seconds=7200),self.app)
        with self.assertRaises(ValueError):
            module.candidate(self.wal,self.cp0,self.witness.replace(digest(self.h),b'0'*64),self.row,self.app)

    def test_duplicate_grant_id_in_prefix_refused(self):
        first = self.event
        second = self.event.replace(b'U20MQ1|1|',b'U20MQ1|2|',1)
        prior = self.h+first
        cp = b'U20MQ1|CHECKPOINT|1|'+digest(prior)+b'\n'
        witness = self.witness+b'U20MQA1|1|'+digest(prior)+b'\n'
        with self.assertRaises(ValueError):
            module.candidate(prior+second,cp,witness,self.row,self.app)

    def test_already_matching_and_partial_refused(self):
        full = self.witness+b'U20MQA1|1|'+digest(self.wal)+b'\n'
        with self.assertRaises(ValueError): module.candidate(self.wal,self.cp1,full,self.row,self.app)
        with self.assertRaises(ValueError): module.candidate(self.wal[:-1],self.cp0,self.witness,self.row,self.app)


if __name__ == '__main__': unittest.main()
