#!/usr/bin/env python3
"""Install a root-owned shared CLI and read-only MCP without replacing files."""
import hashlib
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile

LIB=Path('/usr/local/lib/time-cli')
VERSION=LIB/'0858424-monthly-discovery-1'
BIN=Path('/usr/local/bin')
FILES={'timectl.py':'6ecc5869bc0d81f5419e5b94ad8758ab5c7ab6c89b7168086082cd8d9415f7af',
       'time_mcp.py':'6b3cfc9fc75e6b74450715b58356dc522c1f0e6614b30affb41df6be16d30857',
       'time_consent.py':'9b323212e0a53cc4ee7f03029197f7d7f9c747db4ac766ce361039f10fa27515',
       'time_core.py':'972d895cf3a9d7b177a2c4f4668c2e92973b19c42dc61f5d92a6c8fb8d82d83c'}
WRAPPERS={'timectl':('#!/bin/sh\nunset PYTHONPATH PYTHONHOME\nexport PYTHONDONTWRITEBYTECODE=1\nexec /usr/bin/python3 '+str(VERSION)+'/timectl.py "$@"\n').encode(),
          'time-mcp':('#!/bin/sh\nunset PYTHONPATH PYTHONHOME TIME_MCP_CONSENT_FD\nexport PYTHONDONTWRITEBYTECODE=1\nexec /usr/bin/python3 '+str(VERSION)+'/time_mcp.py "$@"\n').encode()}


def trusted(path, is_file=False):
    for item in reversed((path,)+tuple(path.parents)):
        st=item.lstat();file=item==path and is_file
        if (stat.S_ISLNK(st.st_mode) or st.st_uid!=0 or st.st_gid!=0 or st.st_mode & 0o022 or
                (not stat.S_ISREG(st.st_mode) if file else not stat.S_ISDIR(st.st_mode))):
            raise ValueError('untrusted path: '+str(item))
    if is_file and path.lstat().st_nlink!=1:raise ValueError('unexpected link count')


def main(directory):
    if os.geteuid()!=0 or os.uname().nodename!='u20-Z790-S-DDR4':raise ValueError('root on u20 required')
    trusted(directory);trusted(BIN);trusted(LIB.parent)
    for target in (VERSION,LIB):
        if target.exists() or target.is_symlink():raise ValueError('target already exists: '+str(target))
    for name in WRAPPERS:
        if (BIN/name).exists() or (BIN/name).is_symlink():raise ValueError('wrapper already exists: '+name)
    payload={}
    for name,expected in FILES.items():
        path=directory/name;trusted(path,True);data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=expected:raise ValueError('payload hash mismatch: '+name)
        payload[name]=data
    # A private staging directory is published only after all bytes are synced.
    LIB.mkdir(mode=0o755);stage=Path(tempfile.mkdtemp(prefix='.staging-',dir=str(LIB)))
    installed=[]
    try:
        for name,data in payload.items():
            path=stage/name
            with path.open('xb') as out:
                out.write(data);out.flush();os.fchmod(out.fileno(),0o644);os.fsync(out.fileno())
        os.chmod(stage,0o755)
        dfd=os.open(stage,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(dfd)
        finally:os.close(dfd)
        os.rename(stage,VERSION)
        for name,data in WRAPPERS.items():
            fd=os.open(BIN/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o755)
            st=os.fstat(fd);installed.append((name,st.st_dev,st.st_ino))
            with os.fdopen(fd,'wb') as out:
                out.write(data);out.flush();os.fchmod(out.fileno(),0o755);os.fsync(out.fileno())
        dfd=os.open(BIN,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(dfd)
        finally:os.close(dfd)
        for name in ('u20','timzhang'):
            user=pwd.getpwnam(name);env=os.environ.copy()
            env['HOME']=user.pw_dir;env.pop('XDG_STATE_HOME',None)
            p=subprocess.run(['/usr/bin/setpriv','--reuid',str(user.pw_uid),'--regid',str(user.pw_gid),
                              '--init-groups',str(BIN/'timectl'),'quota'],env=env,text=True,
                             capture_output=True,timeout=20,check=True)
            import json
            result=json.loads(p.stdout)
            if not result.get('ok') or result['data']['monthly_quota'].get('enforcement_ready') is not True:
                raise ValueError('monthly quota unavailable for '+name)
        print('SHARED_CLI_INSTALLED version='+str(VERSION),flush=True)
    except BaseException:
        for name,dev,ino in reversed(installed):
            path=BIN/name
            st=path.lstat()
            if (not stat.S_ISREG(st.st_mode) or (st.st_dev,st.st_ino)!=(dev,ino) or
                    not WRAPPERS[name].startswith(path.read_bytes())):
                print('Wrapper changed; manual recovery required: '+str(path),file=sys.stderr);raise
            path.unlink()
        print('Wrappers removed; retain staged directory for review',file=sys.stderr,flush=True)
        raise


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('usage: install-u20-shared-cli.py ROOT_OWNED_INPUT_DIR')
    try:main(Path(sys.argv[1]).absolute())
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        print('Shared CLI installation refused: '+str(exc),file=sys.stderr);sys.exit(1)
