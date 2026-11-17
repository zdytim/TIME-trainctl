#!/usr/bin/env python3
"""Upgrade an existing monthly release during an idle maintenance window.

Without --execute, inspect prerequisites only. Execution fences the partition,
stops idle services, replaces the release and configuration, then reopens admission.
After admission can resume, automatic rollback is disabled to preserve live ledgers.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import subprocess
import tempfile
import time

CONFIG = Path('/etc/pro6000d-training/config.json')
STATE = Path('/var/lib/slurm/slurmctld')
PROOF = Path('/var/lib/pro6000d-monthly')
MANIFEST = Path('/etc/pro6000d-training/monthly-release.json')
CURRENT = Path('/opt/slurm/current')
PREFIX = Path('/opt/slurm/26.05.5-timequota-priority-20261116')
ROOT = Path(__file__).resolve().parents[1]
ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin','LANG':'C','SLURM_CONF':'/etc/slurm/slurm.conf'}


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20, env=ENV).stdout.strip()


def require_gpu_idle():
    # The SSH session's device cgroup is intentionally restricted even for
    # sudo. A short-lived system service reads NVML in the node service's
    # device context without requesting a GPU allocation.
    processes=run('/usr/bin/systemd-run','--quiet','--wait','--pipe','--collect',
                  '-p','CPUWeight=1','/usr/bin/nvidia-smi',
                  '--query-compute-apps=pid','--format=csv,noheader,nounits')
    if processes:
        raise RuntimeError('GPU compute processes exist; allow them to finish naturally')


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def trusted_file(path, uid):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077 or st.st_nlink != 1:
        raise RuntimeError('untrusted existing monthly state: ' + str(path))


def load_trusted_module(name, path):
    for parent in (path.parent, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('untrusted release directory: ' + str(parent))
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        raise RuntimeError('untrusted release helper: ' + str(path))
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def preflight(candidate, tree_hash, commit, remote_ref, proof, official):
    if os.geteuid() != 0 or os.uname().nodename != 'ubuntu':
        raise RuntimeError('root on pro6000d is required')
    if not re.fullmatch(r'[a-f0-9]{64}',tree_hash): raise RuntimeError('candidate tree hash is invalid')
    if not re.fullmatch(r'[a-f0-9]{40}',commit) or remote_ref != commit:
        raise RuntimeError('exact committed and pushed branch head is required')
    verifier=load_trusted_module('release_proof',ROOT/'deploy/git_release_proof.py')
    verifier.verify_proof(ROOT,proof,commit)
    tree=load_trusted_module('release_tree',ROOT/'deploy/release_tree.py')
    source_verifier=load_trusted_module('priority_source_verifier',ROOT/'tools/verify-priority-source.py')
    reconstructed_hash=source_verifier.verify(official,candidate/'source.tar.gz')
    # The evidence is a Git-proven release file created after reviewing the
    # exact source assembly and target build. Caller-supplied hashes alone are
    # not a build provenance record.
    evidence_path=ROOT.parent/'research/pro6000d/strict-priority-build-evidence.json'
    evidence=json.loads(evidence_path.read_text())
    if (evidence.get('version') != 1 or evidence.get('architecture') != 'x86_64' or
        evidence.get('official_source_sha256') != '4680cafc82b0689865c885eb703441e7ebf764896c40fd03a50f2fcb60664f68' or
        evidence.get('installed_tree_sha256') != tree_hash or
        evidence.get('prefix') != str(PREFIX) or
        evidence.get('source_archive_sha256') != reconstructed_hash or
        evidence.get('submit_source_sha256') != sha(candidate/'job_submit_pro6000d.c') or
        evidence.get('submit_source_sha256') != sha(ROOT/'deploy/job_submit_pro6000d.c') or
        evidence.get('build_script_sha256') != sha(ROOT/'validation/build-slurm26-target-staged.sh') or
        evidence.get('integration_patch_sha256') != sha(ROOT.parent/'research/pro6000d/slurm26-strict-priority-integration.patch')):
        raise RuntimeError('committed build evidence does not match candidate/source')
    if not CURRENT.is_symlink() or CURRENT.resolve() != Path('/opt/slurm/26.05.5-timequota-20261111'):
        raise RuntimeError('unexpected current Slurm version')
    if PREFIX.exists(): raise RuntimeError('candidate installation already exists')
    for service in ('slurmctld','slurmd','pro6000d-training'):
        run('systemctl','is-active','--quiet',service)
    if run('/usr/local/bin/squeue','--all','-h','-o','%i %T'):
        raise RuntimeError('scheduler has jobs; allow all jobs to finish naturally')
    require_gpu_idle()
    if [x.rstrip('*') for x in run('/usr/local/bin/sinfo','--all','--noheader','--format','%P').splitlines()] != ['gpu']:
        raise RuntimeError('unexpected scheduler partitions')
    part=run('/usr/local/bin/scontrol','show','partition','gpu','-o')
    if not re.search(r'(?:^|\s)State=UP(?:\s|$)',part):
        raise RuntimeError('original gpu partition is not UP')
    original=Path('/etc/slurm/slurm.conf').read_bytes()
    if partition_configuration(original,'UP') != original:
        raise RuntimeError('original configured gpu partition is not UP')
    cfg=json.loads(CONFIG.read_text()); monthly=cfg.get('monthly_controller',{})
    if monthly.get('activation_enabled') is not True or monthly.get('read_enabled') is not True:
        raise RuntimeError('existing monthly quota is not active')
    import pwd
    slurm=pwd.getpwnam('slurm')
    sys.path.insert(0,str(ROOT/'backend'))
    from activation_attestation import verify_release
    from controller_bridge import ControllerBridge
    if not verify_release(str(MANIFEST),slurm.pw_uid):
        raise RuntimeError('existing monthly release attestation failed')
    if not ControllerBridge(monthly['socket_path'],slurm.pw_uid).readiness():
        raise RuntimeError('existing monthly controller is not ready')
    for path,uid in ((STATE/'monthly-quota.wal',slurm.pw_uid),(STATE/'monthly-quota.checkpoint',slurm.pw_uid),
                     (PROOF/'witness',0),(PROOF/'node-proof.wal',0),(MANIFEST,0)):
        trusted_file(path,uid)
    dbpath=Path(cfg['state_db'])
    with sqlite3.connect('file:'+str(dbpath)+'?mode=ro',uri=True,timeout=10) as db:
        if db.execute("SELECT 1 FROM jobs WHERE state IN ('WAITING_QUOTA','CANCELLING_RESERVATION','SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED') LIMIT 1").fetchone():
            raise RuntimeError('platform has active jobs')
        if db.execute("SELECT 1 FROM monthly_dispatch_holds WHERE state NOT IN ('CLOSED','CANCELLED') LIMIT 1").fetchone():
            raise RuntimeError('monthly reservations are not reconciled')
    if (candidate/'build.exit').read_text().strip()!='0': raise RuntimeError('candidate build is not complete')
    staged=candidate/'stage'/str(PREFIX).lstrip('/')
    if not staged.is_dir(): raise RuntimeError('staged candidate is missing')
    if hashlib.sha256(tree.release_tree(staged)).hexdigest() != tree_hash:
        raise RuntimeError('staged candidate tree hash does not match reviewed build')
    for name,digest in evidence.get('artifacts',{}).items():
        if name not in ('sbin/slurmctld','sbin/slurmd','lib/slurm/job_submit_pro6000d.so',
                        'lib/slurm/select_cons_tres.so','lib/slurm/sched_backfill.so','lib/slurm/cgroup_v2.so'):
            raise RuntimeError('unexpected build evidence artifact')
        if not re.fullmatch(r'[a-f0-9]{64}',digest) or sha(staged/name)!=digest:
            raise RuntimeError('candidate artifact differs from build evidence: '+name)
    if set(evidence.get('artifacts',{})) != {'sbin/slurmctld','sbin/slurmd','lib/slurm/job_submit_pro6000d.so',
        'lib/slurm/select_cons_tres.so','lib/slurm/sched_backfill.so','lib/slurm/cgroup_v2.so'}:
        raise RuntimeError('incomplete build evidence artifact list')
    for path in ('sbin/slurmctld','sbin/slurmd','lib/slurm/job_submit_pro6000d.so',
                 'lib/slurm/select_cons_tres.so','lib/slurm/sched_backfill.so','lib/slurm/cgroup_v2.so'):
        artifact=staged/path
        if artifact.is_symlink() or not artifact.is_file(): raise RuntimeError('missing candidate artifact '+path)
        with artifact.open('rb') as f: header=f.read(20)
        if len(header)<20 or header[:6]!=b'\x7fELF\x02\x01' or header[18:20]!=b'\x3e\x00':
            raise RuntimeError('candidate artifact is not x86_64 ELF: '+path)
    return {'current':str(CURRENT.resolve()),'staged':str(staged),'controller_sha256':sha(staged/'sbin/slurmctld'),
            'ledger_bytes':(STATE/'monthly-quota.wal').stat().st_size,
            'snapshot_only':True,'activation_supported':False}



def atomic_write(target, data, mode):
    """Replace a root-managed regular file and fsync its parent directory."""
    if target.is_symlink(): raise RuntimeError('refusing symlink target: '+str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.priority-upgrade-',dir=target.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name,target)
        directory=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def partition_configuration(data, target):
    """Change only the single explicitly configured GPU partition state."""
    if target not in ('UP','INACTIVE'): raise ValueError('invalid partition state')
    text=data.decode('utf-8')
    if re.search(r'^\s*include(?:\s|=)',text,re.I|re.M):
        raise RuntimeError('included scheduler configuration is unsupported')
    if re.search(r'^\s*ReconfigFlags\s*=',text,re.I|re.M):
        raise RuntimeError('scheduler reconfiguration flags require explicit review')
    lines=text.splitlines(keepends=True)
    indices=[i for i,line in enumerate(lines) if re.match(r'^\s*PartitionName\s*=',line,re.I)]
    if len(indices)!=1 or not re.search(r'\bPartitionName\s*=\s*gpu(?:\s|$)',lines[indices[0]],re.I):
        raise RuntimeError('expected exactly one gpu partition')
    i=indices[0]; active,sep,comment=lines[i].partition('#')
    states=re.findall(r'\bState\s*=\s*(\S+)',active,re.I)
    if len(states)!=1 or states[0].upper() not in ('UP','INACTIVE'):
        raise RuntimeError('unexpected gpu partition configuration')
    active,n=re.subn(r'(\bState\s*=\s*)(?:UP|INACTIVE)(?=\s|$)',lambda m:m.group(1)+target,active,count=1,flags=re.I)
    if n!=1: raise RuntimeError('partition state replacement failed')
    lines[i]=active+(sep+comment if sep else '')
    return ''.join(lines).encode('utf-8')


def snapshot_file(backup, path):
    if path.is_symlink(): raise RuntimeError('refusing symlink backup target: '+str(path))
    if path.exists():
        dst=backup/'files'/str(path).lstrip('/')
        dst.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        shutil.copy2(path,dst)


def create_identity(epoch):
    """Create the identity and initial broker anchor; never replace prior state."""
    if not re.fullmatch(r'[0-9a-f]{32}',epoch): raise ValueError('invalid epoch')
    identity=Path('/etc/pro6000d-training/priority-identity')
    base=Path('/var/lib/pro6000d-monthly-priority')
    if identity.exists() or base.exists(): raise RuntimeError('priority identity already exists; recovery required')
    identity.mkdir(mode=0o755)
    atomic_write(identity/'installation-id',(epoch+'\n').encode(),0o644)
    base.mkdir(mode=0o700)
    anchor=base/'anchor'; locks=base/'locks'
    anchor.mkdir(mode=0o700); locks.mkdir(mode=0o700)
    atomic_write(anchor/'ordered-chain.lock',b'',0o600)
    atomic_write(anchor/'ordered-chain.anchor',(epoch+' 0 '+'0'*64+'\n').encode(),0o600)
    atomic_write(locks/'priority-delivery.lock',b'',0o600)
    return identity,anchor,locks


def perform_cutover(candidate, tree_hash, commit, remote_ref, proof, official):
    """Perform the reviewed upgrade only after verifying an idle target."""
    lock_path=Path('/run/lock/pro6000d-priority-upgrade.lock')
    fd=os.open(lock_path,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o077:
            raise RuntimeError('untrusted upgrade lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return _perform_cutover_locked(candidate,tree_hash,commit,remote_ref,proof,official)
    finally:
        os.close(fd)


def _perform_cutover_locked(candidate, tree_hash, commit, remote_ref, proof, official):
    # This is a quiescent-only upgrade. A job appearing at any point before
    # stopping Slurm aborts the cutover; no running job is signalled.
    plan=prepare_cutover_state(candidate,tree_hash,commit,remote_ref,proof,official)
    cfg=json.loads(CONFIG.read_text()); manifest=json.loads(MANIFEST.read_text())
    slurm_conf=Path('/etc/slurm/slurm.conf'); original=slurm_conf.read_bytes()
    inactive=partition_configuration(original,'INACTIVE'); final=partition_configuration(original,'UP')
    staged=candidate/'stage'/str(PREFIX).lstrip('/')
    tree=load_trusted_module('release_tree_cutover',ROOT/'deploy/release_tree.py')
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup=Path('/var/backups/pro6000d-training')/('priority-upgrade-'+stamp)
    backup.mkdir(mode=0o700)
    record={'commit':commit,'target':os.uname().nodename,'started_utc':stamp,
            'state':'prepared','backup':str(backup),'candidate_tree_sha256':tree_hash,
            'changed_components':[],'rollback':'not attempted'}
    def save(): atomic_write(backup/'release.json',(json.dumps(record,indent=2)+'\n').encode(),0o600)
    def mark(state): record['state']=state; save()
    for path in (slurm_conf,CONFIG,MANIFEST,Path('/etc/systemd/system/slurmctld.service.d/monthly.conf'),
                 Path('/etc/systemd/system/slurmd.service.d/monthly.conf')):
        snapshot_file(backup,path)
    backend=Path('/usr/local/lib/pro6000d-training')
    ui=Path('/usr/share/cockpit/pro6000d-training')
    source=ROOT/'backend'; ui_source=ROOT/'cockpit-plugin/pro6000d-training'
    for file in sorted(source.glob('*.py')):
        if file.is_symlink(): raise RuntimeError('unexpected backend symlink')
        target=Path('/usr/local/bin/pro6000d-training-client') if file.name=='pro6000d-training-client.py' else backend/file.name
        snapshot_file(backup,target)
    for file in sorted(ui_source.rglob('*')):
        if file.is_symlink(): raise RuntimeError('unexpected UI symlink')
        if file.is_file(): snapshot_file(backup,ui/file.relative_to(ui_source))
    with sqlite3.connect(cfg['state_db']) as live, sqlite3.connect(str(backup/'state.sqlite3')) as copy:
        live.backup(copy,pages=256,sleep=.1)
    os.chmod(backup/'state.sqlite3',0o600)
    save()
    stopped_backend=False; partition_down=False; services_stopped=False; admission_possible=False
    try:
        mark('stopping platform dispatch')
        stopped_backend=True
        run('systemctl','stop','pro6000d-training')
        if run('/usr/local/bin/squeue','--all','-h','-o','%i %T'):
            raise RuntimeError('scheduler jobs appeared before fencing')
        mark('fencing GPU partition')
        partition_down=True
        run('/usr/local/bin/scontrol','update','PartitionName=gpu','State=INACTIVE')
        part=run('/usr/local/bin/scontrol','show','partition','gpu','-o')
        if not re.search(r'(?:^|\s)State=INACTIVE(?:\s|$)',part): raise RuntimeError('partition fence failed')
        if run('/usr/local/bin/squeue','--all','-h','-o','%i %T'):
            raise RuntimeError('scheduler jobs appeared while fencing')
        require_gpu_idle()
        # Recheck the broker database after stopping it, before stopping Slurm.
        with sqlite3.connect('file:'+cfg['state_db']+'?mode=ro',uri=True,timeout=10) as db:
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('WAITING_QUOTA','CANCELLING_RESERVATION','SUBMITTING','PENDING','RUNNING','COMPLETING','SUSPENDED') LIMIT 1").fetchone():
                raise RuntimeError('platform jobs appeared while fencing')
            if db.execute("SELECT 1 FROM monthly_dispatch_holds WHERE state NOT IN ('CLOSED','CANCELLED') LIMIT 1").fetchone():
                raise RuntimeError('open holds appeared while fencing')
        mark('stopping quiescent slurm services')
        services_stopped=True
        run('systemctl','stop','slurmd','slurmctld')
        # Copies are evidence only; never restore them over a live ledger.
        for file in (STATE/'monthly-quota.wal',STATE/'monthly-quota.checkpoint',PROOF/'witness',PROOF/'node-proof.wal'):
            snapshot_file(backup,file)
        mark('installing candidate and identity')
        temp=PREFIX.with_name('.'+PREFIX.name+'-'+stamp)
        if temp.exists() or PREFIX.exists(): raise RuntimeError('candidate prefix already exists')
        shutil.copytree(staged,temp,symlinks=True)
        for root,dirs,files in os.walk(temp,followlinks=False):
            os.chown(root,0,0)
            for name in dirs+files: os.chown(Path(root)/name,0,0,follow_symlinks=False)
        if hashlib.sha256(tree.release_tree(temp)).hexdigest()!=tree_hash:
            raise RuntimeError('installed tree differs from reviewed candidate')
        os.rename(temp,PREFIX)
        identity,anchor,locks=create_identity(secrets.token_hex(16))
        identity_dropin=Path('/etc/systemd/system/slurmctld.service.d/priority-identity.conf')
        if identity_dropin.exists(): raise RuntimeError('identity drop-in already exists')
        atomic_write(identity_dropin,('[Service]\nEnvironment=TIME_ORDERED_IDENTITY_DIR='+str(identity)+'\n').encode(),0o644)
        atomic_write(slurm_conf,inactive,stat.S_IMODE(slurm_conf.stat().st_mode))
        tmp_link=CURRENT.with_name('.priority-current-'+stamp)
        os.symlink(str(PREFIX),tmp_link); os.replace(tmp_link,CURRENT)
        files={'controller':PREFIX/'sbin/slurmctld','node':PREFIX/'sbin/slurmd',
               'submit':PREFIX/'lib/slurm/job_submit_pro6000d.so',
               'scheduler':PREFIX/'lib/slurm/sched_backfill.so','select':PREFIX/'lib/slurm/select_cons_tres.so',
               'cgroup':PREFIX/'lib/slurm/cgroup_v2.so','slurm_conf':slurm_conf,
               'gres_conf':Path('/etc/slurm/gres.conf'),'cgroup_conf':Path('/etc/slurm/cgroup.conf')}
        manifest['files']={key:{'path':str(path.resolve(strict=True)),'sha256':sha(path)} for key,path in files.items()}
        manifest['files']['node_proof']={'path':str(PROOF/'node-proof.wal')}
        atomic_write(MANIFEST,(json.dumps(manifest,indent=2)+'\n').encode(),0o600)
        for file in sorted(source.glob('*.py')):
            target=Path('/usr/local/bin/pro6000d-training-client') if file.name=='pro6000d-training-client.py' else backend/file.name
            atomic_write(target,file.read_bytes(),0o755 if target.name=='pro6000d-training-client' else 0o644)
        for file in sorted(ui_source.rglob('*')):
            if file.is_file(): atomic_write(ui/file.relative_to(ui_source),file.read_bytes(),0o644)
        monthly=cfg['monthly_controller']; monthly['priority_anchor_directory']=str(anchor); monthly['priority_lock_directory']=str(locks)
        monthly['experimental_creation_bound_top']=True; cfg['strict_priority_enabled']=True
        atomic_write(CONFIG,(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode(),stat.S_IMODE(CONFIG.stat().st_mode))
        mark('starting candidate behind fence')
        run('systemctl','daemon-reload'); run('systemctl','start','slurmd')
        sys.path.insert(0,str(ROOT/'backend'))
        from controller_bridge import ControllerBridge, ControllerUnavailable
        from activation_attestation import verify_release
        import pwd
        bridge=ControllerBridge(monthly['socket_path'],pwd.getpwnam('slurm').pw_uid)
        deadline=time.monotonic()+60
        while True:
            try:
                if bridge.readiness() and verify_release(str(MANIFEST),pwd.getpwnam('slurm').pw_uid): break
            except ControllerUnavailable: pass
            if time.monotonic()>=deadline: raise RuntimeError('candidate controller or attestation not ready')
            time.sleep(.5)
        if run('/usr/local/bin/squeue','--all','-h','-o','%i %T'): raise RuntimeError('unexpected jobs behind fence')
        run('systemctl','start','pro6000d-training'); stopped_backend=False
        # Read the real administrator's API through its Unix peer identity.
        admin=pwd.getpwnam('ubuntu'); deadline=time.monotonic()+45
        request=json.dumps({'method':'GET','path':'/api/v1/state'})+'\n'
        while True:
            try:
                reply=subprocess.run(['setpriv','--reuid='+str(admin.pw_uid),'--regid='+str(admin.pw_gid),
                    '--init-groups','/usr/local/bin/pro6000d-training-client'],input=request,text=True,
                    capture_output=True,timeout=5,check=True,env=ENV)
                response=json.loads(reply.stdout); data=response.get('data',{})
                priority=data.get('priority_management',{}); quota=data.get('monthly_quota',{})
                if (response.get('ok') is True and priority.get('available') is True and
                    priority.get('reconciled') is True and quota.get('enforcement_ready') is True and
                    quota.get('selectable_tiers_ready') is True): break
            except (OSError,ValueError,subprocess.SubprocessError):
                pass
            if time.monotonic()>=deadline: raise RuntimeError('priority and monthly health not confirmed')
            time.sleep(.5)
        # Attestation requires configuration mtime to precede both daemons.
        # Stop only while the partition is inactive and no job exists.
        if run('/usr/local/bin/squeue','--all','-h','-o','%i %T'):
            raise RuntimeError('unexpected jobs before final start')
        mark('stopping verified fenced candidate for final configuration')
        stopped_backend=True; run('systemctl','stop','pro6000d-training')
        run('systemctl','stop','slurmd','slurmctld')
        # A restart after writing UP can admit work; persist that boundary first.
        mark('admission boundary; automatic rollback disabled')
        admission_possible=True; record['admission_possible']=True
        record['rollback']='not attempted after admission boundary; preserve live ledgers'; save()
        atomic_write(slurm_conf,final,stat.S_IMODE(slurm_conf.stat().st_mode))
        manifest['files']['slurm_conf']['sha256']=sha(slurm_conf)
        atomic_write(MANIFEST,(json.dumps(manifest,indent=2)+'\n').encode(),0o600)
        run('systemctl','start','slurmd')
        deadline=time.monotonic()+60
        while True:
            try:
                if bridge.readiness() and verify_release(str(MANIFEST),pwd.getpwnam('slurm').pw_uid): break
            except ControllerUnavailable: pass
            if time.monotonic()>=deadline: raise RuntimeError('final controller health failed')
            time.sleep(.5)
        part=run('/usr/local/bin/scontrol','show','partition','gpu','-o')
        if not re.search(r'(?:^|\s)State=UP(?:\s|$)',part): raise RuntimeError('partition did not reopen')
        partition_down=False
        run('systemctl','start','pro6000d-training'); stopped_backend=False
        # The restarted broker must independently reattest its new controller boot.
        deadline=time.monotonic()+45
        while True:
            try:
                reply=subprocess.run(['setpriv','--reuid='+str(admin.pw_uid),'--regid='+str(admin.pw_gid),
                    '--init-groups','/usr/local/bin/pro6000d-training-client'],input=request,text=True,
                    capture_output=True,timeout=5,check=True,env=ENV)
                response=json.loads(reply.stdout); data=response.get('data',{})
                priority=data.get('priority_management',{}); quota=data.get('monthly_quota',{})
                if (response.get('ok') is True and priority.get('available') is True and
                    priority.get('reconciled') is True and quota.get('enforcement_ready') is True and
                    quota.get('selectable_tiers_ready') is True): break
            except (OSError,ValueError,subprocess.SubprocessError): pass
            if time.monotonic()>=deadline: raise RuntimeError('final platform health failed')
            time.sleep(.5)
        record.update(state='active',completed_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                      changed_components=['slurmctld','slurmd','scheduler plugins','backend','Cockpit'],
                      verification='existing ledger replayed; priority and monthly API ready; gpu partition UP',
                      rollback='not attempted; preserve live ledgers')
        save()
        return record
    except Exception as exc:
        record['error']=str(exc)
        if not services_stopped:
            if partition_down:
                run('/usr/local/bin/scontrol','update','PartitionName=gpu','State=UP')
            if stopped_backend: run('systemctl','start','pro6000d-training')
            record['rollback']='original services resumed before scheduler stop'
        elif admission_possible:
            record['rollback']='not attempted after admission boundary; inspect scheduler and preserve live ledgers'
        else:
            record['rollback']='not attempted; partition remains fenced and live ledgers retained'
        record['state']='failed'; save(); raise


def prepare_cutover_state(candidate, tree_hash, commit, remote_ref, proof, official):
    """Build a reviewable plan from immutable evidence without changing service state."""
    result=preflight(candidate,tree_hash,commit,remote_ref,proof,official)
    config=json.loads(CONFIG.read_text())
    manifest=json.loads(MANIFEST.read_text())
    slurm_conf=Path('/etc/slurm/slurm.conf')
    original=slurm_conf.read_bytes()
    inactive=partition_configuration(original,'INACTIVE')
    final=partition_configuration(original,'UP')
    if not isinstance(manifest.get('files'),dict) or not isinstance(manifest.get('pidfiles'),dict):
        raise RuntimeError('invalid active release manifest')
    if config.get('strict_priority_enabled'):
        raise RuntimeError('strict priority is already configured')
    result.update({
        'commit':commit,
        'remote_ref_commit':remote_ref,
        'config_inactive_sha256':hashlib.sha256(inactive).hexdigest(),
        'config_up_sha256':hashlib.sha256(final).hexdigest(),
        'will_preserve':['monthly WAL','checkpoint','witness','node proof','platform database'],
        'preconditions':['no scheduler jobs','no platform jobs or open holds','single gpu partition',
                         'verified existing monthly controller','verified committed release and candidate'],
        'cutover_implemented':True,
    })
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--tree-sha256',required=True)
    p.add_argument('--commit',required=True)
    p.add_argument('--remote-ref-commit',required=True)
    p.add_argument('--git-proof',type=Path,required=True)
    p.add_argument('--official-archive',type=Path,required=True)
    p.add_argument('--execute',action='store_true',help='perform the reviewed cutover')
    args=p.parse_args()
    candidate=args.candidate.resolve(strict=True)
    if args.execute:
        perform_cutover(candidate,args.tree_sha256,args.commit,args.remote_ref_commit,args.git_proof,args.official_archive)
        return
    print(json.dumps(prepare_cutover_state(candidate,args.tree_sha256,args.commit,
                                            args.remote_ref_commit,args.git_proof,args.official_archive),indent=2))


if __name__ == '__main__': main()
