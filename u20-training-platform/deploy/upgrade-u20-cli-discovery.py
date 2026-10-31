#!/usr/bin/env python3
"""Install the narrow 0858424 capability-report fix for u20 only."""
import hashlib
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT=Path('/home/u20/.local/share/time')
OLD=ROOT/'candidate-0858424'
NEW=ROOT/'candidate-monthly-discovery-1'
BIN=Path('/home/u20/.local/bin')
OLD_MANIFEST='ba1e2b6a33262dcbe46823489a473858fa127f9ef0520fb4631fee2eafa1719a'
OLD_CORE='90a0c11296f30c8c379362198b272777d5d1a710fbef166280831cfeaca6d534'
NEW_CORE='972d895cf3a9d7b177a2c4f4668c2e92973b19c42dc61f5d92a6c8fb8d82d83c'
WRAPPERS={'timectl':'be481649b4e67a0e78efc3783de10c04040b1b688899fe0deb67b873dd4bc59f',
          'time-mcp':'b0a5b1298576545cc9e1e9dc836b0c07af8075b234b370e8f8be75ded523bcf7'}

def digest(data): return hashlib.sha256(data).hexdigest()
def check(path, expected=None):
    st=path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or st.st_nlink!=1 or st.st_mode & 0o022:
        raise ValueError('untrusted input: '+str(path))
    data=path.read_bytes()
    if expected and digest(data)!=expected: raise ValueError('unexpected input: '+str(path))
    return data
def atomic(path,data,mode=0o755):
    fd,name=tempfile.mkstemp(prefix='.time-discovery-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(data);out.flush();os.fchmod(out.fileno(),mode);os.fsync(out.fileno())
        os.replace(name,path)
        dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:
        if os.path.exists(name):os.unlink(name)

def main(fixed):
    if pwd.getpwuid(os.geteuid()).pw_name!='u20' or os.uname().nodename!='u20-Z790-S-DDR4':
        raise ValueError('run as u20 on u20')
    for directory in (ROOT, OLD, BIN):
        st=directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.geteuid() or st.st_mode & 0o002:
            raise ValueError('untrusted directory: '+str(directory))
        if st.st_mode & 0o020:
            members=set(grp.getgrgid(st.st_gid).gr_mem)
            members.update(p.pw_name for p in pwd.getpwall() if p.pw_gid==st.st_gid)
            if st.st_gid!=os.getegid() or members!={'u20'}:
                raise ValueError('writable directory group is shared: '+str(directory))
    if NEW.exists() or NEW.is_symlink():raise ValueError('target exists; inspect prior attempt')
    manifest=json.loads(check(OLD/'artifact-manifest.json',OLD_MANIFEST))
    entries=manifest.get('files')
    if not isinstance(entries,list) or len(entries)!=manifest.get('file_count'):
        raise ValueError('invalid manifest')
    fixed_data=check(fixed,NEW_CORE)
    saved={name:check(BIN/name,sha) for name,sha in WRAPPERS.items()}
    validated=[]
    for entry in entries:
        rel=Path(entry['path'])
        if rel.is_absolute() or '..' in rel.parts or len(rel.parts)>2:
            raise ValueError('unsafe manifest path')
        source=OLD/rel
        if len(rel.parts)==2:
            parent=source.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or parent.st_mode & 0o022:
                raise ValueError('untrusted source directory')
        validated.append((rel,check(source,entry['sha256']),int(entry['mode'],8)))
    if sum(rel==Path('time_core.py') for rel,_,_ in validated)!=1:
        raise ValueError('core manifest entry missing')
    backup=Path(tempfile.mkdtemp(prefix='backup-before-monthly-discovery-',dir=str(ROOT)))
    for name,data in saved.items():
        p=backup/name
        with p.open('xb') as out:
            out.write(data);out.flush();os.fchmod(out.fileno(),0o600);os.fsync(out.fileno())
    dfd=os.open(backup,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(dfd)
    finally:os.close(dfd)
    NEW.mkdir(mode=0o700)
    for rel,data,mode in validated:
        target=NEW/rel;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        target.write_bytes(fixed_data if rel==Path('time_core.py') else data);target.chmod(mode)
        if digest(target.read_bytes()) != (NEW_CORE if rel==Path('time_core.py') else digest(data)):
            raise ValueError('copied candidate failed verification')
    for entry in entries:
        if entry['path']=='time_core.py':entry['sha256']=NEW_CORE
    (NEW/'artifact-manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    (NEW/'artifact-manifest.json').chmod(0o600)
    result=subprocess.run(['/usr/bin/python3',str(NEW/'timectl.py'),'capabilities'],capture_output=True,text=True,timeout=15,check=True)
    payload=json.loads(result.stdout); monthly=payload['data']['monthly_quota']
    if not payload.get('ok') or monthly.get('server_state')!='enforcing' or monthly.get('enforcement_ready') is not True:
        raise ValueError('new CLI did not observe active monthly controller')
    desired={'timectl':('#!/bin/sh\nexec /usr/bin/python3 '+str(NEW)+'/timectl.py "$@"\n').encode(),
             'time-mcp':('#!/bin/sh\nunset TIME_MCP_CONSENT_FD\nexec /usr/bin/python3 '+str(NEW)+'/time_mcp.py "$@"\n').encode()}
    changed=[]
    try:
        for name in WRAPPERS:
            if check(BIN/name)!=saved[name]:raise ValueError('wrapper changed during upgrade')
            changed.append(name);atomic(BIN/name,desired[name])
        result=subprocess.run([str(BIN/'timectl'),'capabilities'],capture_output=True,text=True,timeout=15,check=True)
        if json.loads(result.stdout)['data']['monthly_quota']['enforcement_ready'] is not True:
            raise ValueError('installed CLI capability check failed')
    except BaseException:
        for name in reversed(changed):
            current=check(BIN/name)
            if current==desired[name]:atomic(BIN/name,saved[name])
            elif current!=saved[name]:raise RuntimeError('wrapper changed; manual recovery required')
        raise
    print('CLI_DISCOVERY_UPDATED backup='+str(backup)+' target='+str(NEW))

if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('usage: upgrade-u20-cli-discovery.py FIXED_CORE')
    try:main(Path(sys.argv[1]).absolute())
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        print('CLI upgrade refused: '+str(exc),file=sys.stderr);sys.exit(1)
