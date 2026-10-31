#!/usr/bin/env python3
"""Offline history migration primitive with durable progress evidence.

Caller holds the maintenance lock and must keep backend/controller stopped.
On interruption, retain the fence, snapshot and journal for reconciliation;
database and jobcomp are distinct files and cannot be committed atomically.
"""
import datetime
import json
import os
from pathlib import Path
import sqlite3
import stat


def journal_append(path, entry, create=False):
    flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    if create: flags |= os.O_EXCL
    fd=os.open(path, flags, 0o600)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_nlink!=1 or st.st_mode & 0o077:
            raise ValueError('untrusted cutover journal')
        data=(json.dumps(entry,sort_keys=True,separators=(',',':'))+'\n').encode()
        while data:
            n=os.write(fd,data)
            if n<=0: raise OSError('short journal write')
            data=data[n:]
        os.fsync(fd)
    finally: os.close(fd)
    directory=os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(directory)
    finally: os.close(directory)


def apply(backup, snapshot, history_guard, purge_core, log_guard):
    backup=Path(backup); journal=backup/'history-migration.jsonl'
    for item in reversed((backup,) + tuple(backup.parents)):
        st=item.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or st.st_mode & 0o022:
            raise ValueError('untrusted snapshot directory')
    if backup.stat().st_mode & 0o077: raise ValueError('snapshot must be private')
    # Do not retry a partially completed migration: it may have committed a
    # database or log update before an interruption was recorded.
    if journal.exists() or journal.is_symlink():
        raise ValueError('history migration journal exists; reconcile before retry')
    snapshot.requirements()
    def record(phase, create=False, **data):
        journal_append(journal, {'phase':phase, 'time_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(), **data}, create=create)
    record('database_start', create=True)
    deleted=history_guard.purge_stopped(backup,purge_core,snapshot)
    with sqlite3.connect('file:'+str(snapshot.DB)+'?mode=ro',uri=True,timeout=5) as db:
        post_digest=history_guard.logical_digest(db)
    record('database_complete', deleted_jobs=deleted, post_purge_logical_sha256=post_digest)
    record('jobcomp_start')
    cleared=log_guard.clear_stopped(backup,snapshot)
    record('jobcomp_complete', cleared_records=cleared)
    snapshot.requirements()
    return {'deleted_jobs':deleted, 'cleared_jobcomp_records':cleared,
            'post_purge_logical_sha256':post_digest, 'journal':str(journal)}
