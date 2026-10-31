#!/usr/bin/env python3
"""Expose the already-running u20 monthly controller to the platform UI.

No scheduler restart or ledger mutation. This requires a verified idle
controller and a previously recorded live GPU accounting probe.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

INPUTS = {
    'fence.py':'07e540bc17548df86f4a72050c131814c637c78bcffad9e40cd8f28939b17932',
    'base.py':'7611b73148879a4ef831d38a4ceb5384c8cf11239900d0ef751acdde92597978',
    'bridge.py':'30ac4668f6ea6f0a413f4a4a834b27d42c5db069900600058d18da58e2b04a70',
    'view.py':'10be510b5bee9afaec955293dbff499140e099ff612eed1186e7e96e299a637d',
}
CONFIG=Path('/etc/pro6000d-training/config.json')
CONTROLLER='/opt/time/slurm19-7a06cfff70a7/sbin/slurmctld'


def checked(path,digest):
    for item in reversed((path,)+tuple(path.parents)):
        st=item.lstat(); directory=item!=path
        if (stat.S_ISLNK(st.st_mode) or st.st_uid!=0 or st.st_gid!=0 or st.st_mode & 0o022 or
                (not stat.S_ISDIR(st.st_mode) if directory else not stat.S_ISREG(st.st_mode))):
            raise ValueError('untrusted input: '+str(item))
    if path.lstat().st_nlink!=1: raise ValueError('unexpected input links')
    value=path.read_bytes()
    if hashlib.sha256(value).hexdigest()!=digest: raise ValueError('input digest mismatch')
    return value


def load(name,path,data):
    spec=importlib.util.spec_from_loader(name,loader=None)
    module=importlib.util.module_from_spec(spec); module.__file__=str(path)
    exec(compile(data,str(path),'exec'),module.__dict__); return module


def main(directory):
    if os.geteuid()!=0 or os.uname().nodename!='u20-Z790-S-DDR4': raise ValueError('root on u20 required')
    os.umask(0o077)
    sources={name:checked(directory/name,sha) for name,sha in INPUTS.items()}
    fence=load('fence',directory/'fence.py',sources['fence.py'])
    base=load('base',directory/'base.py',sources['base.py'])
    bridge=load('bridge',directory/'bridge.py',sources['bridge.py'])
    view=load('view',directory/'view.py',sources['view.py'])
    for name in ('bridge.py','view.py'):
        live=Path('/usr/local/lib/pro6000d-training')/('controller_'+name if name=='view.py' else 'controller_bridge.py')
        checked(live,INPUTS[name])
    checked(Path('/usr/local/lib/pro6000d-training/pro6000d-training-api.py'),
            '24afd69cff5eac856f2ccaaf8a97b95c1823c911c83f40338745cd4ab813e821')
    lockfd=os.open('/run/lock/time-u20-console-upgrade.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(lockfd,'a') as lock:
        st=os.fstat(lock.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_nlink!=1 or st.st_mode & 0o077:
            raise ValueError('untrusted maintenance lock')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        base.trusted_directory(CONFIG.parent); base.trusted_directory(base.BACKUPS)
        base.trusted_directory(fence.MARKER.parent)
        if fence.MARKER.parent.stat().st_mode & 0o077: raise ValueError('marker parent must be private')
        base.trusted_metadata(CONFIG)
        old=CONFIG.read_bytes(); settings=json.loads(old)
        if settings.get('monthly_controller',{}) not in ({},None):
            raise ValueError('monthly UI configuration already exists; inspect it')
        if fence.MARKER.exists() or fence.MARKER.is_symlink(): raise ValueError('maintenance marker already present')
        pid=int(fence.run(['/usr/bin/systemctl','show','slurmctld.service','-p','MainPID','--value']))
        if pid<=0 or os.readlink('/proc/%d/exe'%pid)!=CONTROLLER: raise ValueError('unexpected controller')
        if 'SlurmctldParameters     = MonthlyGpuQuota' not in fence.run(['/usr/bin/scontrol','show','config']):
            raise ValueError('monthly controller parameter is not active')
        if not base.no_scheduler_jobs() or not base.no_active_platform_jobs(): raise ValueError('active jobs')
        base.health(True)
        uid=pwd.getpwnam('timzhang').pw_uid
        b=bridge.ControllerBridge('/var/lib/slurm/ctld/time-quota.sock',pwd.getpwnam('slurm').pw_uid)
        before=b.snapshot(uid); monthly=view.snapshot_view(before,True)
        if (before['wal_sequence']<3 or before['used']['gpu_seconds']+before['used']['nanoseconds']/1e9<=0 or
                before['active_allocation_count']!=0 or monthly['current_tier']!='debug' or
                [x['limit_gpu_seconds'] for x in monthly['tiers']]!=[18000,360000,1800000]):
            raise ValueError('live probe/accounting state not as expected')
        settings['monthly_controller']={'read_enabled':True,'grant_enabled':True,'activation_verified':True,
                                        'controller_user':'slurm','socket_path':'/var/lib/slurm/ctld/time-quota.sock'}
        new=(json.dumps(settings,ensure_ascii=False,indent=2)+'\n').encode()
        folder=Path(tempfile.mkdtemp(prefix='monthly-ui-',dir=str(base.BACKUPS)))
        backup=folder/'config.before'; backup.write_bytes(old); backup.chmod(0o600)
        with backup.open('rb') as stream: os.fsync(stream.fileno())
        dfd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: os.fsync(dfd)
        finally: os.close(dfd)
        parent=os.open(str(fence.MARKER.parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        content=(json.dumps({'kind':'monthly-ui-activation','nonce':uuid.uuid4().hex})+'\n').encode()
        marker_id=None; stopped=False; changed=False; exposed=False
        try:
            marker_id=fence.create_marker(parent,content)
            stopped=True; fence.run(['/usr/bin/systemctl','stop',fence.SERVICE])
            if not base.no_scheduler_jobs() or not base.no_active_platform_jobs(): raise ValueError('jobs appeared')
            if CONFIG.read_bytes()!=old: raise ValueError('config changed during maintenance')
            changed=True; base.atomic(CONFIG,new,0o640)
            # Once the marker is removed, do not overwrite potentially new
            # approvals or job state by reverting the configuration blindly.
            exposed=True; fence.remove_marker(parent,marker_id,content); marker_id=None
            fence.run(['/usr/bin/systemctl','start',fence.SERVICE])
            deadline=time.monotonic()+20; last=None
            while time.monotonic()<deadline:
                try:
                    for username in ('u20','timzhang'):
                        user=pwd.getpwnam(username); data=base.api(user.pw_uid,'/api/v1/state')
                        cap=base.api(user.pw_uid,'/api/v1/capabilities')
                        m=data['monthly_quota']
                        if (data['identity']['uid']!=user.pw_uid or not data.get('scheduler_available') or
                                not m.get('enforcement_ready') or m.get('mode')!='enforcing' or
                                not cap['monthly_requests']['available']):
                            raise ValueError('monthly UI/API not ready for '+username)
                    break
                except (OSError,KeyError,ValueError,subprocess.SubprocessError) as exc:
                    last=exc; time.sleep(.5)
            else: raise RuntimeError('monthly UI health check failed: '+str(last))
            print('MONTHLY_UI_ENABLED config_backup='+str(backup),flush=True)
        except BaseException:
            try:
                if exposed:
                    if marker_id is None: marker_id=fence.create_marker(parent,content)
                    fence.run(['/usr/bin/systemctl','stop',fence.SERVICE])
                    print('UI activation needs review; backend fenced, controller and data retained',file=sys.stderr,flush=True)
                    raise
                if changed:
                    current=CONFIG.read_bytes()
                    if current==new: base.atomic(CONFIG,old,0o640)
                    elif current!=old: raise ValueError('config changed; manual recovery required')
                if marker_id is not None:
                    fence.remove_marker(parent,marker_id,content); marker_id=None
                if stopped:
                    fence.run(['/usr/bin/systemctl','start',fence.SERVICE]); base.health(True)
                print('RECOVERY_OK old UI configuration restored',file=sys.stderr,flush=True)
            except BaseException as recovery:
                if marker_id is None and stopped:
                    try: marker_id=fence.create_marker(parent,content)
                    except BaseException as exc: print('marker restore failed: '+str(exc),file=sys.stderr)
                print('RECOVERY FAILED; inspect backend and marker: '+str(recovery),file=sys.stderr,flush=True)
                raise
            raise
        finally: os.close(parent)


if __name__=='__main__':
    if len(sys.argv)!=2: raise SystemExit('usage: enable-u20-monthly-ui.py ROOT_OWNED_INPUT_DIR')
    def interrupted(_sig,_frame): raise RuntimeError('monthly UI activation interrupted')
    signal.signal(signal.SIGTERM,interrupted); signal.signal(signal.SIGHUP,interrupted)
    try: main(Path(sys.argv[1]).absolute())
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print('Monthly UI activation failed: '+str(exc),file=sys.stderr); sys.exit(1)
