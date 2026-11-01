#!/usr/bin/env python3
"""Fenced u20 selectable-tier cutover. Requires the pinned preflight to pass.

Before the V2 cutover request, failures may restore the old controller only if
its ledger and witness are byte-identical to the stopped snapshot. Once the V2
request is sent, any failure leaves the backend fenced and preserves all state;
never overwrite a ledger that might contain new work.
"""
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

INPUT=Path('/opt/time/selectable-input-f417af5')
PREFLIGHT=INPUT/'preflight-selectable.py'
PREFLIGHT_SHA='836d3396672a052b3ca9ceb4b868bac3a507bae268820b588aed169afcadceaa'
RELEASE=Path('/opt/time/slurm19-f417af53729e')
CONTROLLER=RELEASE/'sbin/slurmctld'
OLD_CONTROLLER='/opt/time/slurm19-7a06cfff70a7/sbin/slurmctld'
UNIT=Path('/etc/systemd/system/slurmctld.service.d/time-monthly.conf')
CONFIG=Path('/etc/pro6000d-training/config.json')
DB=Path('/var/lib/pro6000d-training/state.sqlite3')
MARKER=Path('/var/lib/pro6000d-training/time-monthly-maintenance')
STATE=Path('/var/lib/slurm/ctld')
WITNESS=Path('/var/lib/time-quota-anchor/witness.log')
BACKUPS=Path('/var/backups/pro6000d-training')
LOCK=Path('/run/lock/time-u20-console-upgrade.lock')
PLATFORM=INPUT/'platform-faa28a6.tar.gz'
PLATFORM_SHA='185bb0b7f08f867b933f82df62f983bba7a28e5268b91a25a9ee6ddc0075dc4a'
CLIENT='/usr/local/bin/pro6000d-training-client'
ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','SLURM_CONF':'/etc/slurm-llnl/slurm.conf'}
TERMINAL=('COMPLETED','FAILED','CANCELLED','CANCELED','TIMEOUT','NODE_FAIL',
          'OUT_OF_MEMORY','PREEMPTED','BOOT_FAIL','DEADLINE','REJECTED','EXPIRED')
TARGETS={
 'backend/pro6000d-training-api.py':Path('/usr/local/lib/pro6000d-training/pro6000d-training-api.py'),
 'backend/controller_bridge.py':Path('/usr/local/lib/pro6000d-training/controller_bridge.py'),
 'backend/controller_view.py':Path('/usr/local/lib/pro6000d-training/controller_view.py'),
 'scheduler/job_submit-enabled.lua':Path('/etc/slurm-llnl/job_submit.lua'),
 'ui/app.js':Path('/usr/share/cockpit/pro6000d-training/app.js'),
 'ui/index.html':Path('/usr/share/cockpit/pro6000d-training/index.html'),
 'ui/styles.css':Path('/usr/share/cockpit/pro6000d-training/styles.css'),
 'ui/manifest.json':Path('/usr/share/cockpit/pro6000d-training/manifest.json'),
}


def run(argv, **kwargs):
    return subprocess.run(argv,check=True,text=True,capture_output=True,timeout=kwargs.pop('timeout',30),**kwargs).stdout.strip()


def digest(data): return hashlib.sha256(data).hexdigest()


def trusted(path, uid=0, private=False):
    for parent in reversed(path.parents):
        st=parent.lstat()
        allowed_uid = uid if parent == STATE and uid != 0 else 0
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != allowed_uid or st.st_mode & 0o022:
            raise ValueError('untrusted parent: '+str(parent))
    st=path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1 or
            st.st_mode & 0o022 or (private and st.st_mode & 0o077)):
        raise ValueError('untrusted file: '+str(path))
    return path.read_bytes(), st


