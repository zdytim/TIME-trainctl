"""Check that the controller's startup marker requires every guard build flag."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

MARKER=b'PRO6000D_MANDATORY_GPU_ALLOCATION_GATE_BUILD_V1'
FLAGS=('TIME_ENABLE_MONTHLY_ALLOCATION_GUARD','TIME_ENABLE_MONTHLY_COMMIT5_DRAFT',
       'TIME_ENABLE_MONTHLY_TRANSACTION_DRAFT','TIME_ENABLE_SELECTABLE_V2',
       'TIME_ENABLE_MONTHLY_LIFECYCLE_DRAFT','TIME_ENABLE_MONTHLY_SLUID_DRAFT',
       'TIME_ENABLE_MONTHLY_QUERY2_DRAFT','TIME_ENABLE_MONTHLY_TOMBSTONES_DRAFT',
       'TIME_ENABLE_MONTHLY_PRIORITY_DRAFT')

class BuildMarkerTest(unittest.TestCase):
    def test_marker_appears_only_with_all_required_flags(self):
        source=os.environ.get('TIME26_TEST_SOURCE')
        if not source: self.skipTest('set TIME26_TEST_SOURCE to the assembled Slurm source')
        text=(Path(source)/'src/slurmctld/node_scheduler.c').read_text()
        start=text.index('#if defined(TIME_ENABLE_MONTHLY_ALLOCATION_GUARD)')
        end=text.index('#endif',start)+len('#endif')
        define=text[start:end]
        start=text.index('#if TIME_TRANSACTION_PROTOTYPE &&')
        end=text.index('#define _DEBUG',start)
        marker=text[start:end]
        with tempfile.TemporaryDirectory(prefix='time26-marker-') as tmp:
            c=Path(tmp)/'guard.c';obj=Path(tmp)/'guard.o';c.write_text(define+'\n'+marker+'\n')
            for omitted in (None,*FLAGS):
                args=['cc','-c',str(c),'-o',str(obj)]
                args += ['-D'+flag+'=1' for flag in FLAGS if flag != omitted]
                subprocess.run(args,check=True,capture_output=True)
                self.assertEqual(MARKER in obj.read_bytes(),omitted is None,omitted)
            args=['cc','-c',str(c),'-o',str(obj),'-DTIME_ENABLE_MONTHLY_TEST_FIXTURE=1']
            args += ['-D'+flag+'=1' for flag in FLAGS]
            subprocess.run(args,check=True,capture_output=True)
            self.assertNotIn(MARKER,obj.read_bytes())

if __name__=='__main__': unittest.main()
