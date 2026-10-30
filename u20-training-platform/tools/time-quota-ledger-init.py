#!/usr/bin/env python3
"""Provision an entirely new, empty staged Slurm monthly ledger.

Run only as the intended slurmctld service user, after independently deciding
that this is a fresh ledger. Never use this to recover or replace a ledger.
"""

import argparse
import hashlib
import os
import pathlib
import stat
import sys

CHECKPOINT = "monthly-quota.checkpoint"
WAL = "monthly-quota.wal"
WAL_HEADER = b"U20MQ1|HEADER|1\n"


def checked_directory(path):
    path = pathlib.Path(os.path.abspath(path))
    uid = os.geteuid()
    for component in reversed((path,) + tuple(path.parents)):
        value = component.lstat()
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
            raise ValueError("path contains a symlink or non-directory: %s" % component)
        if value.st_uid not in (0, uid) or value.st_mode & 0o022:
            raise ValueError("directory must be owned by root or service user and not group/other writable: %s" % component)
    st = path.lstat()
    if st.st_uid != uid:
        raise ValueError("state directory must be owned by the current service user")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    actual = os.fstat(fd)
    if (actual.st_dev, actual.st_ino) != (st.st_dev, st.st_ino):
        os.close(fd)
        raise ValueError("state directory changed during validation")
    return path, fd


def create_file(dir_fd, name, content):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_nlink != 1:
            raise ValueError("created file is not a single-link service-owned regular file")
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short ledger write")
            view = view[written:]
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, help="existing Slurm StateSaveLocation")
    parser.add_argument("--new-empty-ledger", action="store_true",
                        help="acknowledge that this is a fresh ledger, not recovery")
    args = parser.parse_args()
    if not args.new_empty_ledger:
        parser.error("refusing without --new-empty-ledger")
    path, directory = checked_directory(args.state_dir)
    try:
        # Check both before creating either; O_EXCL remains the final guard.
        for name in (CHECKPOINT, WAL):
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError("refusing to replace existing %s" % name)
        # A partial provisioning is intentionally left as evidence; the
        # controller fails closed and a later attempt cannot overwrite it.
        checkpoint = b"U20MQ1|CHECKPOINT|0|" + hashlib.sha256(WAL_HEADER).hexdigest().encode("ascii") + b"\n"
        create_file(directory, CHECKPOINT, checkpoint)
        os.fsync(directory)
        create_file(directory, WAL, WAL_HEADER)
        os.fsync(directory)
        print("Created new empty ledger in %s for uid %d" % (path, os.geteuid()))
        print("Quota enforcement remains disabled until separately installed and activated.")
    finally:
        os.close(directory)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print("Ledger initialization failed; retain any created files for review: %s" % exc, file=sys.stderr)
        sys.exit(1)
