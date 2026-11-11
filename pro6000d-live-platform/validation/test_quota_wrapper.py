"""Test the witness wrapper without executing or changing privileges."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import types
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'deploy/quota-controller-wrapper.py'
spec=importlib.util.spec_from_file_location('quota_wrapper',SOURCE)
wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)

@unittest.skipUnless(os.geteuid()==0, 'requires root-owned test files in isolated VM')
class WitnessWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='/root');base=Path(self.tmp.name)
        wrapper.WITNESS_DIR=base/'protected';wrapper.WITNESS_DIR.mkdir(mode=0o700)
        wrapper.WITNESS=wrapper.WITNESS_DIR/'witness';wrapper.WITNESS.write_text('fixture\n');wrapper.WITNESS.chmod(0o600)
        wrapper.NODE_PROOF=wrapper.WITNESS_DIR/'node-proof.wal';wrapper.NODE_PROOF.write_text('P6NP1|HEADER|1\n');wrapper.NODE_PROOF.chmod(0o600)
        wrapper.NODE_READY=wrapper.WITNESS_DIR/'node-ready'
        self.pidfds=[]
        def verified_node(config):
            fd=os.pidfd_open(os.getpid(),0);self.pidfds.append(fd);return fd
        self.node_patch=patch.object(wrapper,'_open_verified_node_pidfd',side_effect=verified_node);self.node_patch.start()
        self.controller=base/'slurmctld';self.controller.write_bytes(b'fixture-'+wrapper.CONTROLLER_CAPABILITY);self.controller.chmod(0o755)
        wrapper.CONTROLLER=str(self.controller)
        wrapper.SLURM_CONFIG=base/'slurm.conf';wrapper.SLURM_CONFIG.write_text('SlurmctldParameters=MonthlyGpuQuota\n');wrapper.SLURM_CONFIG.chmod(0o600)
        wrapper.GRES_CONFIG=base/'gres.conf';wrapper.GRES_CONFIG.write_text('Name=gpu File=/dev/null\n');wrapper.GRES_CONFIG.chmod(0o600)
        wrapper.CGROUP_CONFIG=base/'cgroup.conf';wrapper.CGROUP_CONFIG.write_text('CgroupPlugin=cgroup/v2\n');wrapper.CGROUP_CONFIG.chmod(0o600)
        self.node=base/'slurmd';self.node.write_bytes(b'fixture-'+wrapper.NODE_CAPABILITY);self.node.chmod(0o755);wrapper.NODE_DAEMON=str(self.node)
        wrapper.RELEASE_MANIFEST=base/'monthly-release.json'
        files={key:{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
               for key,path in (('controller',self.controller),('node',self.node),('slurm_conf',wrapper.SLURM_CONFIG),('gres_conf',wrapper.GRES_CONFIG),('cgroup_conf',wrapper.CGROUP_CONFIG))}
        wrapper.RELEASE_MANIFEST.write_text(json.dumps({'version':1,'files':files}));wrapper.RELEASE_MANIFEST.chmod(0o600)
    def tearDown(self):
        self.node_patch.stop()
        for fd in self.pidfds:
            try: os.close(fd)
            except OSError: pass
        self.tmp.cleanup()
    def test_opens_locked_witness_then_drops_privileges_before_exec(self):
        called=[]
        def execute(path,args,env):
            fd=int(env['TIME_QUOTA_WITNESS_FD'])
            self.assertTrue(os.get_inheritable(fd))
            self.assertEqual(os.fstat(fd).st_ino,wrapper.WITNESS.stat().st_ino)
            proof_fd=int(env['TIME_QUOTA_NODE_PROOF_FD'])
            self.assertTrue(os.get_inheritable(proof_fd))
            self.assertEqual(os.fstat(proof_fd).st_ino,wrapper.NODE_PROOF.stat().st_ino)
            self.assertEqual(args, [str(self.controller), '-D', '-f', str(wrapper.SLURM_CONFIG)])
            self.assertEqual(env['SLURM_CONF'],str(wrapper.SLURM_CONFIG))
            self.assertTrue(os.get_inheritable(int(env['TIME_QUOTA_NODE_PIDFD'])))
            called.append((path,args,fd,proof_fd));raise RuntimeError('exec intercepted')
        user=types.SimpleNamespace(pw_name='slurm',pw_uid=111,pw_gid=112)
        with patch.object(wrapper.pwd,'getpwnam',return_value=user),patch.object(wrapper.os,'initgroups') as groups,\
             patch.object(wrapper.os,'setgid') as gid,patch.object(wrapper.os,'setuid') as uid,\
             patch.object(wrapper.os,'execve',side_effect=execute):
            with self.assertRaisesRegex(RuntimeError,'exec intercepted'):wrapper.main()
        groups.assert_called_once_with('slurm',112);gid.assert_called_once_with(112);uid.assert_called_once_with(111)
        self.assertEqual(called[0][0],str(self.controller));os.close(called[0][2]);os.close(called[0][3])
    def test_incomplete_proof_tail_is_removed_without_deleting_complete_records(self):
        complete=b'P6NP1|HEADER|1\nP6NP1|complete\n'
        wrapper.NODE_PROOF.write_bytes(complete+b'P6NP1|partial')
        def execute(path,args,env):
            os.close(int(env['TIME_QUOTA_WITNESS_FD']));os.close(int(env['TIME_QUOTA_NODE_PROOF_FD']))
            raise RuntimeError('exec intercepted')
        user=types.SimpleNamespace(pw_name='slurm',pw_uid=111,pw_gid=112)
        with patch.object(wrapper.pwd,'getpwnam',return_value=user),patch.object(wrapper.os,'initgroups'),\
             patch.object(wrapper.os,'setgid'),patch.object(wrapper.os,'setuid'),patch.object(wrapper.os,'execve',side_effect=execute):
            with self.assertRaisesRegex(RuntimeError,'exec intercepted'):wrapper.main()
        self.assertEqual(wrapper.NODE_PROOF.read_bytes(),complete)
    def test_complete_record_appended_before_lock_is_not_truncated(self):
        complete=b'P6NP1|HEADER|1\nP6NP1|complete\n'
        original_flock=wrapper.fcntl.flock
        def concurrent_append(fd,operation):
            if operation == wrapper.fcntl.LOCK_EX and os.fstat(fd).st_ino == wrapper.NODE_PROOF.stat().st_ino:
                with wrapper.NODE_PROOF.open('ab') as proof: proof.write(b'P6NP1|complete\n')
            return original_flock(fd,operation)
        def execute(path,args,env):
            os.close(int(env['TIME_QUOTA_WITNESS_FD']));os.close(int(env['TIME_QUOTA_NODE_PROOF_FD']))
            raise RuntimeError('exec intercepted')
        user=types.SimpleNamespace(pw_name='slurm',pw_uid=111,pw_gid=112)
        with patch.object(wrapper.pwd,'getpwnam',return_value=user),patch.object(wrapper.os,'initgroups'),\
             patch.object(wrapper.os,'setgid'),patch.object(wrapper.os,'setuid'),patch.object(wrapper.os,'execve',side_effect=execute),\
             patch.object(wrapper.fcntl,'flock',side_effect=concurrent_append):
            with self.assertRaisesRegex(RuntimeError,'exec intercepted'):wrapper.main()
        self.assertEqual(wrapper.NODE_PROOF.read_bytes(),complete)
    def test_oversized_incomplete_tail_is_not_repaired(self):
        original=b'P6NP1|HEADER|1\n'+b'x'*600
        wrapper.NODE_PROOF.write_bytes(original)
        with self.assertRaises(SystemExit):wrapper.main()
        self.assertEqual(wrapper.NODE_PROOF.read_bytes(),original)
    def test_missing_quota_token_is_rejected_before_exec(self):
        wrapper.SLURM_CONFIG.write_text('SlurmctldParameters=other\n')
        with self.assertRaisesRegex(SystemExit,'lacks mandatory MonthlyGpuQuota'): wrapper.main()
    def test_ambiguous_or_insecure_config_is_rejected(self):
        for config in ('Include /etc/slurm/extra.conf\nSlurmctldParameters=MonthlyGpuQuota\n',
                       'SlurmctldParameters=MonthlyGpuQuota\nSlurmctldParameters=other\n'):
            wrapper.SLURM_CONFIG.write_text(config)
            with self.assertRaises(SystemExit): wrapper.main()
        wrapper.SLURM_CONFIG.write_text('SlurmctldParameters=MonthlyGpuQuota\n')
        wrapper.SLURM_CONFIG.chmod(0o666)
        with self.assertRaises(SystemExit): wrapper.main()
    def test_node_ready_binds_boot_process_environment_and_pidfd(self):
        self.node_patch.stop()
        env=dict(os.environ,SLURM_CONF=str(wrapper.SLURM_CONFIG),TIME_QUOTA_NODE_PROOF_FILE=str(wrapper.NODE_PROOF),TIME_QUOTA_NODE_READY_FILE=str(wrapper.NODE_READY))
        proc=subprocess.Popen(['/bin/sleep','30'],env=env)
        try:
            raw=Path('/proc',str(proc.pid),'stat').read_text();ticks=raw[raw.rfind(')')+2:].split()[19]
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            wrapper.NODE_READY.write_text('P6NR1|%d|%s|%s\n'%(proc.pid,ticks,boot));wrapper.NODE_READY.chmod(0o600)
            with patch.object(wrapper,'_validate_release'):
                fd=wrapper._open_verified_node_pidfd(str(wrapper.SLURM_CONFIG));self.pidfds.append(fd)
            proc.terminate();proc.wait(timeout=5)
            with patch.object(wrapper,'_validate_release'):
                with self.assertRaises((OSError,ValueError)): wrapper._open_verified_node_pidfd(str(wrapper.SLURM_CONFIG))
        finally:
            if proc.poll() is None: proc.terminate();proc.wait(timeout=5)
            self.node_patch.start()
    def test_old_or_modified_controller_is_rejected(self):
        self.controller.write_text('old controller without quota')
        with self.assertRaisesRegex(SystemExit,'manifest does not match'): wrapper.main()
    def test_manifest_matching_old_controller_without_capability_is_rejected(self):
        self.controller.write_bytes(b'old controller without quota')
        manifest=json.loads(wrapper.RELEASE_MANIFEST.read_text())
        manifest['files']['controller']['sha256']=hashlib.sha256(self.controller.read_bytes()).hexdigest()
        wrapper.RELEASE_MANIFEST.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(SystemExit,'manifest does not match'): wrapper.main()
    def test_modified_config_is_rejected_even_with_token(self):
        wrapper.SLURM_CONFIG.write_text('SlurmctldParameters=MonthlyGpuQuota,other\n')
        with self.assertRaisesRegex(SystemExit,'manifest does not match'): wrapper.main()
    def test_node_is_pinned_to_reviewed_executable_and_gpu_mapping(self):
        def execute(path,args,env):
            self.assertEqual(path,str(self.node));self.assertEqual(env['TIME_QUOTA_NODE_PROOF_FILE'],str(wrapper.NODE_PROOF))
            self.assertEqual(env['TIME_QUOTA_NODE_READY_FILE'],str(wrapper.NODE_READY))
            self.assertEqual(env['SLURM_CONF'],str(wrapper.SLURM_CONFIG));raise RuntimeError('node exec intercepted')
        with patch.object(wrapper.os,'execve',side_effect=execute):
            with self.assertRaisesRegex(RuntimeError,'node exec intercepted'): wrapper.node_main()
        self.node.write_bytes(b'old node')
        with self.assertRaisesRegex(SystemExit,'manifest does not match'): wrapper.node_main()
    def test_included_gpu_config_is_rejected_even_if_hash_matches(self):
        wrapper.GRES_CONFIG.write_text('Include /tmp/other.conf\n')
        manifest=json.loads(wrapper.RELEASE_MANIFEST.read_text())
        manifest['files']['gres_conf']['sha256']=hashlib.sha256(wrapper.GRES_CONFIG.read_bytes()).hexdigest()
        wrapper.RELEASE_MANIFEST.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(SystemExit,'manifest does not match'): wrapper.main()
    def test_changed_gpu_device_mapping_is_rejected(self):
        wrapper.GRES_CONFIG.write_text('Name=gpu File=/dev/zero\n')
        with self.assertRaisesRegex(SystemExit,'manifest does not match'): wrapper.main()
    def test_insecure_witness_is_rejected(self):
        wrapper.WITNESS.chmod(0o644)
        with self.assertRaises(SystemExit): wrapper.main()
    def test_symlink_witness_is_rejected(self):
        wrapper.WITNESS.unlink();wrapper.WITNESS.symlink_to(self.controller)
        with self.assertRaises(OSError): wrapper.main()

if __name__=='__main__':unittest.main()
