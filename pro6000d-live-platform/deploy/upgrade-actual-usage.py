#!/usr/bin/env python3
"""Quiescent pro6000d actual-usage release. Preserve live quota ledgers."""
import datetime, hashlib, importlib.util, json, os, pwd, re, shutil, socket, sqlite3, stat, subprocess, tarfile, tempfile, time
from pathlib import Path
B=Path('/tmp/time26-candidate-3fyoc7N4')
P=Path('/opt/slurm/26.05.5-actual-usage-20261118')
OLD=Path('/opt/slurm/26.05.5-timequota-priority-20261116')
M=Path('/etc/pro6000d-training/monthly-release.json')
C=Path('/etc/pro6000d-training/config.json')
S=Path('/etc/slurm/slurm.conf')
STAGE=B/'stage/opt/slurm/26.05.5-actual-usage-20261118'
ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin','LANG':'C','SLURM_CONF':str(S)}
COMMIT='09cd9d118c0c8e01faee55b9196fd8104c0b31f0'
EXPECTED={'release.tar.gz':'b04bbb5a7ed0cbcec9f206ee943b2ac00428f8ae33d4bf55d0dfaacce4921fd5','client-old.tar.gz':'064c59c2dc4868c3974c439eb2cafdfe46011d2f378bc5ae42a6de92d28aada9'}
def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def run(*args, input=None, timeout=30):
    return subprocess.run(args,input=input,text=True,capture_output=True,check=True,timeout=timeout,env=ENV).stdout.strip()

