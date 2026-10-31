#!/usr/bin/env python3
"""Experimental locked marker write primitive; no production entry point.

The caller must independently establish file paths, safe service state, WAL
semantics, scheduler reconciliation, approval snapshot and protected backups.
This primitive cannot authorize recovery or restart a controller.
"""
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import stat

path=Path(__file__).resolve().parent/'time-quota-recovery-inspect.py'
spec=importlib.util.spec_from_file_location('time_marker_inspector',path)
inspect=importlib.util.module_from_spec(spec); spec.loader.exec_module(inspect)


def snapshot(fd, owner):
    before=os.fstat(fd)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner or before.st_nlink != 1 or
            before.st_mode & 0o077 or before.st_size > inspect.LIMIT):
        raise ValueError('untrusted marker descriptor')
    data=bytearray(); offset=0
    while offset < before.st_size:
        block=os.pread(fd,min(1024*1024,before.st_size-offset),offset)
        if not block: raise ValueError('short read')
        data.extend(block); offset+=len(block)
    after=os.fstat(fd)
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (
            after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise ValueError('marker changed during read')
    return bytes(data), (before.st_dev,before.st_ino)


def write_markers(wal_fd, checkpoint_fd, witness_fd, expected, plan, service_uid, witness_uid):
    """Write exact markers under locks; caller owns open FDs and all policy checks."""
    if len(expected)!=3 or any(not isinstance(x,bytes) for x in expected):
        raise ValueError('invalid expected contents')
    if (fcntl.fcntl(checkpoint_fd,fcntl.F_GETFL) & (os.O_ACCMODE|os.O_APPEND)) != os.O_RDWR:
        raise ValueError('checkpoint must be read/write without append')
    if (fcntl.fcntl(witness_fd,fcntl.F_GETFL) & (os.O_ACCMODE|os.O_APPEND)) != (os.O_RDWR|os.O_APPEND):
        raise ValueError('witness must be read/write append')
    locked=[]
    try:
        for fd in (witness_fd,wal_fd,checkpoint_fd):
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); locked.append(fd)
        old=[]; identities=[]
        for fd,uid in ((wal_fd,service_uid),(checkpoint_fd,service_uid),(witness_fd,witness_uid)):
            data,identity=snapshot(fd,uid); old.append(data); identities.append(identity)
        if len(set(identities)) != 3 or tuple(old) != tuple(expected):
            raise ValueError('marker identity or expected contents changed')
        current=inspect.inspect(*old)
        if current['observation'] not in ('wal_one_event_ahead','witness_one_event_behind'):
            raise ValueError('unexpected marker state')
        wal,cp,witness=old; seq=str(current['wal_sequence']).encode()
        digest=hashlib.sha256(wal).hexdigest().encode()
        target_cp=b'U20MQ1|CHECKPOINT|'+seq+b'|'+digest+b'\n'
        suffix=b'U20MQA1|'+seq+b'|'+digest+b'\n'
        if (not isinstance(plan,dict) or plan.get('repair_authorized') is not False or
                plan.get('checkpoint') != target_cp or plan.get('witness') != witness+suffix or
                not inspect.inspect(wal,target_cp,witness+suffix)['matching']):
            raise ValueError('candidate bytes do not match current WAL')
        if cp != target_cp:
            offset=0
            while offset < len(target_cp):
                n=os.pwrite(checkpoint_fd,target_cp[offset:],offset)
                if n <= 0: raise OSError('short checkpoint write')
                offset+=n
            os.ftruncate(checkpoint_fd,len(target_cp)); os.fsync(checkpoint_fd)
        offset=0
        while offset < len(suffix):
            n=os.write(witness_fd,suffix[offset:])
            if n <= 0: raise OSError('short witness write')
            offset+=n
        os.fsync(witness_fd)
        if not inspect.inspect(snapshot(wal_fd,service_uid)[0],snapshot(checkpoint_fd,service_uid)[0],
                               snapshot(witness_fd,witness_uid)[0])['matching']:
            raise ValueError('written marker verification failed')
    finally:
        for fd in reversed(locked): fcntl.flock(fd,fcntl.LOCK_UN)
