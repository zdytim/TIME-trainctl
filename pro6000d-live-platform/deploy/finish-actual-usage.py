#!/usr/bin/env python3
"""Finish a fenced actual-usage cutover, preserving the live quota ledgers."""
import hashlib, importlib.util, json, os, pwd, subprocess, time
from pathlib import Path
backup=Path('/var/backups/pro6000d-training/actual-usage-20261118T095414Z')
script=Path('/tmp/time26-candidate-3fyoc7N4/cutover.py')
data=script.read_bytes()
if hashlib.sha256(data).hexdigest()!='e923c41d575c946c06acab407c7f68fdf21a39944eac063cbee4445def884805': raise RuntimeError('unexpected cutover tool')
ns={'__name__':'recovery_helpers'};exec(compile(data,str(script),'exec'),ns)
run,atomic,idle,control,wait_base,sha=(*(ns[x] for x in ('run','atomic','idle','control','wait_base','sha')),)
S,M,C=ns['S'],ns['M'],ns['C']
if os.geteuid()!=0 or os.uname().nodename!='ubuntu': raise RuntimeError('requires root on target')
if Path('/opt/slurm/current').resolve()!=ns['P']: raise RuntimeError('candidate is not installed')
cfg=json.loads(C.read_text());manifest=json.loads(M.read_text());original=S.read_text()
if 'State=INACTIVE' not in run('/usr/local/bin/scontrol','show','partition','gpu','-o'): raise RuntimeError('GPU partition is not fenced')
if not manifest.get('actual_usage_unlimited2') is True: raise RuntimeError('actual usage policy is not configured')
for unit in ('slurmctld','slurmd','pro6000d-training'): run('systemctl','is-active','--quiet',unit)
idle(cfg)
record_path=backup/'release.json';record=json.loads(record_path.read_text());record['state']='finishing fenced cutover';atomic(record_path,(json.dumps(record,indent=2)+'\n').encode(),0o600)
stopped_broker=False;stopped_slurm=False;slurm_stop_attempted=False;opened=False
try:
    stopped_broker=True;run('systemctl','stop','pro6000d-training')
    idle(cfg)
    slurm_stop_attempted=True;stopped_slurm=True;run('systemctl','stop','slurmd','slurmctld')
    newconf=ns['conf_state'](original,'UP').encode();atomic(S,newconf,0o644)
    manifest['files']['slurm_conf']['sha256']=sha(S);atomic(M,(json.dumps(manifest,indent=2)+'\n').encode(),0o600)
    run('systemctl','start','slurmd')
    sock=cfg['monthly_controller']['socket_path'];wait_base(sock);stopped_slurm=False
    run('systemctl','start','pro6000d-training');stopped_broker=False
    deadline=time.monotonic()+60
    while True:
        try:
            if control(sock,'ACTUAL_USAGE_UNLIMITED2 0')=='OK ACTUAL_USAGE_UNLIMITED2 SUPPORTED': break
        except (OSError,UnicodeError): pass
        if time.monotonic()>=deadline: raise RuntimeError('actual usage capability unavailable')
        time.sleep(.5)
    part=run('/usr/local/bin/scontrol','show','partition','gpu','-o')
    if 'State=UP' not in part or 'MaxTime=UNLIMITED' not in part: raise RuntimeError('partition state not verified')
    qos=run('/usr/local/bin/sacctmgr','-nP','show','qos','where','name=long','format=Name,MaxWall')
    if qos not in ('long|UNLIMITED','long|'): raise RuntimeError('QoS is not unlimited')
    attpath=backup/'release/pro6000d-live-platform/backend/activation_attestation.py'
    spec=importlib.util.spec_from_file_location('activation_attestation',attpath);att=importlib.util.module_from_spec(spec);spec.loader.exec_module(att)
    if not att.verify_release(str(M),pwd.getpwnam('slurm').pw_uid): raise RuntimeError('release attestation failed')
    admin=pwd.getpwnam('ubuntu');request=json.dumps({'method':'GET','path':'/api/v1/state'})+'\n'
    deadline=time.monotonic()+45
    while True:
        try:
            reply=run('setpriv','--reuid='+str(admin.pw_uid),'--regid='+str(admin.pw_gid),'--init-groups','/usr/local/bin/pro6000d-training-client',input=request,timeout=8)
            data=json.loads(reply);state=data.get('data',{});q=state.get('monthly_quota',{});p=state.get('priority_management',{})
            if data.get('ok') is True and q.get('enforcement_ready') is True and q.get('selectable_tiers_ready') is True and p.get('available') is True and p.get('reconciled') is True: break
        except (OSError,ValueError,subprocess.SubprocessError): pass
        if time.monotonic()>=deadline: raise RuntimeError('platform API readiness failed')
        time.sleep(.5)
    for unit in ('slurmctld','slurmd','pro6000d-training'): run('systemctl','is-active','--quiet',unit)
    opened=True
    if 'error' in record:
        record['initial_cutover_error'] = record.pop('error')
        record['recovery'] = 'staged source permissions corrected; shared clients installed; fenced cutover completed'
    record.update({'state':'deployed','finished_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'changed_components':['slurmctld','slurmd','monthly quota broker','Cockpit UI','shared CLI/MCP','gpu partition','long QoS'],'rollback':'not needed; previous release and ledger evidence retained','verification':'controller capability, release attestation, API quota/priority, partition, QoS, service status'})
    atomic(record_path,(json.dumps(record,indent=2)+'\n').encode(),0o600)
    print(json.dumps(record,ensure_ascii=False))
except BaseException as e:
    record['state']='recovery failed';record['recovery_error']=str(e)
    try:
        if slurm_stop_attempted:
            if not stopped_slurm:
                try: run('/usr/local/bin/scontrol','update','PartitionName=gpu','State=INACTIVE')
                except Exception: pass
            atomic(S,ns['conf_state'](original,'INACTIVE').encode(),0o644)
            manifest['files']['slurm_conf']['sha256']=sha(S);atomic(M,(json.dumps(manifest,indent=2)+'\n').encode(),0o600)
            if stopped_slurm: run('systemctl','start','slurmd')
        if stopped_broker: run('systemctl','start','pro6000d-training')
    except Exception as inner: record['service_recovery_error']=str(inner)
    atomic(record_path,(json.dumps(record,indent=2)+'\n').encode(),0o600)
    print(json.dumps(record,ensure_ascii=False));raise
