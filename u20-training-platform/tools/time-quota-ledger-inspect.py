#!/usr/bin/env python3
"""Read-only offline inventory of a Slurm monthly ledger; never repairs it.

Run as the controller's service user only after the controller releases the
ledger locks. An unavailable lock means the controller or another inspector may
still be using the files; the tool exits without reading their contents.
"""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import stat
import sys


def open_locked(directory, name):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(name, flags, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError(name + " must be a private, single-link service-owned regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, info
    except Exception:
        os.close(fd)
        raise


def digest_file(fd):
    digest = hashlib.sha256()
    size = 0
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        size += len(block)
        digest.update(block)
    return size, digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, help="existing Slurm StateSaveLocation")
    args = parser.parse_args()
    path = pathlib.Path(os.path.abspath(args.state_dir))
    value = os.lstat(str(path))
    if (not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode) or
            value.st_uid != os.geteuid() or value.st_mode & 0o022):
        raise ValueError("state directory must be a non-writable-by-others service-owned directory")
    directory = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                        getattr(os, "O_NOFOLLOW", 0))
    opened = []
    try:
        observed = os.fstat(directory)
        if (observed.st_dev, observed.st_ino) != (value.st_dev, value.st_ino):
            raise ValueError("state directory changed while opening")
        # Match the controller's lock order; never examine a partial pair.
        for name in ("monthly-quota.wal", "monthly-quota.checkpoint"):
            fd, info = open_locked(directory, name)
            opened.append((name, fd, info))
        records = []
        for name, fd, before in opened:
            size, digest = digest_file(fd)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or size != before.st_size:
                raise ValueError(name + " changed during inspection")
            records.append({"name": name, "size_bytes": size, "sha256": digest,
                            "owner_uid": before.st_uid, "mode": oct(stat.S_IMODE(before.st_mode))})
        print(json.dumps({"files": records, "controller_health": "not assessed",
                          "recovery_readiness": "not assessed",
                          "note": "Metadata only; preserve originals and review journal and scheduler state."},
                         sort_keys=True, indent=2))
    finally:
        for _name, fd, _info in reversed(opened):
            os.close(fd)
        os.close(directory)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print("Ledger inspection unavailable; no files were changed: %s" % exc, file=sys.stderr)
        sys.exit(1)
