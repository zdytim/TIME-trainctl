#!/usr/bin/env python3
"""Exercise the real slurmd proof helper in a GPU-free root-only VM."""
import ctypes
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

SOURCE=Path(os.environ.get('TIME26_VALIDATION_SOURCE','/tmp/time-slurm26-clean-priority/source'))/'src/slurmd/slurmd/req.c'

@unittest.skipUnless(os.geteuid()==0 and os.uname().nodename=='colima' and not any(Path('/dev').glob('nvidia*')),
                     'requires the GPU-free isolated Colima VM')
class NodeProofReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if subprocess.check_output(['systemd-detect-virt'],text=True).strip()!='apple': raise RuntimeError('unexpected VM')
        cls.tmp=tempfile.TemporaryDirectory(prefix='time26-proof-unit-',dir='/tmp')
        base=Path(cls.tmp.name); text=SOURCE.read_text()
        start=text.index('static int _monthly_write_node_proof(')
        end=text.index('\n}\n\n#endif',start)+2
        helper=text[start:end]
        c='''#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
typedef struct { uint32_t job_id; uint64_t sluid; } step_id;
typedef struct { step_id step_id; uint32_t job_uid,job_state,exit_code,derived_ec; time_t start_time; } kill_job_msg_t;
static struct { const char *node_name; } local_conf = {"colima"}, *conf = &local_conf;
#define xstrdup strdup
#define xfree free
'''+helper+'''
int exercise(int reuse, unsigned exit_code, long start) {
    kill_job_msg_t req = {0};
    req.step_id.job_id=17; req.step_id.sluid=123456; req.job_uid=1001;
    req.job_state=77; req.exit_code=exit_code; req.derived_ec=3; req.start_time=start;
    return _monthly_write_node_proof(&req, reuse);
}
'''
        (base/'proof.c').write_text(c)
        subprocess.run(['cc','-shared','-fPIC','-Wall','-Wextra','-Werror',str(base/'proof.c'),'-o',str(base/'proof.so')],check=True)
        cls.lib=ctypes.CDLL(str(base/'proof.so'));cls.lib.exercise.argtypes=[ctypes.c_int,ctypes.c_uint,ctypes.c_long];cls.lib.exercise.restype=ctypes.c_int
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory(prefix='proof-case-',dir=self.tmp.name)
        self.path=Path(self.dir.name)/'proof';self.path.write_bytes(b'P6NP1|HEADER|1\n');self.path.chmod(0o600)
        self.old=os.environ.get('TIME_QUOTA_NODE_PROOF_FILE');os.environ['TIME_QUOTA_NODE_PROOF_FILE']=str(self.path)
        self.start=int(time.time())-5
    def tearDown(self):
        if self.old is None: os.environ.pop('TIME_QUOTA_NODE_PROOF_FILE',None)
        else: os.environ['TIME_QUOTA_NODE_PROOF_FILE']=self.old
        self.dir.cleanup()
    def test_failed_first_epilog_cannot_create_proof_on_retry(self):
        before=self.path.read_bytes()
        self.assertNotEqual(self.lib.exercise(1,7,self.start),0)
        self.assertEqual(self.path.read_bytes(),before)
    def test_only_exact_previously_persisted_proof_can_be_reused(self):
        self.assertEqual(self.lib.exercise(0,0,self.start),0)
        before=self.path.read_bytes()
        self.assertEqual(self.lib.exercise(1,0,self.start),0)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertNotEqual(self.lib.exercise(1,7,self.start),0)
        self.assertEqual(self.path.read_bytes(),before)
    def test_partial_tail_cannot_be_reused(self):
        self.assertEqual(self.lib.exercise(0,0,self.start),0)
        with self.path.open('ab') as proof: proof.write(b'P6NP1|truncated')
        self.assertNotEqual(self.lib.exercise(1,0,self.start),0)

if __name__=='__main__':unittest.main()
