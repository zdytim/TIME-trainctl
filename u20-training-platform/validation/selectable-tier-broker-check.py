import importlib.util,json,os,pathlib,sys,tempfile,types
candidate=pathlib.Path(os.environ['TIME_VALIDATION_BROKER_DIR']).resolve(strict=True)
sys.path[:0]=[str(candidate),str(pathlib.Path(__file__).resolve().parents[1] / 'backend')]
spec=importlib.util.spec_from_file_location('fixture_broker',candidate/'pro6000d-training-api.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='time-selected-tier-') as d:
 cfg=pathlib.Path(d)/'config.json';cfg.write_text(json.dumps({'state_db':str(pathlib.Path(d)/'state.sqlite3'),'capacity':{'gpu':4,'cpu':8,'memory_mb':8192}}))
 svc=m.Service(str(cfg));svc.tick=lambda:None;svc.scheduler_available=lambda:True;svc.admission_available=lambda:True;svc.quota=lambda uid:{'gpu_limit':4,'active_job_limit':8};svc.active_usage=lambda uid:{'active_jobs':0};svc.dispatch_waiting=lambda:None
 svc.user=lambda uid:types.SimpleNamespace(pw_uid=uid,pw_dir=d)
 svc.safe_user_path=lambda path,user: d
 ident={'uid':1001,'username':'fixture'}
 def summary(ready=True,remaining=60):return {'selectable_tiers_ready':ready,'tiers':[{'name':'important','remaining_gpu_seconds':remaining}]}
 svc.controller_monthly_snapshot=lambda uid:summary(False)
 request={'client_request_id':'fixture-tier-001','name':'fixture','command':'python train.py','selected_tier':'important','qos':'normal','gpu':1,'cpu':1,'memory_mb':1024,'time_minutes':1800,'workdir':d,'output_path':d+'/train-%j.out','environment':'system'}
 try:svc.submit(ident,request);raise AssertionError('unsupported tier accepted')
 except m.ApiError as e:assert e.code=='selected_tier_unavailable',e.code
 svc.controller_monthly_snapshot=lambda uid:summary(True)
 svc.config['monthly_controller']={'selectable_activation_verified':True}
 try:svc.submit(ident,{k:v for k,v in request.items() if k != 'selected_tier'});raise AssertionError('missing tier accepted')
 except m.ApiError as e:assert e.code=='selected_tier_required',e.code
 job=svc.submit(ident,request);assert job['selected_tier']=='important'
 assert job['requested_minutes']==1800
 assert svc.row_job(svc.store.db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone())['selected_tier']=='important'
 assert svc.submit(ident,request)['id']==job['id']
 try:svc.submit(ident,{**request,'selected_tier':'normal'});raise AssertionError('mismatched retry accepted')
 except m.ApiError as e:assert e.code=='idempotency_conflict',e.code
 svc.controller_monthly_snapshot=lambda uid:summary(True,0)
 try:svc.submit(ident,{**request,'client_request_id':'fixture-tier-002'});raise AssertionError('exhausted tier accepted')
 except m.ApiError as e:assert e.code=='selected_tier_exhausted',e.code
 assert svc.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
 svc.store.db.close()
print('PASS: tier capability, required selection, long selected job, persistence, retries and exhausted tier with private database')
