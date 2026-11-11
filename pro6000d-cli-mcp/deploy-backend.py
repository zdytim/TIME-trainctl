#!/usr/bin/env python3
"""Publish the additive CLI capability API without restarting Slurm."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time

TARGET=Path('/usr/local/lib/pro6000d-training/pro6000d-training-api.py')
CONFIG=Path('/etc/pro6000d-training/config.json')
SERVICE='pro6000d-training'
ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin','LANG':'C'}


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def command(*args,**kwargs): return subprocess.run(args,check=True,text=True,capture_output=True,env=ENV,timeout=30,**kwargs)
def trusted_file(path):
    for item in (path,*path.parents):
        st=item.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid!=0 or st.st_mode&0o022: raise ValueError('untrusted path: '+str(item))
        if item==path:
            if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1: raise ValueError('not a trusted file: '+str(item))
        elif not stat.S_ISDIR(st.st_mode): raise ValueError('not a trusted directory: '+str(item))
def replace(target,data,mode):
    fd,tmp=tempfile.mkstemp(prefix='.cli-api-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data); stream.flush(); os.fchmod(stream.fileno(),mode); os.fsync(stream.fileno())
        os.replace(tmp,target)
        fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY); os.fsync(fd); os.close(fd)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
def read_capabilities():
    p=command('setpriv','--reuid=1000','--regid=1000','--init-groups','/usr/local/bin/pro6000d-training-client',
              input=json.dumps({'method':'GET','path':'/api/v1/capabilities'})+'\n')
    response=json.loads(p.stdout); cap=response.get('data',{})
    if response.get('ok') is not True or cap.get('schema_version')!='1.0': raise ValueError('capability response invalid')
    if cap.get('monthly_quota',{}).get('enforcement_ready') is not True or cap.get('monthly_requests',{}).get('available') is not True:
        raise ValueError('monthly enforcement or requests not ready')
    return cap


def main():
    p=argparse.ArgumentParser(); p.add_argument('--source',required=True,type=Path); p.add_argument('--commit',required=True)
    p.add_argument('--old-sha256',required=True); p.add_argument('--new-sha256',required=True); args=p.parse_args()
    if os.geteuid()!=0 or os.uname().nodename!='ubuntu': raise SystemExit('root on pro6000d required')
    lock=open('/run/lock/pro6000d-cli-deploy.lock','a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    source=args.source.absolute(); trusted_file(source); trusted_file(TARGET)
    if sha(source)!=args.new_sha256 or sha(TARGET)!=args.old_sha256: raise SystemExit('backend checksum mismatch')
    for unit in (SERVICE,'slurmctld','slurmd'): command('systemctl','is-active','--quiet',unit)
    config=json.loads(CONFIG.read_text()); dbpath=Path(config.get('state_db','/var/lib/pro6000d-training/state.sqlite3'))
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup=Path('/var/backups/pro6000d-training')/('cli-api-'+stamp); backup.mkdir(mode=0o700)
    shutil.copy2(TARGET,backup/'pro6000d-training-api.py')
    with sqlite3.connect(str(dbpath)) as live, sqlite3.connect(str(backup/'state.sqlite3')) as saved: live.backup(saved)
    os.chmod(backup/'state.sqlite3',0o600)
    record={'commit':args.commit,'host':os.uname().nodename,'started_utc':stamp,'backup':str(backup),
            'old_sha256':args.old_sha256,'new_sha256':args.new_sha256,'state':'prepared','rollback':'not needed'}
    def save(): replace(backup/'release.json',(json.dumps(record,indent=2)+'\n').encode(),0o600)
    save(); stopped=False; published=False; admission_open=False
    try:
        stopped=True; command('systemctl','stop',SERVICE)
        with sqlite3.connect('file:'+str(dbpath)+'?mode=ro',uri=True) as db:
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('SUBMITTING','CANCELLING_RESERVATION')").fetchone()[0]:
                raise RuntimeError('in-flight submission or cancellation requires reconciliation')
        published=True; replace(TARGET,source.read_bytes(),stat.S_IMODE(TARGET.stat().st_mode))
        record['state']='backend-published'; save()
        # Once the API starts, it may accept new requests; do not auto-rollback
        # code or database after that boundary.
        admission_open=True; record['state']='admission-boundary'; save()
        command('systemctl','start',SERVICE); stopped=False
        end=time.monotonic()+20; last=None; ready=False
        while time.monotonic()<end:
            try: read_capabilities(); ready=True; break
            except (subprocess.SubprocessError,ValueError,KeyError,json.JSONDecodeError) as exc: last=exc; time.sleep(.5)
        if not ready: raise RuntimeError('API readiness failed: '+str(last))
        for unit in (SERVICE,'slurmctld','slurmd'): command('systemctl','is-active','--quiet',unit)
        record.update(state='active',completed_utc=dt.datetime.now(dt.timezone.utc).isoformat()); save()
        print('Backend active; backup:',backup,flush=True)
    except BaseException as exc:
        record.update(state='failed',error=str(exc))
        if not admission_open:
            try:
                if published: replace(TARGET,(backup/'pro6000d-training-api.py').read_bytes(),0o644)
                if stopped: command('systemctl','start',SERVICE)
                command('systemctl','is-active','--quiet',SERVICE)
                if sha(TARGET)!=args.old_sha256: raise RuntimeError('restored checksum mismatch')
                record['rollback']='previous API restored and service active; live database retained'
            except BaseException as rollback_exc:
                record['rollback']='FAILED: '+str(rollback_exc)
        else: record['rollback']='not attempted after API admission; inspect jobs and requests first'
        save(); raise


if __name__=='__main__': main()