def atomic(path, data, mode, uid=0, gid=0):
    fd,name=tempfile.mkstemp(prefix='.time-selectable-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(data);out.flush();os.fchmod(out.fileno(),mode);os.fchown(out.fileno(),uid,gid);os.fsync(out.fileno())
        os.replace(name,path)
        directory=os.open(str(path.parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def no_jobs():
    if run(['/usr/bin/squeue','-h','-o','%i'],env=ENV): return False
    with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=5) as db:
        marks=','.join('?' for _ in TERMINAL)
        active=db.execute('SELECT COUNT(*) FROM jobs WHERE state IS NULL OR state NOT IN ('+marks+')',TERMINAL).fetchone()[0]
        unresolved=db.execute("SELECT COUNT(*) FROM monthly_grant_outbox WHERE state IS NULL OR state!='APPLIED'").fetchone()[0]
        return not active and not unresolved


def ensure_marker(contents):
    try:
        fd=os.open(str(MARKER),os.O_RDONLY|os.O_NOFOLLOW)
    except FileNotFoundError:
        fd=os.open(str(MARKER),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:
            f.write(contents);f.flush();os.fchmod(f.fileno(),0o600);os.fsync(f.fileno())
        dfd=os.open(str(MARKER.parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: os.fsync(dfd)
        finally: os.close(dfd)
        return
    with os.fdopen(fd,'rb') as f:
        st=os.fstat(f.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_nlink!=1 or
            st.st_mode&0o077 or f.read(len(contents)+1)!=contents):
            raise ValueError('maintenance marker changed or is untrusted')


def stop_backend_after_failure():
    try:
        run(['/usr/bin/systemctl','stop','pro6000d-training'])
        return True
    except BaseException as exc:
        print('BACKEND_STOP_FAILED: '+str(exc),file=sys.stderr,flush=True)
        return False


def pid_for(service):
    return int(run(['/usr/bin/systemctl','show',service,'-p','MainPID','--value']))


def require_controller(expected):
    pid=pid_for('slurmctld')
    if pid <= 0 or os.readlink('/proc/%d/exe'%pid)!=str(expected):
        raise ValueError('unexpected controller process')
    return pid


def rpc(message, slurm_uid):
    path=STATE/'time-quota.sock'
    directory=STATE.lstat(); endpoint=path.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid!=slurm_uid or directory.st_mode & 0o022 or
        not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid!=slurm_uid or endpoint.st_mode & 0o077):
        raise ValueError('untrusted controller socket')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(3);sock.connect(str(path))
        peer_pid,peer_uid,_=struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if peer_uid!=slurm_uid or peer_pid != pid_for('slurmctld'): raise ValueError('unexpected controller socket peer')
        sock.sendall(message.encode('ascii')+b'\n');data=bytearray()
        while len(data)<=1024 and b'\n' not in data:
            part=sock.recv(512)
            if not part: break
            data.extend(part)
        if len(data)>1024 or not data.endswith(b'\n') or data.count(b'\n')!=1:
            raise ValueError('incomplete controller response')
        return bytes(data[:-1]).decode('ascii')


def await_snapshot(message, uid, expected_prefix):
    last=None; deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        try:
            result=rpc(message,uid)
            if result.startswith(expected_prefix): return result
            last=result
        except (OSError,ValueError,subprocess.SubprocessError) as exc: last=str(exc)
        time.sleep(.25)
    raise ValueError('controller snapshot unavailable: '+str(last))


def platform_files():
    archive,_=trusted(PLATFORM)
    if digest(archive)!=PLATFORM_SHA: raise ValueError('platform archive changed')
    found={}
    with tarfile.open(PLATFORM,'r:gz') as tar:
        for member in tar:
            if member.name in TARGETS:
                if not member.isfile() or member.name in found or member.size>16*1024*1024:
                    raise ValueError('invalid platform member')
                with tar.extractfile(member) as stream: found[member.name]=stream.read(member.size+1)
                if len(found[member.name])!=member.size: raise ValueError('truncated platform member')
    if set(found)!=set(TARGETS): raise ValueError('missing platform member')
    return found


def check_vendor():
    manifest,_=trusted(INPUT/'platform-faa28a6.manifest.json')
    if digest(manifest)!='986b1f8d94691625ae4f3b6b84134af140c0bfc581f9c2ff51ef5bada91510e5':
        raise ValueError('platform manifest changed')
    for item in json.loads(manifest)['files']:
        name=item['path']
        if not name.startswith('ui/vendor/'): continue
        data,_=trusted(Path('/usr/share/cockpit/pro6000d-training')/name[3:])
        if len(data)!=item['size'] or digest(data)!=item['sha256']:
            raise ValueError('live UI vendor differs: '+name)


def api(uid, path):
    user=pwd.getpwuid(uid)
    output=run(['/usr/bin/setpriv','--reuid='+str(uid),'--regid='+str(user.pw_gid),
                '--init-groups',CLIENT],input=json.dumps({'method':'GET','path':path})+'\n',timeout=10)
    payload=json.loads(output)
    if payload.get('ok') is not True: raise ValueError('platform API unavailable')
    return payload['data']


def main():
    if os.geteuid()!=0 or os.uname().nodename!='u20-Z790-S-DDR4': raise ValueError('root on u20 required')
    script,_=trusted(PREFLIGHT)
    if digest(script)!=PREFLIGHT_SHA: raise ValueError('preflight script changed')
    spec=importlib.util.spec_from_file_location('time_selectable_preflight',PREFLIGHT)
    preflight=importlib.util.module_from_spec(spec);spec.loader.exec_module(preflight)
    lockfd=os.open(str(LOCK),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        st=os.fstat(lockfd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_nlink!=1 or st.st_mode&0o077:
            raise ValueError('untrusted maintenance lock')
        fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        preflight.main();check_vendor();contents=platform_files()
        if not no_jobs(): raise ValueError('jobs appeared before fence')
        if MARKER.exists() or MARKER.is_symlink(): raise ValueError('maintenance marker already exists')
        marker=(json.dumps({'kind':'selectable-cutover','nonce':uuid.uuid4().hex})+'\n').encode()
        mfd=os.open(str(MARKER),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        marker_id=None; stopped_backend=False; stopped_old=False; new_started=False; attempted=False; exposed=False
        backup=None; saved={}; ledger={}; original_unit=None
        try:
            with os.fdopen(mfd,'wb') as f:
                info=os.fstat(f.fileno());marker_id=(info.st_dev,info.st_ino)
                f.write(marker);f.flush();os.fchmod(f.fileno(),0o600);os.fsync(f.fileno())
            pfd=os.open(str(MARKER.parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try: os.fsync(pfd)
            finally: os.close(pfd)
            stopped_backend=True;run(['/usr/bin/systemctl','stop','pro6000d-training'])
            # A start request must be skipped by the loaded ConditionPathExists
            # while the marker exists; verify the actual loaded unit behavior.
            run(['/usr/bin/systemctl','start','pro6000d-training'])
            unit=run(['/usr/bin/systemctl','show','pro6000d-training','-p','ActiveState','-p','ConditionResult'])
            unit_fields=dict(line.split('=',1) for line in unit.splitlines() if '=' in line)
            if unit_fields.get('ActiveState')!='inactive' or unit_fields.get('ConditionResult')!='no':
                stop_backend_after_failure()
                raise ValueError('maintenance condition did not prevent backend start')
            if not no_jobs(): raise ValueError('jobs or grants appeared after fence')
            require_controller(OLD_CONTROLLER)
            stopped_old=True;run(['/usr/bin/systemctl','stop','slurmctld'])
            if run(['/usr/bin/systemctl','is-active','slurmd'])!='active': raise ValueError('node service inactive')
            if not BACKUPS.is_dir() or BACKUPS.is_symlink() or BACKUPS.stat().st_uid!=0 or BACKUPS.stat().st_mode&0o022:
                raise ValueError('untrusted backup directory')
            backup=Path(tempfile.mkdtemp(prefix='selectable-',dir=str(BACKUPS)))
            os.chmod(backup,0o700)
            slurm_uid=pwd.getpwnam('slurm').pw_uid
            for path in (STATE/'monthly-quota.wal',STATE/'monthly-quota.checkpoint',WITNESS):
                data,info=trusted(path,0 if path==WITNESS else slurm_uid,True)
                ledger[str(path)]=(data,stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid)
                (backup/('ledger-'+path.name)).write_bytes(data);os.chmod(backup/('ledger-'+path.name),0o600)
            for path in [UNIT,CONFIG]+list(TARGETS.values()):
                data,info=trusted(path);saved[str(path)]=(data,stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid)
                (backup/('file-'+digest(str(path).encode()))).write_bytes(data)
                os.chmod(backup/('file-'+digest(str(path).encode())),0o600)
            with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=5) as src, sqlite3.connect(str(backup/'state.sqlite3')) as dst:
                src.backup(dst,pages=256,sleep=.1)
                if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('database backup failed integrity')
            os.chmod(backup/'state.sqlite3',0o600)
            backup_index={
                'files':{name:{'backup':'file-'+digest(name.encode()),'sha256':digest(data),
                               'mode':mode,'uid':owner,'gid':group}
                         for name,(data,mode,owner,group) in saved.items()},
                'ledger':{name:{'backup':'ledger-'+Path(name).name,'sha256':digest(data),
                                'mode':mode,'uid':owner,'gid':group}
                          for name,(data,mode,owner,group) in ledger.items()},
                'database_sha256':digest((backup/'state.sqlite3').read_bytes()),
            }
            (backup/'manifest.json').write_text(json.dumps(backup_index,sort_keys=True)+'\n')
            os.chmod(backup/'manifest.json',0o600)
            for item in backup.iterdir():
                with item.open('rb') as f: os.fsync(f.fileno())
            bfd=os.open(str(backup),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try: os.fsync(bfd)
            finally: os.close(bfd)
            def phase(value):
                payload={'phase':value,'time':dt.datetime.now().astimezone().isoformat()}
                atomic(backup/'phase.json',(json.dumps(payload,sort_keys=True)+'\n').encode(),0o600)
            phase('backed_up')
            original_unit=saved[str(UNIT)][0]
            old=OLD_CONTROLLER.encode();new=str(CONTROLLER).encode()
            if original_unit.count(old)!=1: raise ValueError('controller unit does not match expected baseline')
            atomic(UNIT,original_unit.replace(old,new,1),0o644)
            run(['/usr/bin/systemctl','daemon-reload'])
            new_started=True;run(['/usr/bin/systemctl','start','slurmctld'])
            require_controller(CONTROLLER)
            if not no_jobs(): raise ValueError('jobs appeared before selectable cutover')
            uid=pwd.getpwnam('timzhang').pw_uid
            before=await_snapshot('SNAP '+str(uid),slurm_uid,'OK ').split()
            if len(before)!=18 or int(before[13])!=0 or int(before[14])!=0:
                raise ValueError('old snapshot has active allocations')
            phase('cutover_request_pending')
            attempted=True
            if rpc('CUTOVER2_RESET_USAGE 0',slurm_uid)!='OK': raise ValueError('selectable cutover refused')
            after=await_snapshot('SNAP2 '+str(uid),slurm_uid,'OK 2 ').split()
            if len(after)!=18 or any(int(after[i]) for i in (5,6,7,8,9,10,11,12,16,17)):
                raise ValueError('selectable initial balances unexpected')
            phase('cutover_applied')
            for name,path in TARGETS.items():
                previous=saved[str(path)];atomic(path,contents[name],previous[1],previous[2],previous[3])
            settings=json.loads(saved[str(CONFIG)][0]);settings['monthly_controller']['selectable_activation_verified']=True
            conf=(json.dumps(settings,ensure_ascii=False,indent=2)+'\n').encode()
            previous=saved[str(CONFIG)];atomic(CONFIG,conf,previous[1],previous[2],previous[3])
            # This is the point after which user work might enter. Never restore
            # old data or the old controller automatically past this boundary.
            info=MARKER.lstat()
            if (info.st_dev,info.st_ino)!=marker_id or MARKER.read_bytes()!=marker: raise ValueError('marker changed')
            if not no_jobs(): raise ValueError('job or grant appeared before exposure')
            phase('exposure_pending')
            exposed=True;MARKER.unlink()
            dfd=os.open(str(MARKER.parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try: os.fsync(dfd)
            finally: os.close(dfd)
            run(['/usr/bin/systemctl','start','pro6000d-training'])
            deadline=time.monotonic()+25;last=None
            while time.monotonic()<deadline:
                try:
                    state=api(uid,'/api/v1/state');m=state['monthly_quota']
                    if (state['identity']['uid']==uid and state.get('scheduler_available') is True and
                        m.get('selectable_tiers_ready') is True and m.get('enforcement_ready') is True and
                        [tier.get('name') for tier in m.get('tiers',[])]==['debug','important','normal']):
                        break
                    last='selectable capability not ready'
                except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:last=str(exc)
                time.sleep(.25)
            else: raise ValueError('platform health check failed: '+str(last))
            report={'status':'activated','target':os.uname().nodename,'time':dt.datetime.now().astimezone().isoformat(),
                    'controller_commit':'f417af53729e22bdddb8a54043df80fe07428d32',
                    'platform_commit':'faa28a622fef97cc6d5b4cebd795eb16013bfc44',
                    'backup':str(backup),'controller_pid':require_controller(CONTROLLER),
                    'v2_sequence':int(after[15]),'rollback':'not required','verification':'V1 and V2 controller snapshots and platform API capability'}
            atomic(backup/'deployment.json',(json.dumps(report,sort_keys=True)+'\n').encode(),0o600)
            phase('activated')
            print(json.dumps(report,sort_keys=True),flush=True)
        except BaseException:
            if attempted or exposed:
                # Even if recreating a durable marker fails, stopping the
                # backend must still be attempted independently.
                marker_ok=False
                try:
                    ensure_marker(marker);marker_ok=True
                except BaseException as exc:
                    print('FENCE_RESTORE_FAILED: '+str(exc),file=sys.stderr,flush=True)
                stopped=stop_backend_after_failure()
                print('CUTOVER_NEEDS_REVIEW marker_ok=%s backend_stopped=%s; new controller and ledger preserved; backup=%s' %
                      (marker_ok,stopped,backup),file=sys.stderr,flush=True)
            else:
                try:
                    if new_started: run(['/usr/bin/systemctl','stop','slurmctld'])
                    if ledger:
                        for name,(old,_,_,_) in ledger.items():
                            if Path(name).read_bytes()!=old: raise ValueError('ledger changed; refuse automatic rollback')
                    if original_unit is not None:
                        old,mode,uid,gid=saved[str(UNIT)];atomic(UNIT,old,mode,uid,gid)
                        run(['/usr/bin/systemctl','daemon-reload'])
                    if stopped_old:
                        run(['/usr/bin/systemctl','start','slurmctld']);require_controller(OLD_CONTROLLER)
                    if marker_id is not None:
                        info=MARKER.lstat()
                        if (info.st_dev,info.st_ino)!=marker_id or MARKER.read_bytes()!=marker: raise ValueError('marker changed')
                        MARKER.unlink()
                        dfd=os.open(str(MARKER.parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                        try: os.fsync(dfd)
                        finally: os.close(dfd)
                    if stopped_backend: run(['/usr/bin/systemctl','start','pro6000d-training'])
                    print('ROLLBACK_OK old controller restored; backup='+str(backup),file=sys.stderr,flush=True)
                except BaseException as exc:
                    marker_ok=False
                    try:
                        ensure_marker(marker);marker_ok=True
                    except BaseException as fence_error:
                        print('FENCE_RESTORE_FAILED: '+str(fence_error),file=sys.stderr,flush=True)
                    stopped=stop_backend_after_failure() if stopped_backend else False
                    print('ROLLBACK_FAILED marker_ok=%s backend_stopped=%s: %s' %
                          (marker_ok,stopped,exc),file=sys.stderr,flush=True)
            raise
    finally: os.close(lockfd)


if __name__=='__main__':
    def interrupted(_sig,_frame): raise RuntimeError('cutover interrupted')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGHUP,interrupted)
    try: main()
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,sqlite3.Error,subprocess.SubprocessError,tarfile.TarError) as exc:
        print('Selectable cutover failed: '+str(exc),file=sys.stderr);sys.exit(1)
