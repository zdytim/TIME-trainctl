#!/usr/bin/env python3
"""Offline evidence matching for interrupted monthly grants; no live resources."""
import importlib.util
import json
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1]/'tools/time-grant-recovery-evidence.py'
spec = importlib.util.spec_from_file_location('grant_evidence', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GrantEvidence(unittest.TestCase):
    def setUp(self):
        self.id = '0123456789abcdef0123456789abcdef'
        self.event = ('U20MQ1|1|GRANT|1001|202610|3600|0|'+self.id+'|uid-1000\n').encode()
        self.outbox = {'application_id':self.id, 'target_uid':1001, 'month':202610,
                       'gpu_seconds':3600, 'actor_uid':1000, 'state':'PENDING'}
        self.application = {'id':self.id, 'owner_uid':1001, 'kind':'monthly_gpu_hours',
                            'status':'GRANT_PENDING', 'approved_json':json.dumps({'month':202610,'gpu_hours':1})}

    def test_pending_approval_matches_complete_grant(self):
        result = module.match_grant(self.event, self.outbox, self.application)
        self.assertTrue(result['intent_matches'])
        self.assertFalse(result['repair_authorized'])

    def test_applied_and_approved_match(self):
        self.outbox['state']='APPLIED'; self.application['status']='APPROVED'
        self.assertTrue(module.match_grant(self.event, self.outbox, self.application)['intent_matches'])

    def test_each_identity_amount_and_state_disagreement_is_rejected(self):
        cases = [('target_uid',1002),('month',202611),('gpu_seconds',7200),
                 ('actor_uid',1002),('state','FAILED'),('application_id','f'*32)]
        for field,value in cases:
            with self.subTest(field=field):
                row = dict(self.outbox, **{field:value})
                self.assertFalse(module.match_grant(self.event, row, self.application)['intent_matches'])

    def test_approval_mismatch_or_unapproved_is_rejected(self):
        for changes in ({'status':'PENDING'}, {'owner_uid':1002}, {'kind':'quota_change'},
                        {'approved_json':json.dumps({'month':202610,'gpu_hours':2})}):
            with self.subTest(changes=changes):
                self.assertFalse(module.match_grant(self.event, self.outbox, dict(self.application, **changes))['intent_matches'])

    def test_bad_event_never_matches(self):
        for event in (self.event.replace(b'|GRANT|', b'|PREPARE|'), self.event[:-1],
                      self.event.replace(b'|0|', b'|1|'), self.event+b'extra'):
            with self.subTest(event=event):
                self.assertFalse(module.match_grant(event, self.outbox, self.application)['intent_matches'])


if __name__ == '__main__': unittest.main()
