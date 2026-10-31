#!/usr/bin/env python3
"""Cutover primitive: purge only if the stopped live DB matches its backup.

The caller must hold the maintenance lock, fence submissions and independently
verify no scheduler jobs. This function does not stop or start any services.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat


def logical_digest(db):
    result = hashlib.sha256()
    for line in db.iterdump():
        data = line.encode('utf-8')
        result.update(len(data).to_bytes(8, 'big')); result.update(data)
    return result.hexdigest()


def purge_stopped(backup, purge_core, snapshot):
    """Return deleted row count; rejects any changes since the verified snapshot."""
    backup = Path(backup)
    for item in reversed((backup,) + tuple(backup.parents)):
        st = item.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError('untrusted snapshot directory: ' + str(item))
    if backup.stat().st_mode & 0o077:
        raise ValueError('snapshot must be private')
    def read_private(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1 or st.st_mode & 0o077:
                raise ValueError('untrusted snapshot file: ' + str(path))
            chunks = []
            while True:
                data = os.read(fd, 1024*1024)
                if not data: break
                chunks.append(data)
            return b''.join(chunks)
        finally:
            os.close(fd)
    manifest = json.loads(read_private(backup/'manifest.json'))
    saved = backup/'state.sqlite3'
    digest = hashlib.sha256(read_private(saved)).hexdigest()
    if digest != manifest['database']['sha256']:
        raise ValueError('snapshot database hash mismatch')
    snapshot.requirements()
    # Open the immutable backup and compare its complete logical contents while
    # the live connection holds BEGIN IMMEDIATE. A comparison before acquiring
    # that lock would allow another writer to change the database in between.
    with sqlite3.connect('file:' + str(saved) + '?mode=ro&immutable=1', uri=True) as reference, \
            sqlite3.connect('file:' + str(snapshot.DB) + '?mode=rw', uri=True, timeout=5) as live:
        live.execute('PRAGMA foreign_keys=ON')
        expected = logical_digest(reference)
        def locked_precheck(conn):
            snapshot.requirements()
            if logical_digest(conn) != expected:
                raise ValueError('live database differs from verified snapshot')
        return purge_core.purge(live, precheck=locked_precheck)
