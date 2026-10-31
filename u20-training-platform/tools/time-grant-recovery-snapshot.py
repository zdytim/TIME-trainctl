#!/usr/bin/env python3
"""Create a protected, read-only preview of an interrupted monthly GRANT.

Never writes scheduler state, approves a grant, or restarts a service. The
preview does not establish full WAL semantics or authorize marker repair.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

HERE=Path(__file__).resolve().parent
def load(name, filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    obj=importlib.util.module_from_spec(spec); spec.loader.exec_module(obj); return obj
inspector=load('time_snapshot_inspector','time-quota-recovery-inspect.py')
repair=load('time_snapshot_candidate','time-grant-repair-candidate.py')


def trusted_directory(path, final_owner, permitted_ancestors):
    """Open each directory without following symlinks; return final dir fd."""
    path=Path(os.path.abspath(path))
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        parts=path.parts[1:]
        for i,part in enumerate(parts):
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            os.close(fd); fd=child
            info=os.fstat(fd)
            owners=(final_owner,) if i==len(parts)-1 else permitted_ancestors
            # Root-owned sticky ancestors such as /tmp protect the following
            # trusted-owned entry from removal by other users. Final paths
            # themselves must remain non-writable by group/others.
            sticky_parent=(i<len(parts)-1 and info.st_uid==0 and info.st_mode & stat.S_ISVTX)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners or
                    (info.st_mode & 0o022 and not sticky_parent)):
                raise ValueError('untrusted directory component: '+str(path))
        return fd
    except BaseException:
        os.close(fd); raise


def service_inactive():
    for unit in ('slurmctld.service','pro6000d-training.service'):
        p=subprocess.run(['/usr/bin/systemctl','show',unit,'-p','LoadState','-p','ActiveState'],
                         check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=8)
        fields=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
        if fields.get('LoadState')!='loaded' or fields.get('ActiveState') not in ('inactive','failed'):
            raise ValueError('service not known to be inactive: '+unit)


def save(directory, name, data):
    fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory)
    with os.fdopen(fd,'wb') as out:
        out.write(data); out.flush(); os.fsync(out.fileno())


def collect(state_dir, witness_dir, db_path, backup_dir, service_uid, root_uid, check_services):
    check_services()
    state_dir,witness_dir,db_path,backup_dir=map(lambda p:Path(os.path.abspath(p)),
                                                  (state_dir,witness_dir,db_path,backup_dir))
    dirs=[]; files=[]; dbfd=None; source=None
    try:
        for p,owner,ancestors in ((witness_dir,root_uid,(0,root_uid)),
                                   (state_dir,service_uid,(0,root_uid,service_uid)),
                                   (db_path.parent,root_uid,(0,root_uid)),
                                   (backup_dir,root_uid,(0,root_uid))):
            dirs.append(trusted_directory(p,owner,ancestors))
        if os.fstat(dirs[0]).st_mode & 0o077 or os.fstat(dirs[2]).st_mode & 0o077:
            raise ValueError('witness and database directories must be private')
        for parent,name,owner in ((dirs[0],'witness.log',root_uid),
                                  (dirs[1],'monthly-quota.wal',service_uid),
                                  (dirs[1],'monthly-quota.checkpoint',service_uid)):
            fd,info=inspector.locked_file(parent,name,owner); files.append((fd,info,name))
        witness,wal,checkpoint=[inspector.content(*item) for item in files]
        check_services()
        dbfd=os.open(db_path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=dirs[2])
        dbinfo=os.fstat(dbfd)
        if (not stat.S_ISREG(dbinfo.st_mode) or dbinfo.st_uid!=root_uid or dbinfo.st_nlink!=1 or dbinfo.st_mode & 0o077):
            raise ValueError('untrusted SQLite file')
        # Root-only database parent prevents untrusted replacement of this path.
        source=sqlite3.connect('file:'+quote(str(db_path),safe='/')+'?mode=ro',uri=True,timeout=5)
        before=os.stat(db_path,follow_symlinks=False)
        if (before.st_dev,before.st_ino)!=(dbinfo.st_dev,dbinfo.st_ino): raise ValueError('database path changed')
        out=Path(tempfile.mkdtemp(prefix='time-grant-preview-',dir=str(backup_dir)))
        os.chmod(out,0o700)
        outfd=trusted_directory(out,root_uid,(0,root_uid))
        try:
            for name,data in (('monthly-quota.wal',wal),('monthly-quota.checkpoint',checkpoint),('witness.log',witness)):
                save(outfd,name,data)
            deadline=time.monotonic()+30
            def progress(_status,_remaining,_total):
                if time.monotonic()>deadline: raise TimeoutError('SQLite backup timed out')
            with sqlite3.connect(str(out/'state.sqlite3'),timeout=5) as backup:
                source.backup(backup,pages=256,progress=progress,sleep=.1)
                if backup.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('backup integrity failure')
                backup.row_factory=sqlite3.Row
                fields=wal.splitlines()[-1].split(b'|')
                if len(fields)!=9 or fields[2]!=b'GRANT': raise ValueError('last WAL event is not a complete GRANT')
                grant_id=fields[7].decode('ascii')
                row=backup.execute('SELECT * FROM monthly_grant_outbox WHERE application_id=?',(grant_id,)).fetchone()
                app=backup.execute('SELECT * FROM applications WHERE id=?',(grant_id,)).fetchone()
                if row is None or app is None: raise ValueError('approval evidence is missing')
                plan=repair.candidate(wal,checkpoint,witness,dict(row),dict(app))
            os.chmod(out/'state.sqlite3',0o600)
            snapshotfd=os.open('state.sqlite3',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=outfd)
            try: os.fsync(snapshotfd)
            finally: os.close(snapshotfd)
            after=os.stat(db_path,follow_symlinks=False)
            if (after.st_dev,after.st_ino)!=(dbinfo.st_dev,dbinfo.st_ino): raise ValueError('database path changed')
            check_services()
            for fd,_,_ in files: os.lseek(fd,0,os.SEEK_SET)
            if [inspector.content(*item) for item in files] != [witness,wal,checkpoint]:
                raise ValueError('marker content changed during database snapshot')
            save(outfd,'checkpoint.proposed',plan['checkpoint'])
            save(outfd,'witness.proposed',plan['witness'])
            observed=inspector.inspect(wal,checkpoint,witness)
            result={'backup':str(out),'observation':observed['observation'],'intent_matches':True,
                    'repair_authorized':False,'wal_sha256':hashlib.sha256(wal).hexdigest(),
                    'note':'preview only: full WAL semantics and scheduler state remain unverified'}
            save(outfd,'preview.json',(json.dumps(result,indent=2)+'\n').encode()); os.fsync(outfd)
            os.fsync(dirs[3])
            return result
        finally: os.close(outfd)
    finally:
        if source is not None: source.close()
        if dbfd is not None: os.close(dbfd)
        for fd,_,_ in reversed(files): os.close(fd)
        for fd in reversed(dirs): os.close(fd)


if __name__=='__main__':
    if len(sys.argv)!=1 or os.geteuid()!=0:
        raise SystemExit('run as root without arguments; preview only')
    try:
        print(json.dumps(collect('/var/lib/slurm/ctld','/var/lib/time-quota-anchor',
            '/var/lib/pro6000d-training/state.sqlite3','/var/backups/pro6000d-training',
            pwd.getpwnam('slurm').pw_uid,0,service_inactive),indent=2))
    except (OSError,ValueError,KeyError,sqlite3.Error,subprocess.SubprocessError) as exc:
        print('Preview refused; original files unchanged: '+str(exc),file=sys.stderr); sys.exit(1)
