"""Controller-backed view remains read-only until enforcement is verified."""
import importlib.util
import pathlib
import sys
import time
import unittest
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
spec=importlib.util.spec_from_file_location('pro6000d_view_api',ROOT/'backend/pro6000d-training-api.py')
api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
class ControllerViewTests(unittest.TestCase):
    def setUp(self):
        self.s=api.Service.__new__(api.Service)
        self.s.config={'monthly_controller':{'read_enabled':True,'controller_user':'slurm'}}
        self.s.monthly=type('Ledger',(),{'summary':lambda self,uid: {'mode':'staged','enforcement_ready':False}})()
        self.snapshot={'schema_version':2,'uid':1001,'month':int(api.month_now().replace('-','')),
            'granted':{'gpu_seconds':0,'nanoseconds':0},
            'tiers':{x:{'used':{'gpu_seconds':used,'nanoseconds':0}} for x,used in [('debug',18000),('important',30),('normal',0)]},
            'overage':{'gpu_seconds':0,'nanoseconds':0}, 'observed_sec':int(time.time()),'observed_nsec':0,
            'wal_sequence':12,'active_allocation_count':1,'active_gpu_count':1}
    def view(self):
        class Bridge:
            def __init__(self,*args): pass
            def selectable_snapshot(inner,uid): return self.snapshot
            def readiness(inner): return getattr(self,'controller_ready',True)
        with patch.object(api.pwd,'getpwnam',return_value=type('User',(),{'pw_uid':999})()),patch.object(api,'ControllerBridge',Bridge),patch.object(api,'verify_release',return_value=getattr(self,'release_verified',True)):
            return self.s.monthly_controller_snapshot(1001)
    def test_reads_tier_usage_without_claiming_enforcement(self):
        view=self.view()
        self.assertEqual(view['tiers'][0]['used_gpu_seconds'],18000)
        self.assertFalse(view['enforcement_ready'])
        self.assertFalse(view['selectable_tiers_ready'])
    def test_activation_needs_explicit_configuration_and_controller_attestation(self):
        self.s.config['monthly_controller']['activation_enabled']=True
        view=self.view()
        self.assertTrue(view['enforcement_ready'])
        self.assertTrue(view['selectable_tiers_ready'])
        self.assertFalse(view['request_available'])
        self.s.config['monthly_controller']['grant_enabled']=True
        self.assertTrue(self.view()['request_available'])
        self.controller_ready=False
        view=self.view()
        self.assertFalse(view['enforcement_ready'])
        self.controller_ready=True; self.release_verified=False
        self.assertFalse(self.view()['enforcement_ready'])
    def test_old_month_or_stale_snapshot_is_not_displayed_as_current(self):
        self.snapshot['observed_sec']-=100
        view=self.view()
        self.assertFalse(view['usage_verified'])
        self.assertFalse(view['enforcement_ready'])

    def test_capabilities_follow_verified_monthly_state(self):
        self.s.identity=lambda uid: {'uid':uid}
        for ready in (False,True):
            self.s.monthly_controller_snapshot=lambda uid: {'mode':'enforcing' if ready else 'unknown',
                'enforcement_ready':ready,'selectable_tiers_ready':ready,
                'accounting_status':'ok' if ready else 'unknown','request_available':ready}
            result=self.s.handle(1001,{'method':'GET','path':'/api/v1/capabilities'})
            self.assertEqual(result['schema_version'],'1.0')
            self.assertIs(result['monthly_quota']['enforcement_ready'],ready)
            self.assertIs(result['monthly_requests']['available'],ready)
            self.assertEqual(result['monthly_requests']['idempotency'],'owner-request-id-v1')
            self.assertEqual(result['monthly_requests']['reconcile'],'application-grant-v1')

if __name__=='__main__': unittest.main()
