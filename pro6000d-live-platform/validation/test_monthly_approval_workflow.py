"""Exercise the real platform approval workflow using a fake controller."""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
spec=importlib.util.spec_from_file_location('pro6000d_grant_api',ROOT/'backend/pro6000d-training-api.py')
api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)

class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        config=pathlib.Path(self.tmp.name)/'config.json'
        config.write_text(json.dumps({'state_db':str(pathlib.Path(self.tmp.name)/'db.sqlite')}))
        self.service=api.Service(str(config))
        self.service.monthly.summary=lambda uid:{'enforcement_ready':True,'request_available':True}
        self.service.admin_uids=lambda: [1000]
        self.user={'uid':1001,'username':'worker','is_admin':False}
        self.admin={'uid':1000,'username':'admin','is_admin':True}
        class Bridge:
            def __init__(self): self.grants=[]
            def grant(self,*args): self.grants.append(args); return {'applied':True}
        self.bridge=Bridge()
        self.service.monthly_controller_bridge=lambda:self.bridge
    def tearDown(self): self.service.store.db.close(); self.tmp.cleanup()
    def test_disabled_grants_do_not_accept_new_applications(self):
        self.service.monthly.summary=lambda uid:{'enforcement_ready':True,'request_available':False}
        with self.assertRaises(api.ApiError) as exc:
            self.service.create_application(self.user,{'kind':'monthly_gpu_hours',
                'requested':{'gpu_hours':3,'month':api.month_now(),'client_request_id':'request-123'},'reason':'long project'})
        self.assertEqual(exc.exception.code,'monthly_not_enabled')
        self.assertEqual(self.service.store.db.execute('SELECT count(*) FROM applications').fetchone()[0],0)
    def test_approval_is_applied_once_by_controller_and_not_shadow_ledger(self):
        application=self.service.create_application(self.user,{'kind':'monthly_gpu_hours',
            'requested':{'gpu_hours':3,'month':api.month_now(),'client_request_id':'request-123'},'reason':'long project'})
        result=self.service.decide_application(self.admin,application['id'],{'action':'approve'})
        self.assertEqual(result['status'],'APPROVED')
        self.assertEqual(self.bridge.grants,[(1001,int(api.month_now().replace('-','')),10800,application['id'],'uid-1000')])
        self.assertEqual(self.service.store.db.execute('SELECT count(*) FROM monthly_additions').fetchone()[0],0)
        self.assertEqual(self.service.store.db.execute('SELECT state FROM monthly_grant_outbox').fetchone()[0],'APPLIED')
        self.service.finish_monthly_grant(self.admin,application['id'])
        self.assertEqual(len(self.bridge.grants),1)
    def test_approval_does_not_write_grant_if_gate_unready(self):
        application=self.service.create_application(self.user,{'kind':'monthly_gpu_hours',
            'requested':{'gpu_hours':3,'month':api.month_now(),'client_request_id':'request-123'},'reason':'long project'})
        self.service.monthly.summary=lambda uid:{'enforcement_ready':False}
        with self.assertRaises(api.ApiError): self.service.decide_application(self.admin,application['id'],{'action':'approve'})
        self.assertFalse(self.bridge.grants)
        self.assertEqual(self.service.store.db.execute('SELECT status FROM applications').fetchone()[0],'PENDING')

if __name__=='__main__': unittest.main()
