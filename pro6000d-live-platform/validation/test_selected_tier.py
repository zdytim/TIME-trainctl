"""Offline API checks for preserving the user-selected monthly tier."""
import importlib.util
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
spec = importlib.util.spec_from_file_location('pro6000d_api_validation', ROOT / 'backend/pro6000d-training-api.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

class SelectedTierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = api.Service.__new__(api.Service)
        self.service.user = lambda uid: types.SimpleNamespace(pw_uid=uid, pw_name="worker", pw_dir=self.tmp.name)
        self.service.safe_user_path = lambda path, user: path
        self.service.qos = {'normal': {'max_minutes': 1440}}
        self.service.config = {'capacity': {'gpu': 4, 'cpu': 16, 'memory_mb': 65536}}
        self.service.monthly = types.SimpleNamespace(summary=lambda uid: {'enforcement_ready': True, 'selectable_tiers_ready': True})
        self.job = {'name':'train','command':'true','gpu':1,'cpu':1,'memory_mb':1024,'time_minutes':60,
                    'qos':'normal','workdir':self.tmp.name,'environment':'system','selected_tier':'important'}
    def tearDown(self): self.tmp.cleanup()
    def test_selected_tier_survives_validation(self):
        job = self.service.validate_job({'uid': 1001}, self.job)
        self.assertEqual(job['selected_tier'], 'important')
    def test_invalid_tier_is_rejected(self):
        self.job['selected_tier'] = 'other'
        with self.assertRaises(api.ApiError) as exc: self.service.validate_job({'uid': 1001}, self.job)
        self.assertEqual(exc.exception.code, 'invalid_selected_tier')
    def test_active_gate_requires_an_explicit_selected_tier(self):
        del self.job['selected_tier']
        with self.assertRaises(api.ApiError) as exc: self.service.validate_job({'uid':1001},self.job)
        self.assertEqual(exc.exception.code,'selected_tier_required')
    def test_configured_gate_unavailable_does_not_accept_legacy_submission(self):
        self.service.config['monthly_controller']={'activation_enabled':True}
        self.service.monthly.summary=lambda uid:{'enforcement_ready':False,'selectable_tiers_ready':False}
        del self.job['selected_tier']
        with self.assertRaises(api.ApiError) as exc: self.service.validate_job({'uid':1001},self.job)
        self.assertEqual(exc.exception.code,'monthly_gate_unavailable')
    def test_unready_enforcement_refuses_selection(self):
        self.service.monthly.summary = lambda uid: {'enforcement_ready': False, 'selectable_tiers_ready': False}
        with self.assertRaises(api.ApiError) as exc: self.service.validate_job({'uid': 1001}, self.job)
        self.assertEqual(exc.exception.code, 'selected_tier_unavailable')
    def test_dispatch_reserves_then_binds_same_job(self):
        config_path = pathlib.Path(self.tmp.name) / 'dispatch-config.json'
        config_path.write_text(json.dumps({'state_db': str(pathlib.Path(self.tmp.name) / 'dispatch.sqlite3'),
                                           'capacity': {'gpu':4,'cpu':16,'memory_mb':65536},
                                           'job_script_dir': self.tmp.name}))
        service = api.Service(str(config_path))
        service.user = self.service.user
        service.safe_user_path = self.service.safe_user_path
        service.monthly.summary = self.service.monthly.summary
        service.tick = lambda: None
        service.scheduler_available = lambda: True
        service.admission_available = lambda: True
        service.dispatch_waiting = lambda: None
        service.quota = lambda uid: {'gpu_limit':4,'concurrent_job_limit':4,'active_job_limit':8}
        service.render_script = lambda *args: '#!/bin/bash\ntrue\n'
        result = service.submit({'uid':1001,'username':'worker','is_admin':False}, self.job)
        row = service.store.db.execute('SELECT * FROM jobs WHERE id=?', (result['id'],)).fetchone()
        calls = []
        class Coordinator:
            def hold(self, *args, **kwargs): calls.append(('hold',args))
            def bind(self, *args): calls.append(('bind',args))
        service.monthly_reservation_coordinator = lambda: Coordinator()
        def run(argv, timeout=0):
            calls.append(('submit',argv))
            return 0, '42', ''
        with patch.object(api, 'run', run), patch.object(api.grp, 'getgrnam', return_value=types.SimpleNamespace(gr_gid=1001)):
            job = service.dispatch_job({'uid':1001},row,service.row_job(row))
        self.assertEqual([call[0] for call in calls], ['hold','submit','bind'])
        self.assertIn('pro6000d-job:'+result['id']+':tier=important', calls[1][1])
        self.assertIn('--nodes=1-1', calls[1][1])
        self.assertEqual(calls[2][1], (result['id'],1001,42))
        self.assertEqual((job['scheduler_id'],job['state']), ('42','PENDING'))
        service.store.db.close()
    def test_waiting_broker_dispatches_selected_debug_before_normal(self):
        config_path=pathlib.Path(self.tmp.name)/'priority.json'
        config_path.write_text(json.dumps({'state_db':str(pathlib.Path(self.tmp.name)/'priority.sqlite3'),
            'capacity':{'gpu':4,'cpu':16,'memory_mb':65536}}))
        service=api.Service(str(config_path));service.user=self.service.user;service.safe_user_path=self.service.safe_user_path
        service.monthly.summary=self.service.monthly.summary;service.tick=lambda:None
        service.scheduler_available=lambda:True;service.admission_available=lambda:True;service.dispatch_waiting=lambda:None
        service.quota=lambda uid:{'gpu_limit':4,'concurrent_job_limit':4,'active_job_limit':8}
        ident={'uid':1001,'username':'worker','is_admin':False}
        normal=service.submit(ident,{**self.job,'selected_tier':'normal'})
        debug=service.submit(ident,{**self.job,'selected_tier':'debug'})
        service.store.db.execute("UPDATE jobs SET created_at='2026-01-01T00:00:00Z' WHERE id=?",(normal['id'],))
        service.store.db.execute("UPDATE jobs SET created_at='2026-01-02T00:00:00Z' WHERE id=?",(debug['id'],))
        service.identity=lambda uid:ident
        service.active_usage=lambda uid:{'gpu':0,'concurrent_jobs':0}
        service.last_reconcile_ok=True;calls=[]
        service.dispatch_job=lambda ident,row,job,temporary=False:calls.append(row['id'])
        api.Service.dispatch_waiting(service)
        self.assertEqual(calls,[debug['id'],normal['id']])
        service.store.db.close()
    def test_blocked_prefix_does_not_starve_later_eligible_job(self):
        import sqlite3
        db=sqlite3.connect(':memory:',isolation_level=None); db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, origin TEXT, state TEXT, selected_tier TEXT, created_at TEXT, owner_uid INTEGER, temporary_application_id TEXT, reason_code TEXT, reason_text TEXT, updated_at TEXT)')
        db.executescript('CREATE TABLE job_dispatch_order(seq INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL UNIQUE); CREATE TRIGGER jobs_dispatch_order_insert AFTER INSERT ON jobs BEGIN INSERT OR IGNORE INTO job_dispatch_order(job_id) VALUES (NEW.id); END;')
        db.executemany('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
            [(f'{i:04d}','platform','WAITING_QUOTA','debug','2026-01-01',1,None,None,None,None) for i in range(256)]+[('last','platform','WAITING_QUOTA','normal','2026-01-02',2,None,None,None,None)])
        service=self.service; service.store=types.SimpleNamespace(db=db)
        service.last_reconcile_ok=True; service.admission_available=lambda:True
        service.identity=lambda uid:{'uid':uid}
        service.monthly.block_reason=lambda uid:'exhausted' if uid==1 else None
        service.quota=lambda uid:{'gpu_limit':4,'concurrent_job_limit':4}
        service.active_usage=lambda uid:{'gpu':0,'concurrent_jobs':0}
        service.row_job=lambda row:{'gpu':1,'time_minutes':60,'qos':'normal'}
        service.validate_job=lambda *args,**kwargs:None
        sent=[]; service.dispatch_job=lambda ident,row,job,temporary=False:sent.append(row['id'])
        api.Service.dispatch_waiting(service)
        self.assertLessEqual(db.execute("SELECT count(*) FROM jobs WHERE reason_code='monthly_quota_wait'").fetchone()[0],64)
        self.assertEqual(sent,[])
        for _ in range(3): api.Service.dispatch_waiting(service)
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)',('late-debug','platform','WAITING_QUOTA','debug','2026-01-03',2,None,None,None,None))
        api.Service.dispatch_waiting(service)
        self.assertEqual(sent,['late-debug','last'])
        db.close()
    def test_continuous_arrivals_do_not_prevent_rechecking_old_waiter(self):
        import sqlite3
        db=sqlite3.connect(':memory:',isolation_level=None); db.row_factory=sqlite3.Row
        db.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY,origin TEXT,state TEXT,selected_tier TEXT,created_at TEXT,owner_uid INTEGER,temporary_application_id TEXT,reason_code TEXT,reason_text TEXT,updated_at TEXT); CREATE TABLE job_dispatch_order(seq INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL UNIQUE); CREATE TRIGGER jobs_dispatch_order_insert AFTER INSERT ON jobs BEGIN INSERT OR IGNORE INTO job_dispatch_order(job_id) VALUES (NEW.id); END;")
        db.executemany('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)',[(f'old-{i:03}','platform','WAITING_QUOTA','debug','2026-01-01',1,None,None,None,None) for i in range(70)])
        service=self.service; service.store=types.SimpleNamespace(db=db); service.last_reconcile_ok=True
        service.admission_available=lambda:True; service.identity=lambda uid:{'uid':uid}
        blocked=[True]; service.monthly.block_reason=lambda uid:'exhausted' if blocked[0] else None
        service.quota=lambda uid:{'gpu_limit':4,'concurrent_job_limit':4}
        service.active_usage=lambda uid:{'gpu':0,'concurrent_jobs':0}
        service.row_job=lambda row:{'gpu':1,'time_minutes':60,'qos':'normal'}
        service.validate_job=lambda *args,**kwargs:None
        sent=[]
        def dispatch(ident,row,job,temporary=False):
            sent.append(row['id']); db.execute("UPDATE jobs SET state='PENDING' WHERE id=?",(row['id'],))
        service.dispatch_job=dispatch
        api.Service.dispatch_waiting(service)
        blocked[0]=False
        for cycle in range(12):
            db.executemany('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)',[(f'new-{cycle}-{i}','platform','WAITING_QUOTA','debug','2026-01-02',1,None,None,None,None) for i in range(10)])
            api.Service.dispatch_waiting(service)
        self.assertIn('old-000',sent)
        db.close()
    def test_verified_controller_gate_ignores_legacy_staged_block(self):
        config_path=pathlib.Path(self.tmp.name)/'legacy.json'
        config_path.write_text(json.dumps({'state_db':str(pathlib.Path(self.tmp.name)/'legacy.sqlite3'),
            'capacity':{'gpu':4,'cpu':16,'memory_mb':65536}}))
        service=api.Service(str(config_path));service.user=self.service.user;service.safe_user_path=self.service.safe_user_path
        service.monthly.summary=self.service.monthly.summary;service.tick=lambda:None
        service.scheduler_available=lambda:True;service.admission_available=lambda:True;service.dispatch_waiting=lambda:None
        service.quota=lambda uid:{'gpu_limit':4,'concurrent_job_limit':4,'active_job_limit':8}
        ident={'uid':1001,'username':'worker','is_admin':False};job=service.submit(ident,self.job)
        service.config['monthly_controller']={'activation_enabled':True}
        service.monthly_controller_snapshot=lambda uid:{'enforcement_ready':True,'selectable_tiers_ready':True}
        service.monthly.block_reason=lambda uid:'old staged ledger blocks'
        service.identity=lambda uid:ident;service.active_usage=lambda uid:{'gpu':0,'concurrent_jobs':0}
        service.last_reconcile_ok=True;calls=[]
        service.dispatch_job=lambda ident,row,job,temporary=False:calls.append(row['id'])
        api.Service.dispatch_waiting(service)
        self.assertEqual(calls,[job['id']]);service.store.db.close()
    def test_nonzero_sbatch_is_reconciled_without_dropping_hold(self):
        config_path=pathlib.Path(self.tmp.name)/'uncertain-submit.json'
        config_path.write_text(json.dumps({'state_db':str(pathlib.Path(self.tmp.name)/'submit.sqlite3'),
            'capacity':{'gpu':4,'cpu':16,'memory_mb':65536},'job_script_dir':self.tmp.name}))
        service=api.Service(str(config_path));service.user=self.service.user;service.safe_user_path=self.service.safe_user_path
        service.monthly.summary=self.service.monthly.summary;service.tick=lambda:None
        service.scheduler_available=lambda:True;service.admission_available=lambda:True;service.dispatch_waiting=lambda:None
        service.quota=lambda uid:{'gpu_limit':4,'concurrent_job_limit':4,'active_job_limit':8}
        service.render_script=lambda *args:'#!/bin/bash\ntrue\n'
        result=service.submit({'uid':1001,'username':'worker','is_admin':False},self.job)
        row=service.store.db.execute('SELECT * FROM jobs WHERE id=?',(result['id'],)).fetchone()
        calls=[]
        class Coordinator:
            def hold(self,*args,**kwargs):calls.append('hold')
            def cancel(self,*args):calls.append('cancel')
        service.monthly_reservation_coordinator=lambda:Coordinator()
        with patch.object(api,'run',return_value=(1,'','transport failure')),patch.object(api.grp,'getgrnam',return_value=types.SimpleNamespace(gr_gid=1001)):
            job=service.dispatch_job({'uid':1001},row,service.row_job(row))
        self.assertEqual(job['state'],'SUBMITTING')
        self.assertEqual(calls,['hold'])
        service.store.db.close()
    def test_cancel_fences_submission_before_releasing_hold(self):
        config_path = pathlib.Path(self.tmp.name) / 'cancel-config.json'
        config_path.write_text(json.dumps({'state_db': str(pathlib.Path(self.tmp.name) / 'cancel.sqlite3'),
                                           'capacity': {'gpu':4,'cpu':16,'memory_mb':65536}}))
        service = api.Service(str(config_path))
        service.user = self.service.user
        service.safe_user_path = self.service.safe_user_path
        service.monthly.summary = self.service.monthly.summary
        service.tick = lambda: None
        service.scheduler_available = lambda: True
        service.admission_available = lambda: True
        service.dispatch_waiting = lambda: None
        service.quota = lambda uid: {'gpu_limit':4,'concurrent_job_limit':4,'active_job_limit':8}
        ident={'uid':1001,'username':'worker','is_admin':False}
        job=service.submit(ident,self.job)
        calls=[]
        class Coordinator:
            def cancel(self, jid, uid):
                state=service.store.db.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()[0]
                calls.append((jid,uid,state))
        service.monthly_reservation_coordinator=lambda: Coordinator()
        result=service.cancel(ident,job['id'])
        self.assertEqual(calls,[(job['id'],1001,'CANCELLING_RESERVATION')])
        self.assertEqual(result['state'],'CANCELLED')
        service.store.db.close()
    def test_uncertain_cancellation_stays_fenced_and_can_reconcile(self):
        config_path = pathlib.Path(self.tmp.name) / 'cancel-uncertain.json'
        config_path.write_text(json.dumps({'state_db': str(pathlib.Path(self.tmp.name) / 'uncertain.sqlite3'),
                                           'capacity': {'gpu':4,'cpu':16,'memory_mb':65536}}))
        service = api.Service(str(config_path))
        service.user = self.service.user; service.safe_user_path = self.service.safe_user_path
        service.monthly.summary = self.service.monthly.summary
        service.tick = lambda: None; service.scheduler_available = lambda: True
        service.admission_available = lambda: True; service.dispatch_waiting = lambda: None
        service.quota = lambda uid: {'gpu_limit':4,'concurrent_job_limit':4,'active_job_limit':8}
        ident={'uid':1001,'username':'worker','is_admin':False}
        job=service.submit(ident,self.job)
        class Coordinator:
            available = False
            def cancel(self, jid, uid):
                if not self.available: raise api.ReservationUncertain('lost response')
        coordinator=Coordinator()
        service.monthly_reservation_coordinator=lambda: coordinator
        result=service.cancel(ident,job['id'])
        self.assertEqual(result['state'],'CANCELLING_RESERVATION')
        self.assertEqual(result['reason_code'],'cancellation_unconfirmed')
        coordinator.available=True
        service.reconcile_reservation_cancellations()
        self.assertEqual(service.store.db.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()[0], 'CANCELLED')
        service.store.db.close()
    def test_expired_approval_fences_then_releases_selected_hold(self):
        config_path = pathlib.Path(self.tmp.name) / 'expire-config.json'
        config_path.write_text(json.dumps({'state_db': str(pathlib.Path(self.tmp.name) / 'expire.sqlite3'),
                                           'capacity': {'gpu':4,'cpu':16,'memory_mb':65536}}))
        service = api.Service(str(config_path))
        service.user = self.service.user; service.safe_user_path = self.service.safe_user_path
        service.monthly.summary = self.service.monthly.summary
        service.tick = lambda: None; service.scheduler_available = lambda: True
        service.admission_available = lambda: True; service.dispatch_waiting = lambda: None
        service.quota = lambda uid: {'gpu_limit':4,'concurrent_job_limit':4,'active_job_limit':8}
        ident={'uid':1001,'username':'worker','is_admin':False}
        job=service.submit(ident,self.job)
        service.store.db.execute("INSERT INTO applications(id,owner_uid,owner_name,kind,status,requested_json,reason,expires_at,bound_job_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ('app',1001,'worker','temporary_job','APPROVED','{}','reason','2000-01-01T00:00:00+00:00',job['id'],'now','now'))
        service.store.db.execute('UPDATE jobs SET temporary_application_id=? WHERE id=?',('app',job['id']))
        calls=[]
        class Coordinator:
            def cancel(self,jid,uid): calls.append(service.store.db.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()[0])
        service.monthly_reservation_coordinator=lambda: Coordinator()
        service.expire_applications()
        row=service.store.db.execute('SELECT state,cancel_target FROM jobs WHERE id=?',(job['id'],)).fetchone()
        self.assertEqual((row['state'],row['cancel_target']),('CANCELLING_RESERVATION','EXPIRED'))
        service.reconcile_reservation_cancellations()
        self.assertEqual(calls,['CANCELLING_RESERVATION'])
        self.assertEqual(service.store.db.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()[0], 'EXPIRED')
        service.store.db.close()
    def test_submit_persists_selected_tier_in_real_sqlite_schema(self):
        config_path = pathlib.Path(self.tmp.name) / 'config.json'
        config_path.write_text(json.dumps({'state_db': str(pathlib.Path(self.tmp.name) / 'state.sqlite3'),
                                           'capacity': {'gpu':4,'cpu':16,'memory_mb':65536}}))
        service = api.Service(str(config_path))
        service.user = self.service.user
        service.safe_user_path = self.service.safe_user_path
        service.monthly.summary = self.service.monthly.summary
        service.tick = lambda: None
        service.scheduler_available = lambda: True
        service.admission_available = lambda: True
        service.dispatch_waiting = lambda: None
        service.quota = lambda uid: {'gpu_limit':4,'concurrent_job_limit':4,'active_job_limit':8}
        job = service.submit({'uid':1001,'username':'worker','is_admin':False}, self.job)
        row = service.store.db.execute('SELECT selected_tier,state FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual((row['selected_tier'],row['state']), ('important','WAITING_QUOTA'))
        service.store.db.close()
    def test_row_job_preserves_stored_tier(self):
        row = {'name':'train','qos':'normal','requested_gpu':1,'requested_cpu':1,'requested_mem_mb':1024,
               'requested_minutes':60,'workdir':self.tmp.name,'env_name':'system','command':'true',
               'output_path':self.tmp.name+'/out','selected_tier':'normal'}
        self.assertEqual(self.service.row_job(row)['selected_tier'], 'normal')

if __name__ == '__main__': unittest.main()
