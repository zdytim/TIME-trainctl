#!/usr/bin/env python3
"""Install reviewed shared clients without modifying the scheduler or user accounts."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

LIB = Path('/usr/local/lib/pro6000d-client')
VERSION = LIB/'20261111-cli-mcp-v1'
BIN = Path('/usr/local/bin')
FILES = ('timectl.py','time_core.py','time_mcp.py','time_consent.py','time_mcp_confirm.py')


def trusted(path, file=False):
    path=Path(path)
    for item in (path, *path.parents):
        st=item.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError('untrusted path: '+str(item))
        if item == path and file:
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise ValueError('untrusted file: '+str(item))
        elif not stat.S_ISDIR(st.st_mode):
            raise ValueError('not a directory: '+str(item))


def wrapper(script, read_only=False):
    clear='unset PYTHONPATH PYTHONHOME TIME_MCP_CONSENT_FD\n'
    return ('#!/bin/sh\n'+clear+'exec /usr/bin/python3 -B -E -s '+str(VERSION/script)+' "$@"\n').encode()


WRAPPERS={'timectl':wrapper('timectl.py'), 'time-mcp':wrapper('time_mcp.py',True),
          'time-mcp-confirm':wrapper('time_mcp_confirm.py')}


def sync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def remove_owned_stage(directory, payload):
    if not directory.exists(): return
    trusted(directory)
    for path in directory.iterdir():
        trusted(path,True)
        if path.name not in payload or not payload[path.name].startswith(path.read_bytes()):
            raise RuntimeError('staging directory changed during rollback: '+str(path))
    for path in directory.iterdir(): path.unlink()
    directory.rmdir()


def main(source):
    if os.geteuid()!=0 or os.uname().nodename!='ubuntu': raise ValueError('root on pro6000d required')
    source=Path(source).absolute(); trusted(source); trusted(source/'manifest.json',True)
    manifest=json.loads((source/'manifest.json').read_text())
    if set(manifest)!=set(FILES): raise ValueError('unexpected manifest entries')
    payload={}
    for name in FILES:
        path=source/name; trusted(path,True); data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=manifest[name]: raise ValueError('hash mismatch: '+name)
        payload[name]=data
    trusted(LIB.parent); trusted(BIN)
    if LIB.exists() or LIB.is_symlink(): raise ValueError('client library already exists; review before upgrading')
    for name in WRAPPERS:
        if (BIN/name).exists() or (BIN/name).is_symlink(): raise ValueError('command already exists: '+name)
    stage=None; installed=[]; created=False
    try:
        LIB.mkdir(mode=0o755); created=True; sync_dir(LIB.parent)
        stage=Path(tempfile.mkdtemp(prefix='.stage-',dir=LIB))
        for name,data in payload.items():
            with (stage/name).open('xb') as f:
                f.write(data); f.flush(); os.fchmod(f.fileno(),0o644); os.fsync(f.fileno())
        stage.chmod(0o755)
        sync_dir(stage)
        os.rename(stage,VERSION)
        sync_dir(LIB)
        for name,data in WRAPPERS.items():
            fd,tmp=tempfile.mkstemp(prefix='.pro6000d-command-',dir=BIN)
            try:
                with os.fdopen(fd,'wb') as f:
                    f.write(data); f.flush(); os.fchmod(f.fileno(),0o755); os.fsync(f.fileno())
                    st=os.fstat(f.fileno())
                os.link(tmp,BIN/name,follow_symlinks=False)
                installed.append((name,st.st_dev,st.st_ino))
            finally:
                if os.path.exists(tmp): os.unlink(tmp)
        sync_dir(BIN)
        print('Installed shared CLI and MCP:',VERSION,flush=True)
    except BaseException:
        for name,dev,ino in reversed(installed):
            path=BIN/name; st=path.lstat()
            if (st.st_dev,st.st_ino)!=(dev,ino) or path.read_bytes()!=WRAPPERS[name]:
                raise RuntimeError('command changed during rollback: '+str(path))
            path.unlink()
        sync_dir(BIN)
        if created:
            if stage is not None: remove_owned_stage(stage,payload)
            remove_owned_stage(VERSION,payload)
            sync_dir(LIB)
            LIB.rmdir(); sync_dir(LIB.parent)
        raise


if __name__=='__main__':
    if len(sys.argv)!=2: raise SystemExit('usage: install-shared.py ROOT_OWNED_SOURCE_DIR')
    main(sys.argv[1])
