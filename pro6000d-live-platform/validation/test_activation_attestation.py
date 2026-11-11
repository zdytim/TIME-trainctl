"""Readiness must fail closed for stale processes or mismatched artifacts."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from activation_attestation import REQUIRED, MAPPED, verify_release


class ReleaseAttestationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.proc=self.root/'proc'; self.proc.mkdir()
        self.manifest=self.root/'manifest.json'; files={}
        for name in REQUIRED:
            path=self.root/name; path.write_bytes(('artifact-'+name).encode())
            files[name]={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        for role,pid,uid in [('controller','101',1234),('node','102',0)]:
            proc=self.proc/pid;proc.mkdir()
            (proc/'exe').write_bytes((self.root/role).read_bytes())
            (proc/'status').write_text('Uid:\t%d\t%d\t%d\t%d\n'%(uid,uid,uid,uid))
            (self.root/(role+'.pid')).write_text(pid+'\n')
            (proc/'maps').write_text('')
        (self.root/'node_proof').write_bytes(b'P6NP1|HEADER|1\n');(self.root/'node_proof').chmod(0o600)
        files['node_proof']={'path':str(self.root/'node_proof')}
        (self.root/'slurm_conf').write_text('SlurmctldParameters=MonthlyGpuQuota\nJobSubmitPlugins=pro6000d\nProctrackType=proctrack/cgroup\nTaskPlugin=task/cgroup,task/affinity\nSchedulerType=sched/backfill\nSelectType=select/cons_tres\n')
        (self.root/'cgroup_conf').write_text('CgroupPlugin=cgroup/v2\nConstrainDevices=yes\n')
        for name in ('slurm_conf','cgroup_conf'):
            files[name]['sha256']=hashlib.sha256((self.root/name).read_bytes()).hexdigest()
        for pid in ('101','102'):
            (self.proc/pid/'environ').write_bytes(('SLURM_CONF='+str(self.root/'slurm_conf')+'\0TIME_QUOTA_NODE_PROOF_FILE='+str(self.root/'node_proof')+'\0').encode())
            (self.proc/pid/'cmdline').write_bytes(b'slurm\0-D\0')
        (self.proc/'101'/'fd').mkdir();(self.proc/'101'/'fdinfo').mkdir()
        (self.proc/'101'/'fd'/'5').symlink_to(self.root/'node_proof')
        (self.proc/'101'/'fdinfo'/'5').write_text('flags:\t0100000\n')
        self.files=files
        self.write_maps()
        self.manifest.write_text(json.dumps({'version':1,'files':files,
            'pidfiles':{'controller':str(self.root/'controller.pid'),'node':str(self.root/'node.pid')}}))
    def write_maps(self, omit=None):
        lines=[]
        for name in MAPPED:
            if name==omit: continue
            st=(self.root/name).stat()
            lines.append('1-2 r-xp 0 %x:%x %d %s\n'%(os.major(st.st_dev),os.minor(st.st_dev),st.st_ino,self.root/name))
        (self.proc/'101'/'maps').write_text(''.join(lines))
        st=(self.root/'cgroup').stat()
        (self.proc/'102'/'maps').write_text('1-2 r-xp 0 %x:%x %d %s\n'%(os.major(st.st_dev),os.minor(st.st_dev),st.st_ino,self.root/'cgroup'))
    def verify(self):
        with patch('activation_attestation._process_started_ns',return_value=10**25):
            return verify_release(str(self.manifest),1234,proc_root=str(self.proc),owner=os.getuid(),check_ancestors=False)
    def tearDown(self): self.tmp.cleanup()
    def test_attestation_returns_the_verified_controller_pid(self):
        with patch('activation_attestation._process_started_ns',return_value=10**25):
            pid=verify_release(str(self.manifest),1234,proc_root=str(self.proc),owner=os.getuid(),
                               check_ancestors=False,return_controller_pid=True)
        self.assertEqual(pid,101)
    def test_all_paired_artifacts_and_processes_are_required(self):
        self.assertTrue(self.verify())
        (self.root/'node').write_text('changed')
        self.assertFalse(self.verify())
    def test_stale_node_process_is_rejected(self):
        (self.proc/'102'/'exe').write_text('old node binary')
        self.assertFalse(self.verify())
    def test_unloaded_plugin_is_rejected(self):
        self.write_maps(omit='select')
        self.assertFalse(self.verify())
    def test_unloaded_node_cgroup_is_rejected(self):
        (self.proc/'102'/'maps').write_text('')
        self.assertFalse(self.verify())
    def test_unmatched_node_proof_descriptor_is_rejected(self):
        (self.proc/'101'/'fd'/'5').unlink()
        self.assertFalse(self.verify())
    def test_node_using_other_proof_file_is_rejected(self):
        (self.proc/'102'/'environ').write_bytes(('SLURM_CONF='+str(self.root/'slurm_conf')+'\0TIME_QUOTA_NODE_PROOF_FILE=/wrong\0').encode())
        self.assertFalse(self.verify())
    def test_process_using_other_configuration_is_rejected(self):
        (self.proc/'102'/'environ').write_bytes(b'SLURM_CONF=/wrong/slurm.conf\0')
        self.assertFalse(self.verify())
    def test_config_changed_after_process_started_is_rejected(self):
        with patch('activation_attestation._process_started_ns',return_value=0):
            self.assertFalse(verify_release(str(self.manifest),1234,proc_root=str(self.proc),owner=os.getuid(),check_ancestors=False))
    def test_writable_manifest_is_rejected(self):
        self.manifest.chmod(0o666)
        self.assertFalse(self.verify())
    def test_incorrect_process_identity_is_rejected(self):
        (self.proc/'102'/'status').write_text('Uid:\t1000\t1000\t1000\t1000\n')
        self.assertFalse(self.verify())

if __name__=='__main__': unittest.main()
