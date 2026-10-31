#!/usr/bin/env python3
"""Restore a fenced interrupted history migration only from known states.

Caller holds the exclusive maintenance lock and independently proves there
are no scheduler jobs. Controller and backend must stay stopped. Unknown or
newer data is never overwritten; keep the fence on any ambiguity.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile


def private_bytes(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_nlink!=1 or st.st_mode & 0o077:
            raise ValueError('untrusted snapshot file')
        with os.fdopen(os.dup(fd),'rb') as source: return source.read()
    finally: os.close(fd)


def restore(backup, snapshot, core, guard, migration):
    backup=Path(backup)
    for item in reversed((backup,)+tuple(backup.parents)):
        st=item.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or st.st_mode & 0o022:
            raise ValueError('untrusted snapshot directory')
    if backup.stat().st_mode & 0o077: raise ValueError('snapshot must be private')
    manifest=json.loads(private_bytes(backup/'manifest.json'))
    saved_db=backup/'state.sqlite3'; saved_log=private_bytes(backup/'jobcomp.log')
    if hashlib.sha256(private_bytes(saved_db)).hexdigest()!=manifest['database']['sha256']:
        raise ValueError('snapshot database hash mismatch')
    if hashlib.sha256(saved_log).hexdigest()!=manifest['files']['jobcomp.log']['sha256']:
        raise ValueError('snapshot log hash mismatch')
    raw=private_bytes(backup/'history-migration.jsonl')
    entries=[json.loads(line) for line in raw.splitlines()]
    phases=[entry['phase'] for entry in entries]
    allowed=['database_start','database_complete','jobcomp_start','jobcomp_complete']
    if not phases or phases!=allowed[:len(phases)]:
        raise ValueError('migration journal is incomplete or unexpected')
    snapshot.requirements()
    # Reproduce the exact purge privately, so even an interruption before the
    # database-complete journal entry has only two acceptable database states.
    with sqlite3.connect('file:'+str(saved_db)+'?mode=ro&immutable=1',uri=True) as saved, \
            sqlite3.connect(':memory:') as predicted:
        original_digest=guard.logical_digest(saved)
        saved.backup(predicted); predicted.execute('PRAGMA foreign_keys=ON')
        core.purge(predicted)
        purged_digest=guard.logical_digest(predicted)
    logpath=Path(snapshot.JOBCOMP)
    logfd=os.open(logpath,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        before=os.fstat(logfd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:
            raise ValueError('untrusted current jobcomp')
        with os.fdopen(os.dup(logfd),'rb') as stream: current_log=stream.read()
        if current_log not in (b'',saved_log): raise ValueError('new or unknown jobcomp data')
        if current_log!=saved_log and 'jobcomp_start' not in phases:
            raise ValueError('empty jobcomp is inconsistent with migration journal')
        if snapshot.identity(before)!=snapshot.identity(os.fstat(logfd)):
            raise ValueError('jobcomp changed while reading')
        # ATTACH the validated, immutable copy, then keep the write lock while
        # comparing and reversing just the tables changed by purge_core.
        with sqlite3.connect('file:'+str(snapshot.DB)+'?mode=rw',uri=True,timeout=5) as live:
            live.execute('PRAGMA foreign_keys=ON')
            live.execute('ATTACH DATABASE ? AS saved', ('file:'+str(saved_db)+'?mode=ro&immutable=1',))
            live.execute('BEGIN IMMEDIATE')
            try:
                snapshot.requirements()
                current_digest=guard.logical_digest(live)
                if current_digest not in (original_digest,purged_digest):
                    raise ValueError('new or unknown database data')
                if snapshot.identity(before)!=snapshot.identity(os.lstat(logpath)):
                    raise ValueError('jobcomp changed before restoration')
                if current_digest==purged_digest and purged_digest!=original_digest:
                    live.execute('DROP TRIGGER time_history_request_insert_guard')
                    live.execute('DROP TRIGGER time_history_request_update_guard')
                    live.execute('DELETE FROM job_request_tombstones')
                    live.execute('INSERT INTO jobs SELECT * FROM saved.jobs')
                    live.execute('INSERT INTO job_request_fingerprints SELECT * FROM saved.job_request_fingerprints')
                    if guard.logical_digest(live)!=original_digest or live.execute('PRAGMA foreign_key_check').fetchone():
                        raise ValueError('restored database does not match snapshot')
                live.commit()
            except BaseException:
                live.rollback(); raise
        migration.journal_append(backup/'history-rollback.jsonl',{'phase':'database_restored'})
        if current_log!=saved_log:
            parent=os.open(logpath.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            name=None
            try:
                tempfd,name=tempfile.mkstemp(prefix='.time-jobcomp-restore-',dir=str(logpath.parent))
                try:
                    os.fchown(tempfd,before.st_uid,before.st_gid); os.fchmod(tempfd,stat.S_IMODE(before.st_mode))
                    data=memoryview(saved_log)
                    while data:
                        n=os.write(tempfd,data)
                        if n<=0: raise OSError('short jobcomp restore write')
                        data=data[n:]
                    os.fsync(tempfd)
                finally: os.close(tempfd)
                snapshot.requirements()
                if snapshot.identity(before)!=snapshot.identity(os.stat(logpath.name,dir_fd=parent,follow_symlinks=False)):
                    raise ValueError('jobcomp changed during restoration')
                os.replace(name,logpath.name,dst_dir_fd=parent); name=None; os.fsync(parent)
            finally:
                if name is not None: os.unlink(name)
                os.close(parent)
        migration.journal_append(backup/'history-rollback.jsonl',{'phase':'jobcomp_restored'})
        return {'database_restored':True,'jobcomp_restored':True}
    finally: os.close(logfd)
