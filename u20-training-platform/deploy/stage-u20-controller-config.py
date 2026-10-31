#!/usr/bin/env python3
"""Stage controller-only config and witness launcher without activating either."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

RELEASE = Path('/opt/time/slurm19-7a06cfff70a7')
BASE = Path('/etc/slurm-llnl/slurm.conf')
CONFIG = Path('/etc/slurm-llnl/time-controller-7a06.conf')
LAUNCHER = Path('/usr/local/libexec/time-quota-anchor-7a06')
BASE_SHA = '6c6072b71804357a06c270f883981f95b9b2ae4ce35913026b42956d69172c63'
MANIFEST_SHA = 'a2b59034b83e7f2f2c384598632a99ff92d0be0e89ca361db2712dd453d5103d'
INPUTS = {
    'config.py': '7f0146536dc9db262699db61a96922a3a3e90b42655a6306da6f91a4745f04f7',
    'launcher.py': 'b705fcb6c16739bd6b283f137399c66cbc0ea642d1e0155110efd4ef8c854ba8',
}


def digest(data): return hashlib.sha256(data).hexdigest()


def trusted(path, directory):
    for component in reversed((path,) + tuple(path.parents)):
        st = component.lstat()
        is_dir = component != path or directory
        if (stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_gid != 0 or st.st_mode & 0o022 or
                (not stat.S_ISDIR(st.st_mode) if is_dir else not stat.S_ISREG(st.st_mode))):
            raise ValueError('untrusted path: ' + str(component))
    if not directory and path.lstat().st_nlink != 1:
        raise ValueError('file must have one link: ' + str(path))


def read_pinned(path, expected):
    trusted(path, False)
    data = path.read_bytes()
    if digest(data) != expected: raise ValueError('unexpected digest: ' + str(path))
    return data


def verify_release():
    trusted(RELEASE, True)
    manifest = json.loads(read_pinned(RELEASE / 'manifest.json', MANIFEST_SHA))
    if manifest.get('runtime_prefix') != str(RELEASE): raise ValueError('unexpected release prefix')
    entries = {item['path']: item for item in manifest['files']}
    if len(entries) != 99: raise ValueError('unexpected release manifest')
    seen = set()
    for path in RELEASE.rglob('*'):
        st = path.lstat()
        if st.st_uid != 0 or st.st_gid != 0 or stat.S_ISLNK(st.st_mode):
            raise ValueError('untrusted release member: ' + str(path))
        if stat.S_ISDIR(st.st_mode):
            if stat.S_IMODE(st.st_mode) != 0o755: raise ValueError('unexpected directory mode')
            continue
        name = str(path.relative_to(RELEASE))
        if name == 'manifest.json': continue
        item = entries.get(name)
        if (not item or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
                stat.S_IMODE(st.st_mode) != int(item['mode'], 8) or st.st_size != item['size'] or
                digest(path.read_bytes()) != item['sha256']):
            raise ValueError('unexpected release member: ' + name)
        seen.add(name)
    if seen != set(entries): raise ValueError('release members missing')


def create(path, data, mode):
    trusted(path.parent, True)
    parent = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fchmod(out.fileno(), mode); os.fsync(out.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def main(directory):
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('expected u20 host and root are required')
    trusted(directory, True)
    inputs = {name: read_pinned(directory / name, sha) for name, sha in INPUTS.items()}
    verify_release()
    original = read_pinned(BASE, BASE_SHA)
    spec = importlib.util.spec_from_loader('time_u20_config', loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(inputs['config.py'], str(directory / 'config.py'), 'exec'), module.__dict__)
    candidate = module.candidate_config(original)
    # Refuse before writing either file if either name already exists.
    for path in (CONFIG, LAUNCHER):
        if path.exists() or path.is_symlink(): raise FileExistsError('staged path already exists: ' + str(path))
    if read_pinned(BASE, BASE_SHA) != original: raise ValueError('baseline changed during staging')
    # A partial staging remains visible and must be inspected, never overwritten.
    create(CONFIG, candidate, 0o644)
    create(LAUNCHER, inputs['launcher.py'], 0o755)
    print(json.dumps({'config': str(CONFIG), 'config_sha256': digest(candidate),
                      'launcher': str(LAUNCHER), 'launcher_sha256': digest(inputs['launcher.py']),
                      'activated': False, 'note': 'no service or live configuration changed'}))


if __name__ == '__main__':
    if len(sys.argv) != 2: raise SystemExit('usage: stage-u20-controller-config.py ROOT_OWNED_INPUT_DIR')
    try: main(Path(sys.argv[1]).absolute())
    except (OSError, ValueError, KeyError) as exc:
        print('Staging refused; inspect any partial files: ' + str(exc), file=sys.stderr); sys.exit(1)
