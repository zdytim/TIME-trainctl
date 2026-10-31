#!/usr/bin/env python3
"""Exercise the offline inspector with disposable in-memory ledger data."""
import hashlib
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'tools/time-quota-recovery-inspect.py'
spec = importlib.util.spec_from_file_location('quota_inspect', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def sha(data):
    return hashlib.sha256(data).hexdigest().encode()


class RecoveryHistory(unittest.TestCase):
    def setUp(self):
        self.header = b'U20MQ1|HEADER|1\n'
        self.event = b'U20MQ1|1|GRANT|example\n'
        self.wal = self.header + self.event
        self.cp = b'U20MQ1|CHECKPOINT|1|' + sha(self.wal) + b'\n'
        self.witness = (b'U20MQA1|0|' + sha(self.header) + b'\n' +
                        b'U20MQA1|1|' + sha(self.wal) + b'\n')

    def test_history_matches_all_prefixes(self):
        result = module.inspect(self.wal, self.cp, self.witness)
        self.assertTrue(result['matching'])
        self.assertTrue(result['witness_history_matches_wal'])

    def test_historical_witness_tampering_cannot_be_reported_matching(self):
        altered = self.witness.replace(sha(self.header), b'0' * 64, 1)
        result = module.inspect(self.wal, self.cp, altered)
        self.assertFalse(result['matching'])
        self.assertFalse(result['witness_history_matches_wal'])

    def test_witness_ahead_remains_mismatch(self):
        extra = self.witness + b'U20MQA1|2|' + b'a' * 64 + b'\n'
        result = module.inspect(self.wal, self.cp, extra)
        self.assertFalse(result['matching'])
        self.assertFalse(result['witness_history_matches_wal'])

    def test_incomplete_line_is_rejected(self):
        with self.assertRaises(ValueError):
            module.inspect(self.wal + b'U20MQ1|2|PARTIAL', self.cp, self.witness)

    def test_wal_ahead_of_both_markers_is_identified_without_repair(self):
        old_cp = b'U20MQ1|CHECKPOINT|0|' + sha(self.header) + b'\n'
        old_witness = b'U20MQA1|0|' + sha(self.header) + b'\n'
        result = module.inspect(self.wal, old_cp, old_witness)
        self.assertFalse(result['matching'])
        self.assertTrue(result['checkpoint_matches_wal_prefix'])
        self.assertEqual(result['observation'], 'wal_one_event_ahead')
        self.assertEqual(result['recovery_readiness'], 'not assessed')

    def test_checkpoint_ahead_of_witness_is_identified_without_repair(self):
        old_witness = b'U20MQA1|0|' + sha(self.header) + b'\n'
        result = module.inspect(self.wal, self.cp, old_witness)
        self.assertFalse(result['matching'])
        self.assertEqual(result['observation'], 'witness_one_event_behind')

    def test_bad_old_checkpoint_is_not_treated_as_orderly_interruption(self):
        bad_cp = b'U20MQ1|CHECKPOINT|0|' + b'0'*64 + b'\n'
        old_witness = b'U20MQA1|0|' + sha(self.header) + b'\n'
        result = module.inspect(self.wal, bad_cp, old_witness)
        self.assertFalse(result['checkpoint_matches_wal_prefix'])
        self.assertEqual(result['observation'], 'inconsistent_or_unclassified')


if __name__ == '__main__':
    unittest.main()
