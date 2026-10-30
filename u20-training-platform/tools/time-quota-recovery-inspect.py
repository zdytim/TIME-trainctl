#!/usr/bin/env python3
"""Inspect a stopped controller's WAL, checkpoint and witness without repairing them.

Matching digests do not prove that job state, historical usage or the WAL's
business semantics are correct. Preserve all originals before any recovery.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys

LIMIT = 128 * 1024 * 1024
HEADER = b'U20MQ1|HEADER|1\n'
CHECKPOINT = re.compile(rb'U20MQ1\|CHECKPOINT\|(0|[1-9][0-9]*)\|([0-9a-f]{64})\n\Z')
WITNESS = re.compile(rb'U20MQA1\|(0|[1-9][0-9]*)\|([0-9a-f]{64})\n\Z')


def directory(path, owner, private=False):
    path = Path(os.path.abspath(path))
    before = path.lstat()
    if (not stat.S_ISDIR(before.st_mode) or before.st_uid != owner or
            before.st_mode & (0o077 if private else 0o022)):
        raise ValueError('untrusted directory: ' + str(path))
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise ValueError('directory changed while opening: ' + str(path))
    return fd


def locked_file(directory_fd, name, owner):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner or
                before.st_nlink != 1 or before.st_mode & 0o077 or before.st_size > LIMIT):
            raise ValueError('untrusted or oversized file: ' + name)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, before
    except Exception:
        os.close(fd)
        raise


def content(fd, before, name):
    result = bytearray()
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        result.extend(block)
        if len(result) > LIMIT:
            raise ValueError(name + ' grew beyond inspection limit')
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(result) != before.st_size:
        raise ValueError(name + ' changed during inspection')
    return bytes(result)


def inspect(wal, checkpoint, witness):
    cp = CHECKPOINT.fullmatch(checkpoint)
    lines = witness.splitlines(keepends=True)
    if not cp or not lines:
        raise ValueError('malformed checkpoint or empty witness')
    last = None
    witness_digests = []
    for index, line in enumerate(lines):
        record = WITNESS.fullmatch(line)
        if not record or int(record.group(1)) != index:
            raise ValueError('malformed or nonconsecutive witness')
        last = record
        witness_digests.append(record.group(2).decode())
    digest = hashlib.sha256(wal).hexdigest()
    # This is a structural count, not a replay of controller business rules.
    wal_lines = wal.splitlines(keepends=True)
    if not wal_lines or wal_lines[0] != HEADER:
        raise ValueError('invalid WAL header')
    for seq, line in enumerate(wal_lines[1:], 1):
        if len(line) > 1023 or not line.endswith(b'\n') or b'\0' in line:
            raise ValueError('invalid WAL line framing')
        fields = line.split(b'|', 3)
        if len(fields) < 4 or fields[0] != b'U20MQ1' or fields[1] != str(seq).encode():
            raise ValueError('invalid WAL sequence')
    wal_seq = len(wal_lines) - 1
    # Each witness record contains the digest of the WAL through that event.
    # Check every available prefix, not merely the final witness record.
    prefix = hashlib.sha256()
    history_matches = len(witness_digests) <= len(wal_lines)
    for index, line in enumerate(wal_lines):
        prefix.update(line)
        if index < len(witness_digests) and prefix.hexdigest() != witness_digests[index]:
            history_matches = False
    checkpoint_seq, checkpoint_digest = int(cp.group(1)), cp.group(2).decode()
    witness_seq, witness_digest = int(last.group(1)), last.group(2).decode()
    match = (history_matches and wal_seq == checkpoint_seq == witness_seq and
             digest == checkpoint_digest == witness_digest)
    return {'matching': match, 'wal_sequence': wal_seq, 'checkpoint_sequence': checkpoint_seq,
            'witness_sequence': witness_seq, 'wal_sha256': digest,
            'checkpoint_sha256': checkpoint_digest, 'witness_sha256': witness_digest,
            'witness_history_matches_wal': history_matches,
            'recovery_readiness': 'not assessed',
            'note': 'No replay or repair was performed; preserve originals and reconcile Slurm job state.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default='/var/lib/slurm/ctld')
    parser.add_argument('--witness-dir', default='/var/lib/time-quota-anchor')
    parser.add_argument('--service-user', default='slurm')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('root required')
    uid = pwd.getpwnam(args.service_user).pw_uid
    dirs, files = [], []
    try:
        dirs.append(directory(args.witness_dir, 0, private=True))
        dirs.append(directory(args.state_dir, uid))
        # Launcher holds witness first; controller subsequently locks WAL and checkpoint.
        for parent, name, owner in ((dirs[0], 'witness.log', 0),
                                    (dirs[1], 'monthly-quota.wal', uid),
                                    (dirs[1], 'monthly-quota.checkpoint', uid)):
            fd, info = locked_file(parent, name, owner)
            files.append((fd, info, name))
        witness, wal, checkpoint = [content(*item) for item in files]
        result = inspect(wal, checkpoint, witness)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result['matching'] else 2
    finally:
        for fd, _, _ in reversed(files):
            os.close(fd)
        for fd in reversed(dirs):
            os.close(fd)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print('Inspection refused; originals unchanged: %s' % exc, file=sys.stderr)
        sys.exit(1)
