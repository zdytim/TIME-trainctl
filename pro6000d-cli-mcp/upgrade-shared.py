#!/usr/bin/env python3
"""Upgrade shared clients; defaults to read-only. Not crash-atomic across wrappers."""
import argparse
import hashlib
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

FILES = ('timectl.py', 'time_core.py', 'time_mcp.py', 'time_consent.py', 'time_mcp_confirm.py')
OLD_NAME = '20261111-cli-mcp-v1'
NEW_NAME = '20261117-unlimited-v2'
LIB = Path('/usr/local/lib/pro6000d-client')
BIN = Path('/usr/local/bin')

def trusted(path, file=False):
    path = Path(path)
    for item in (path, *path.parents):
        st = item.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError('untrusted path: ' + str(item))
        if item == path and file:
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise ValueError('untrusted file: ' + str(item))
        elif not stat.S_ISDIR(st.st_mode):
            raise ValueError('not a directory: ' + str(item))

def read_checked(path, mode, check):
    check(path, True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != mode:
            raise ValueError('invalid file mode or type: ' + str(path))
        with os.fdopen(fd, 'rb') as f:
            fd = -1
            return f.read()
    finally:
        if fd >= 0: os.close(fd)

def manifest_payload(source, check):
    source = Path(source)
    check(source)
    check(source/'manifest.json', True)
    # Source files are not executed. Their hashes are meaningful only when the
    # root-owned source directory itself has been authenticated by deployment.
    fd = os.open(source/'manifest.json', os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o022: raise ValueError('unsafe manifest')
        manifest = json.load(f)
    if not isinstance(manifest, dict) or set(manifest) != set(FILES): raise ValueError('manifest mismatch')
    payload = {}
    for name in FILES:
        check(source/name, True)
        fd = os.open(source/name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o022: raise ValueError('unsafe source')
            data = f.read()
        if hashlib.sha256(data).hexdigest() != manifest[name]: raise ValueError('source hash mismatch: '+name)
        payload[name] = data
    return payload

def wrappers(lib, name):
    def w(script):
        return ('#!/bin/sh\nunset PYTHONPATH PYTHONHOME TIME_MCP_CONSENT_FD\nexec /usr/bin/python3 -B -E -s '+str(lib/name/script)+' "$@"\n').encode()
    return {'timectl': w('timectl.py'), 'time-mcp': w('time_mcp.py'), 'time-mcp-confirm': w('time_mcp_confirm.py')}

def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)

def write_atomic(directory, name, data, mode):
    fd, tmp = tempfile.mkstemp(prefix='.client-upgrade-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fchmod(f.fileno(), mode); os.fsync(f.fileno())
        os.replace(tmp, directory/name)
        sync_dir(directory)
    finally:
        if os.path.lexists(tmp): os.unlink(tmp)

def verify_dir(path, payload, check, mode):
    check(path)
    if stat.S_IMODE(path.lstat().st_mode) != mode: raise ValueError('directory mode differs: '+str(path))
    if set(os.listdir(path)) != set(payload): raise ValueError('directory contents differ: '+str(path))
    for name, data in payload.items():
        file_mode = 0o600 if mode == 0o700 else 0o644
        if read_checked(path/name, file_mode, check) != data: raise ValueError('file differs: '+str(path/name))

def _upgrade(source, old_source, lib=LIB, bin_dir=BIN, execute=False, check=trusted, hook=lambda *_: None):
    source, old_source, lib, bin_dir = map(Path, (source, old_source, lib, bin_dir))
    payload, old_payload = manifest_payload(source, check), manifest_payload(old_source, check)
    check(lib); check(bin_dir)
    verify_dir(lib/OLD_NAME, old_payload, check, 0o755)
    version, backup = lib/NEW_NAME, lib/(NEW_NAME+'-backups')
    old, new = wrappers(lib, OLD_NAME), wrappers(lib, NEW_NAME)
    states = []
    for name in old:
        data = read_checked(bin_dir/name, 0o755, check)
        if data == old[name]: states.append('old')
        elif data == new[name]: states.append('new')
        else: raise ValueError('unknown wrapper: '+name)
    if len(set(states)) != 1: raise ValueError('mixed wrappers; manual recovery required')
    if os.path.lexists(version): verify_dir(version, payload, check, 0o755)
    if os.path.lexists(backup): verify_dir(backup, old, check, 0o700)
    if states[0] == 'new' and (not os.path.lexists(version) or not os.path.lexists(backup)):
        raise ValueError('installed version or backup missing')
    if not execute or states[0] == 'new': return
    if not os.path.lexists(version):
        stage = Path(tempfile.mkdtemp(prefix='.client-version-', dir=lib))
        os.chmod(stage, 0o755)
        for name, data in payload.items(): write_atomic(stage, name, data, 0o644)
        verify_dir(stage, payload, check, 0o755)
        sync_dir(stage)
        os.rename(stage, version); sync_dir(lib)
    verify_dir(version, payload, check, 0o755)
    if not os.path.lexists(backup):
        stage = Path(tempfile.mkdtemp(prefix='.client-backup-', dir=lib))
        os.chmod(stage, 0o700)
        for name, data in old.items(): write_atomic(stage, name, data, 0o600)
        verify_dir(stage, old, check, 0o700)
        sync_dir(stage)
        os.rename(stage, backup); sync_dir(lib)
    verify_dir(backup, old, check, 0o700)
    try:
        for name in old:
            if read_checked(bin_dir/name, 0o755, check) != old[name]: raise RuntimeError('wrapper changed: '+name)
            hook('before', name)
            if read_checked(bin_dir/name, 0o755, check) != old[name]: raise RuntimeError('wrapper changed: '+name)
            write_atomic(bin_dir, name, new[name], 0o755)
            hook('after', name)
    except BaseException as error:
        conflicts = []
        for name in old:
            try:
                current = read_checked(bin_dir/name, 0o755, check)
                if current == new[name]: write_atomic(bin_dir, name, old[name], 0o755)
                elif current != old[name]: conflicts.append(name)
            except BaseException: conflicts.append(name)
        if conflicts: raise RuntimeError('rollback incomplete; manual recovery required: '+', '.join(conflicts)) from error
        raise

def upgrade(source, old_source, lib=LIB, bin_dir=BIN, execute=False, check=trusted, hook=lambda *_: None):
    if not execute:
        return _upgrade(source, old_source, lib, bin_dir, False, check, hook)
    lib = Path(lib)
    check(lib)
    lockpath = lib / '.upgrade.lock'
    fd = os.open(lockpath, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        check(lockpath, True)
        st = os.fstat(fd)
        current = os.lstat(lockpath)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
                stat.S_IMODE(st.st_mode) != 0o600 or
                (st.st_dev, st.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('unsafe upgrade lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _upgrade(source, old_source, lib, bin_dir, True, check, hook)
    finally:
        os.close(fd)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='reviewed new source and manifest')
    parser.add_argument('old_source', type=Path, help='reviewed installed v1 source and manifest')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if args.execute and (os.geteuid() != 0 or os.uname().nodename != 'ubuntu'): parser.error('--execute requires root on ubuntu')
    upgrade(args.source.absolute(), args.old_source.absolute(), execute=args.execute)
    print('Upgrade complete' if args.execute else 'Dry-run checks passed; no files changed')

if __name__ == '__main__': main()
