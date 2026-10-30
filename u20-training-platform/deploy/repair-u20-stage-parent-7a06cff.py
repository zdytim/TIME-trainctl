#!/usr/bin/env python3
"""Repair only the parent traversal mode of the newly staged 7a06cff bundle."""
import hashlib
import json
import os
from pathlib import Path
import stat

parent = Path('/opt/time')
release = parent / 'slurm19-7a06cfff70a7'
expected_manifest = 'a2b59034b83e7f2f2c384598632a99ff92d0be0e89ca361db2712dd453d5103d'

def main():
    if os.geteuid() != 0:
        raise ValueError('root required')
    fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('parent differs from the exact staged state')
        if sorted(x.name for x in parent.iterdir()) != [release.name]:
            raise ValueError('unexpected content in release parent')
        release_stat = release.lstat()
        if (not stat.S_ISDIR(release_stat.st_mode) or release_stat.st_uid != 0 or
                release_stat.st_gid != 0 or stat.S_IMODE(release_stat.st_mode) != 0o755):
            raise ValueError('unexpected staged release directory')
        manifest_path = release / 'manifest.json'
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_manifest:
            raise ValueError('staged manifest digest mismatch')
        manifest = json.loads(raw)
        expected = {item['path']: item for item in manifest['files']}
        if len(expected) != 99 or manifest['runtime_prefix'] != str(release):
            raise ValueError('unexpected staged release identity')
        found = set()
        for path in release.rglob('*'):
            st = path.lstat()
            if st.st_uid != 0 or st.st_gid != 0 or stat.S_ISLNK(st.st_mode):
                raise ValueError('unexpected owner or link: ' + str(path))
            if stat.S_ISDIR(st.st_mode):
                if stat.S_IMODE(st.st_mode) != 0o755:
                    raise ValueError('unexpected directory mode: ' + str(path))
                continue
            if not stat.S_ISREG(st.st_mode):
                raise ValueError('unexpected file type: ' + str(path))
            name = str(path.relative_to(release))
            if name == 'manifest.json':
                if stat.S_IMODE(st.st_mode) != 0o644:
                    raise ValueError('unexpected manifest mode')
                continue
            if (name not in expected or stat.S_IMODE(st.st_mode) != 0o755 or
                    hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]['sha256']):
                raise ValueError('unexpected staged member: ' + name)
            found.add(name)
        if found != set(expected):
            raise ValueError('missing staged member')
        # Parent is root-only throughout verification; only this descriptor is changed.
        os.fchmod(fd, 0o755)
        os.fsync(fd)
        print('Repaired /opt/time from 0700 to 0755 after verifying all 99 staged members.')
    finally:
        os.close(fd)

if __name__ == '__main__':
    main()
