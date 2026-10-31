#!/usr/bin/env python3
"""Exercise a read-only WAL replay helper extracted from the exact candidate source."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=os.environ.get('TIME_SLURM19_SOURCE')


@unittest.skipUnless(SOURCE,'requires the independently verified candidate source')
class SourceReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); cls.root=Path(cls.tmp.name)
        subprocess.run(['python3',str(ROOT/'tools/build-quota-replay-helper.py'),
                        '--source',SOURCE,'--output',str(cls.root/'build')],check=True)
        cls.helper=cls.root/'build/quota-replay'

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def replay(self, data):
        path=self.root/'ledger'; path.write_bytes(data)
        before=path.read_bytes()
        p=subprocess.run([str(self.helper),str(path)],capture_output=True,text=True,timeout=5)
        self.assertEqual(path.read_bytes(),before)
        return p

    def test_valid_grant_is_replayed(self):
        wal=b'U20MQ1|HEADER|1\nU20MQ1|1|GRANT|1001|202610|3600|0|grant1|uid-1000\n'
        self.assertEqual(self.replay(wal).returncode,0)

    def test_unmatched_abort_is_rejected(self):
        wal=b'U20MQ1|HEADER|1\nU20MQ1|1|ABORT|99|1\nU20MQ1|2|GRANT|1001|202610|3600|0|grant1|uid-1000\n'
        self.assertNotEqual(self.replay(wal).returncode,0)

    def test_allocation_lifecycle_replayed(self):
        wal=(b'U20MQ1|HEADER|1\nU20MQ1|1|PREPARE|42|1001|1|1|1790000000|0\n'
             b'U20MQ1|2|COMMIT|42|1\nU20MQ1|3|SETTLE|42|1|1790000000|0|1790000002|0\n'
             b'U20MQ1|4|RELEASE|42|1|1790000003|0\n'
             b'U20MQ1|5|GRANT|1001|202610|3600|0|grant1|uid-1000\n')
        self.assertEqual(self.replay(wal).returncode,0)

    def test_duplicate_grant_is_rejected(self):
        wal=(b'U20MQ1|HEADER|1\nU20MQ1|1|GRANT|1001|202610|3600|0|grant1|uid-1000\n'
             b'U20MQ1|2|GRANT|1001|202610|3600|0|grant1|uid-1000\n')
        self.assertNotEqual(self.replay(wal).returncode,0)


if __name__=='__main__': unittest.main()
