#!/usr/bin/env python3
"""Clear backed-up jobcomp only when all entries match platform history.

Caller holds the maintenance lock, fences submissions and verifies no jobs.
Controller/backend must remain stopped; this is not a cross-file transaction.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile


def clear_stopped(backup, snapshot):
    backup = Path(backup)
    for path in reversed((backup,) + tuple(backup.parents)):
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('untrusted snapshot directory')
    if backup.stat().st_mode & 0o077: raise ValueError('snapshot must be private')
    def read_private(path):
        fd=os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            st=os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o077:
                raise ValueError('untrusted snapshot file')
            with os.fdopen(os.dup(fd), 'rb') as source: return source.read()
        finally: os.close(fd)
    manifest=json.loads(read_private(backup/'manifest.json'))
    log_backup=read_private(backup/'jobcomp.log')
    if hashlib.sha256(log_backup).hexdigest()!=manifest['files']['jobcomp.log']['sha256']:
        raise ValueError('jobcomp backup hash mismatch')
    saved_db=backup/'state.sqlite3'
    if hashlib.sha256(read_private(saved_db)).hexdigest()!=manifest['database']['sha256']:
        raise ValueError('database backup hash mismatch')
    with sqlite3.connect('file:'+str(saved_db)+'?mode=ro&immutable=1',uri=True) as db:
        ids={str(row[0]) for row in db.execute('SELECT scheduler_id FROM jobs WHERE scheduler_id IS NOT NULL')}
    lines=log_backup.splitlines()
    for line in lines:
        matches=re.findall(rb'(?:^|\s)JobId=(\d+)(?=\s|$)',line)
        if len(matches)!=1 or matches[0].decode('ascii') not in ids:
            raise ValueError('jobcomp contains unmatched or malformed records')
    snapshot.requirements()
    source=Path(snapshot.JOBCOMP)
    directory=os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary=None
    try:
        fd=os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,dir_fd=directory)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:
                raise ValueError('untrusted current jobcomp')
            with os.fdopen(os.dup(fd),'rb') as stream: current=stream.read()
            if hashlib.sha256(current).digest()!=hashlib.sha256(log_backup).digest():
                raise ValueError('jobcomp changed after snapshot')
            if snapshot.identity(before)!=snapshot.identity(os.fstat(fd)):
                raise ValueError('jobcomp changed while reading')
            outfd,temporary=tempfile.mkstemp(prefix='.time-jobcomp-',dir=str(source.parent))
            try:
                os.fchown(outfd,before.st_uid,before.st_gid)
                os.fchmod(outfd,stat.S_IMODE(before.st_mode)); os.fsync(outfd)
            finally: os.close(outfd)
            snapshot.requirements()
            if snapshot.identity(before)!=snapshot.identity(os.stat(source.name,dir_fd=directory,follow_symlinks=False)):
                raise ValueError('jobcomp changed before replacement')
            os.replace(temporary,source.name,dst_dir_fd=directory); temporary=None
            os.fsync(directory)
        finally: os.close(fd)
    finally:
        if temporary is not None: os.unlink(temporary)
        os.close(directory)
    return len(lines)
