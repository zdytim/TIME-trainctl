"""Non-production checks for release inventory and cutover fences."""
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy'))
from release_tree import release_tree
spec=importlib.util.spec_from_file_location('activate_monthly',ROOT/'deploy/activate-monthly.py')
activation=importlib.util.module_from_spec(spec);spec.loader.exec_module(activation)

class ReleaseSafetyTests(unittest.TestCase):
    def test_inventory_binds_content_mode_and_symlink_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'lib').mkdir(); (root/'lib'/'a').write_bytes(b'a'); (root/'lib'/'b').write_bytes(b'b')
            (root/'lib'/'current').symlink_to('a')
            first=release_tree(root)
            (root/'lib'/'a').write_bytes(b'changed')
            self.assertNotEqual(first,release_tree(root))
            (root/'lib'/'a').write_bytes(b'a'); (root/'lib'/'current').unlink(); (root/'lib'/'current').symlink_to('b')
            self.assertNotEqual(first,release_tree(root))
            (root/'lib'/'current').unlink(); (root/'lib'/'current').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError): release_tree(root)
    def test_rejects_world_writable_release_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            item=Path(temp)/'x'; item.write_bytes(b'x'); item.chmod(0o666)
            with self.assertRaises(ValueError): release_tree(temp)
    def test_cutover_rejects_platform_waiters_and_submissions(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'db'; db=sqlite3.connect(path)
            db.execute('CREATE TABLE jobs(state TEXT)'); db.execute("INSERT INTO jobs VALUES ('COMPLETED')"); db.commit()
            self.assertTrue(activation.platform_queue_empty(path))
            for state in ('WAITING_QUOTA','CANCELLING_RESERVATION','SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED'):
                db.execute('INSERT INTO jobs VALUES (?)',(state,)); db.commit()
                self.assertFalse(activation.platform_queue_empty(path))
                db.execute('DELETE FROM jobs WHERE state=?',(state,)); db.commit()
            db.close()
    def test_queue_check_is_all_partitions_and_uses_clean_environment(self):
        class Result: stdout=''; returncode=0
        with patch.object(activation.subprocess,'run',return_value=Result()) as called:
            self.assertTrue(activation.empty_queue())
        args,kwargs=called.call_args
        self.assertIn('--all',args[0]); self.assertEqual(kwargs['env'],activation.SAFE_ENV)
        self.assertNotIn('SQUEUE_USERS',kwargs['env'])
    def test_cutover_requires_only_one_idle_node_and_one_partition(self):
        with patch.object(activation,'run',side_effect=['gpu*','NodeName=pro6000d State=IDLE']):
            activation.require_single_idle_target()
        with patch.object(activation,'run',side_effect=['gpu*\nother','NodeName=pro6000d State=IDLE']):
            with self.assertRaises(RuntimeError): activation.require_single_idle_target()
        with patch.object(activation,'run',side_effect=['gpu*','NodeName=pro6000d State=ALLOCATED']):
            with self.assertRaises(RuntimeError): activation.require_single_idle_target()
    def test_rejects_includes_or_existing_controller_parameters(self):
        conf='SlurmUser=slurm\nPartitionName=gpu Nodes=pro6000d State=UP\n'
        self.assertIn(b'State=INACTIVE',activation.configured_conf(conf,'INACTIVE'))
        self.assertIn(b'State=UP',activation.configured_conf(conf,'UP'))
        self.assertIn(b'SlurmctldParameters=MonthlyGpuQuota',activation.configured_conf(conf,'INACTIVE'))
        for conf in ('Include other.conf\n','SlurmctldParameters=foo\n','PluginDir=/tmp\n','ReconfigFlags=KeepPartState\n',
                     'PartitionName=gpu Nodes=pro6000d # State=UP\n',
                     'PartitionName=gpu Nodes=pro6000d State=UP State=DOWN\n'):
            with self.assertRaises(RuntimeError): activation.configured_conf(conf,'INACTIVE')

if __name__=='__main__': unittest.main()