def atomic(path,data,mode):
    path=Path(path); fd,tmp=tempfile.mkstemp(prefix='.actual-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fchmod(f.fileno(),mode); os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def conf_state(original, state):
    lines=original.splitlines(keepends=True); found=0
    for i,line in enumerate(lines):
        if re.match(r'^\s*PartitionName=gpu(?:\s|$)',line):
            found+=1
            for key,val in (('DefaultTime','UNLIMITED'),('MaxTime','UNLIMITED'),('State',state)):
                line,n=re.subn(r'(?<!\S)'+key+r'=\S+',key+'='+val,line)
                if n!=1: raise RuntimeError('unexpected partition '+key)
            lines[i]=line
    if found!=1: raise RuntimeError('unexpected GPU partition configuration')
    return ''.join(lines)

def safe_extract(archive,dest):
    with tarfile.open(archive,'r:gz') as t:
        for m in t:
            p=Path(m.name)
            if p.is_absolute() or '..' in p.parts or not (m.isdir() or m.isfile()): raise RuntimeError('unsafe archive')
        t.extractall(dest)

def idle(cfg):
    if run('/usr/local/bin/squeue','--all','-h','-o','%i %T'): raise RuntimeError('scheduler jobs exist')
    gpu=run('/usr/bin/systemd-run','--quiet','--wait','--pipe','--collect','-p','CPUWeight=1','/usr/bin/nvidia-smi','--query-compute-apps=pid','--format=csv,noheader,nounits')
    if gpu: raise RuntimeError('GPU compute processes exist')
    with sqlite3.connect('file:'+cfg['state_db']+'?mode=ro',uri=True,timeout=10) as d:
        if d.execute("SELECT 1 FROM jobs WHERE state IN ('WAITING_QUOTA','CANCELLING_RESERVATION','SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED') LIMIT 1").fetchone(): raise RuntimeError('platform jobs exist')
        if d.execute("SELECT 1 FROM monthly_dispatch_holds WHERE state NOT IN ('CLOSED','CANCELLED') LIMIT 1").fetchone(): raise RuntimeError('open holds exist')

def control(path,command):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(5);s.connect(path);s.sendall((command+'\n').encode());b=b''
        while b'\n' not in b and len(b)<4096:
            chunk=s.recv(4096)
            if not chunk: break
            b+=chunk
        return b.decode().strip()

def wait_base(sock):
    deadline=time.monotonic()+60
    while True:
        try:
            if control(sock,'READY1 0')=='OK TIME26_SELECTABLE_ENDING2_ROLL1_RANK1_CANCEL_BOUND1_AUTOCANCEL1_RECONCILE1_NODEPROOF1_SELECT_DONE1_GPUMAP1_CANCEL_EMPTY1': return
        except (OSError,UnicodeError): pass
        if time.monotonic()>=deadline: raise RuntimeError('controller base readiness failed')
        time.sleep(.5)

def main():
    if os.geteuid()!=0 or os.uname().nodename!='ubuntu': raise RuntimeError('requires root on target')
    if Path('/opt/slurm/current').resolve()!=OLD: raise RuntimeError('unexpected current release')
    bstat=B.lstat()
    if not stat.S_ISDIR(bstat.st_mode) or stat.S_IMODE(bstat.st_mode)!=0o700 or bstat.st_uid not in (0,pwd.getpwnam('ubuntu').pw_uid): raise RuntimeError('unsafe staging directory')
    os.chown(B,0,0);os.chmod(B,0o700)
    for root,dirs,files in os.walk(B,followlinks=False):
        os.chown(root,0,0)
        for n in dirs+files: os.chown(Path(root)/n,0,0,follow_symlinks=False)
    for name,digest in EXPECTED.items():
        if sha(B/name)!=digest: raise RuntimeError('archive hash mismatch '+name)
    if sha(STAGE/'sbin/slurmctld')!='cc83f42a86dc8149e7bdda3eee2bcf06864ad980ae345f1a3e2285c011bce0ca': raise RuntimeError('controller hash mismatch')
    if sha(STAGE/'sbin/slurmd')!='bbec11037960c5810505cc6da2fbc526882806fb76de2987b8914577f0a6b8ea': raise RuntimeError('node hash mismatch')
    if run('/usr/local/bin/sacctmgr','-nP','show','qos','where','name=long','format=Name,MaxWall')!='long|3-00:00:00': raise RuntimeError('unexpected QoS baseline')
    cfg=json.loads(C.read_text());manifest=json.loads(M.read_text());original=S.read_text(); idle(cfg)
    for unit in ('slurmctld','slurmd','pro6000d-training'): run('systemctl','is-active','--quiet',unit)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=Path('/var/backups/pro6000d-training')/('actual-usage-'+stamp);backup.mkdir(mode=0o700)
    release=backup/'release'; old=backup/'old';release.mkdir();old.mkdir();safe_extract(B/'release.tar.gz',release);safe_extract(B/'client-old.tar.gz',old)
    spec=importlib.util.spec_from_file_location('release_tree',release/'pro6000d-live-platform/deploy/release_tree.py');tree=importlib.util.module_from_spec(spec);spec.loader.exec_module(tree)
    expected_tree='0e8628e69ab0a3fbfebad35c6de5d2f0e89e66428663f5a4205be42125b2bece'
    if hashlib.sha256(tree.release_tree(STAGE)).hexdigest()!=expected_tree: raise RuntimeError('staged tree hash mismatch')
    spec=importlib.util.spec_from_file_location('activation_attestation',release/'pro6000d-live-platform/backend/activation_attestation.py');attest=importlib.util.module_from_spec(spec);spec.loader.exec_module(attest)
    def platform_ready():
        if not attest.verify_release(str(M),pwd.getpwnam('slurm').pw_uid): return False
        admin=pwd.getpwnam('ubuntu')
        request=json.dumps({'method':'GET','path':'/api/v1/state'})+'\n'
        reply=run('setpriv','--reuid='+str(admin.pw_uid),'--regid='+str(admin.pw_gid),'--init-groups','/usr/local/bin/pro6000d-training-client',input=request,timeout=8)
        data=json.loads(reply); state=data.get('data',{}); quota=state.get('monthly_quota',{});priority=state.get('priority_management',{})
        return data.get('ok') is True and quota.get('enforcement_ready') is True and quota.get('selectable_tiers_ready') is True and priority.get('available') is True and priority.get('reconciled') is True
    record={'commit':COMMIT,'target':os.uname().nodename,'started_utc':stamp,'backup':str(backup),'state':'preflight','qos_long_before':'3-00:00:00','rollback':'not attempted'}
    def mark(state): record['state']=state;atomic(backup/'release.json',(json.dumps(record,indent=2)+'\n').encode(),0o600)
    paths=[S,M,Path('/usr/local/libexec/pro6000d-quota-controller')]
    for n in ('controller_bridge.py','monthly_reservation.py','pro6000d-training-api.py','strict_admission.py'): paths.append(Path('/usr/local/lib/pro6000d-training')/n)
    for n in ('app.js','monthly-ui.js'): paths.append(Path('/usr/share/cockpit/pro6000d-training')/n)
    for path in paths:
        if path.is_symlink() or not path.is_file(): raise RuntimeError('unexpected backup target '+str(path))
        dest=backup/'files'/str(path).lstrip('/');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
    with sqlite3.connect(cfg['state_db']) as src,sqlite3.connect(str(backup/'state.sqlite3')) as dst: src.backup(dst)
    os.chmod(backup/'state.sqlite3',0o600);mark('backed up')
    broker_stopped=False;fenced=False;started=False
    broker_stop_attempted=False;fence_attempted=False;scheduler_stop_attempted=False;files_changed=False;qos_change_attempted=False
    try:
        broker_stop_attempted=True;mark('broker stop requested')
        run('systemctl','stop','pro6000d-training');broker_stopped=True
        idle(cfg)
        fence_attempted=True;mark('partition fence requested')
        run('/usr/local/bin/scontrol','update','PartitionName=gpu','State=INACTIVE');fenced=True
        if 'State=INACTIVE' not in run('/usr/local/bin/scontrol','show','partition','gpu','-o'): raise RuntimeError('partition fence failed')
        idle(cfg);mark('partition fenced, no jobs or GPU processes')
        scheduler_stop_attempted=True;mark('scheduler stop requested')
        run('systemctl','stop','slurmd','slurmctld');mark('idle scheduler stopped')
        for source in (Path('/var/lib/slurm/slurmctld/monthly-quota.wal'),Path('/var/lib/slurm/slurmctld/monthly-quota.checkpoint'),Path('/var/lib/pro6000d-monthly/witness'),Path('/var/lib/pro6000d-monthly/node-proof.wal')):
            if source.is_file():
                dest=backup/'evidence'/str(source).lstrip('/');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
        if P.exists(): raise RuntimeError('new prefix already exists')
        tmp=P.with_name('.'+P.name+'-'+stamp);shutil.copytree(STAGE,tmp,symlinks=True)
        for root,dirs,files in os.walk(tmp,followlinks=False):
            os.chown(root,0,0)
            for n in dirs+files: os.chown(Path(root)/n,0,0,follow_symlinks=False)
        if hashlib.sha256(tree.release_tree(tmp)).hexdigest()!=expected_tree: raise RuntimeError('installed tree hash mismatch')
        os.rename(tmp,P)
        original_mode=stat.S_IMODE(S.stat().st_mode);files_changed=True;mark('configuration update requested')
        atomic(S,conf_state(original,'INACTIVE').encode(),original_mode)
        qos_change_attempted=True;mark('QoS update requested')
        run('/usr/local/bin/sacctmgr','-i','modify','qos','where','name=long','set','MaxWall=UNLIMITED')
        if run('/usr/local/bin/sacctmgr','-nP','show','qos','where','name=long','format=Name,MaxWall') not in ('long|UNLIMITED','long|'): raise RuntimeError('QoS update not verified')
        tmp_link=Path('/opt/slurm/.actual-current-'+stamp);os.symlink(str(P),tmp_link);os.replace(tmp_link,'/opt/slurm/current')
        base=release/'pro6000d-live-platform'
        atomic('/usr/local/libexec/pro6000d-quota-controller',(base/'deploy/quota-controller-wrapper.py').read_bytes(),0o755)
        for n in ('controller_bridge.py','monthly_reservation.py','pro6000d-training-api.py','strict_admission.py'): atomic(Path('/usr/local/lib/pro6000d-training')/n,(base/'backend'/n).read_bytes(),0o644)
        for n in ('app.js','monthly-ui.js'): atomic(Path('/usr/share/cockpit/pro6000d-training')/n,(base/'cockpit-plugin/pro6000d-training'/n).read_bytes(),0o644)
        files={'controller':P/'sbin/slurmctld','node':P/'sbin/slurmd','submit':P/'lib/slurm/job_submit_pro6000d.so','scheduler':P/'lib/slurm/sched_backfill.so','select':P/'lib/slurm/select_cons_tres.so','cgroup':P/'lib/slurm/cgroup_v2.so','slurm_conf':S,'gres_conf':Path('/etc/slurm/gres.conf'),'cgroup_conf':Path('/etc/slurm/cgroup.conf')}
        manifest['files']={k:{'path':str(v.resolve(strict=True)),'sha256':sha(v)} for k,v in files.items()};manifest['files']['node_proof']={'path':'/var/lib/pro6000d-monthly/node-proof.wal'};manifest['actual_usage_unlimited2']=True
        atomic(M,(json.dumps(manifest,indent=2)+'\n').encode(),0o600)
        # Once the new controller starts, never restore an older binary over its WAL.
        record['rollback']='after candidate startup use compatible controller and preserve WAL';mark('candidate startup boundary')
        started=True;run('systemctl','daemon-reload');run('systemctl','start','slurmd')
        sock=cfg['monthly_controller']['socket_path'];wait_base(sock)
        run('systemctl','start','pro6000d-training');broker_stopped=False
        deadline=time.monotonic()+60
        while True:
            try:
                if control(sock,'ACTUAL_USAGE_UNLIMITED2 0')=='OK ACTUAL_USAGE_UNLIMITED2 SUPPORTED' and platform_ready(): break
            except (OSError,UnicodeError,ValueError,KeyError,subprocess.SubprocessError): pass
            if time.monotonic()>=deadline: raise RuntimeError('controller actual-usage readiness failed')
            time.sleep(.5)
        idle(cfg);mark('candidate ready behind fence')
        run('systemctl','stop','pro6000d-training');broker_stopped=True
        run('systemctl','stop','slurmd','slurmctld')
        atomic(S,conf_state(original,'UP').encode(),original_mode);manifest['files']['slurm_conf']['sha256']=sha(S);atomic(M,(json.dumps(manifest,indent=2)+'\n').encode(),0o600)
        # Install both shared clients while dispatch remains stopped.
        cli=release/'pro6000d-cli-mcp';oldcli=old/'pro6000d-cli-mcp'
        run('/usr/bin/python3',str(cli/'upgrade-shared.py'),str(cli),str(oldcli),'--execute',timeout=60)
        run('systemctl','start','slurmd');wait_base(sock);run('systemctl','start','pro6000d-training');broker_stopped=False;deadline=time.monotonic()+60
        while True:
            try:
                if control(sock,'ACTUAL_USAGE_UNLIMITED2 0')=='OK ACTUAL_USAGE_UNLIMITED2 SUPPORTED' and platform_ready(): break
            except (OSError,UnicodeError,ValueError,KeyError,subprocess.SubprocessError): pass
            if time.monotonic()>=deadline: raise RuntimeError('final controller readiness failed')
            time.sleep(.5)
        if 'State=UP' not in run('/usr/local/bin/scontrol','show','partition','gpu','-o'): raise RuntimeError('partition not UP')
        fenced=False
        for unit in ('slurmctld','slurmd','pro6000d-training'): run('systemctl','is-active','--quiet',unit)
        record['changed_components']=['slurmctld','slurmd','monthly quota broker','Cockpit UI','shared CLI/MCP','gpu partition','long QoS'];record['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat();record['rollback']='not needed; previous release and evidence retained';mark('deployed')
        print(json.dumps(record,ensure_ascii=False))
    except BaseException as exc:
        record['error']=str(exc)
        if not started:
            # Before the new controller writes anything the old release is recoverable.
            rollback_errors=[]
            if files_changed:
                for path in paths:
                    saved=backup/'files'/str(path).lstrip('/')
                    if saved.exists(): atomic(path,saved.read_bytes(),stat.S_IMODE(saved.stat().st_mode))
                if Path('/opt/slurm/current').resolve()!=OLD:
                    link=Path('/opt/slurm/.actual-rollback-'+stamp);os.symlink(str(OLD),link);os.replace(link,'/opt/slurm/current')
            if qos_change_attempted:
                try: run('/usr/local/bin/sacctmgr','-i','modify','qos','where','name=long','set','MaxWall=3-00:00:00')
                except Exception as error: rollback_errors.append('qos: '+str(error))
            if scheduler_stop_attempted:
                try: run('systemctl','start','slurmd')
                except Exception as error: rollback_errors.append('scheduler: '+str(error))
            if fence_attempted:
                try: run('/usr/local/bin/scontrol','update','PartitionName=gpu','State=UP')
                except Exception as error: rollback_errors.append('partition: '+str(error))
            if broker_stop_attempted:
                try: run('systemctl','start','pro6000d-training')
                except Exception as error: rollback_errors.append('broker: '+str(error))
            record['rollback']='before candidate startup: '+('errors '+str(rollback_errors) if rollback_errors else 'previous services requested')
        else:
            record['rollback']='not attempted after candidate startup; retain WAL and keep admission fenced'
            try:
                if S.exists():
                    atomic(S,conf_state(original,'INACTIVE').encode(),stat.S_IMODE(S.stat().st_mode))
                    manifest['files']['slurm_conf']['sha256']=sha(S);atomic(M,(json.dumps(manifest,indent=2)+'\n').encode(),0o600)
                if run('systemctl','is-active','slurmctld')=='active': run('/usr/local/bin/scontrol','update','PartitionName=gpu','State=INACTIVE')
            except Exception: pass
        mark('failed');print(json.dumps(record,ensure_ascii=False));raise
if __name__=='__main__': main()
