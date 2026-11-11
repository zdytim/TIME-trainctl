"""Compile the actual submit plugin with a minimal Slurm ABI stub, offline."""
import ctypes
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
class Job(ctypes.Structure):
    _fields_ = [('user_id', ctypes.c_uint32), ('comment', ctypes.c_char_p)]

class SubmitMarkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        (root / 'slurm').mkdir()
        (root / 'slurm/slurm.h').write_text('#include <stdint.h>\ntypedef struct { uint32_t user_id; char *comment; } job_desc_msg_t;\n#define NO_VAL UINT32_MAX\n#define SLURM_SUCCESS 0\n')
        (root / 'slurm/slurm_version.h').write_text('#define SLURM_VERSION_NUMBER 1\n')
        (root / 'slurm/slurm_errno.h').write_text('#define ESLURM_ACCESS_DENIED 1001\n')
        so = root / 'submit.so'
        subprocess.run(['cc','-shared','-fPIC','-Wall','-Wextra','-Werror','-I',str(root),str(ROOT/'deploy/job_submit_pro6000d.c'),'-o',str(so)],check=True)
        cls.plugin = ctypes.CDLL(str(so))
        cls.plugin.job_submit.argtypes = [ctypes.POINTER(Job),ctypes.c_uint32,ctypes.c_void_p]
        cls.plugin.job_submit.restype = ctypes.c_int
        cls.plugin.job_modify.argtypes = [ctypes.POINTER(Job), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        cls.plugin.job_modify.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def submit(self, marker, uid=0, user=1000):
        return self.plugin.job_submit(ctypes.byref(Job(user, marker)), uid, None)
    def test_selected_tiers_and_legacy(self):
        base = b'pro6000d-job:' + b'a' * 32
        for suffix in (b'', b':tier=debug', b':tier=important', b':tier=normal'):
            with self.subTest(suffix=suffix): self.assertEqual(self.submit(base+suffix), 0)
    def test_only_root_can_modify_job_marker(self):
        job = Job(1000, b'pro6000d-job:' + b'a' * 32 + b':tier=normal')
        self.assertNotEqual(self.plugin.job_modify(ctypes.byref(job), None, 1000, None), 0)
        self.assertEqual(self.plugin.job_modify(ctypes.byref(job), None, 0, None), 0)
    def test_rejects_forged_or_unparseable_markers(self):
        base = b'pro6000d-job:' + b'a' * 32
        for marker,uid,user in ((base,1000,1000),(base,0,0),(base+b':tier=other',0,1000),
                                (b'pro6000d-job:'+b'A'*32+b':tier=debug',0,1000),
                                (base+b':tier=normal:extra',0,1000)):
            with self.subTest(marker=marker, uid=uid, user=user): self.assertNotEqual(self.submit(marker,uid,user), 0)

if __name__ == '__main__': unittest.main()
