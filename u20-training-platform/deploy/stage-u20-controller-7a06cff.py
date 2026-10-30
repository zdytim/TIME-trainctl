#!/usr/bin/env python3
"""Stage a pinned u20 controller bundle; do not change services or configuration."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile

REVISION = '7a06cfff70a74019a132737964cb229b1ee21b37'
MANIFEST_SHA = 'a2b59034b83e7f2f2c384598632a99ff92d0be0e89ca361db2712dd453d5103d'
VERIFIER_SHA = '5b17290756781798af8bfcac395a79b7ff80ac239976b470328a3874717019df'
PARENT = Path('/opt/time')
DEST = PARENT / ('slurm19-' + REVISION[:12])


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def trusted_dir(path):
    for part in reversed((path,) + tuple(path.parents)):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('untrusted directory: ' + str(part))


def copy_input(source, target, limit):
    fd = os.open(str(source), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError('invalid input file: ' + str(source))
        with target.open('xb') as out:
            total = 0
            while True:
                data = os.read(fd, 1024 * 1024)
                if not data:
                    break
                total += len(data)
                if total > limit:
                    raise ValueError('input exceeded size limit')
                out.write(data)
            out.flush()
            os.fsync(out.fileno())
    finally:
        os.close(fd)


def rename_noreplace(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    func = libc.renameat2
    func.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    if func(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--verifier', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('root required')
    os.umask(0o077)
    verifier = args.verifier.absolute()
    trusted_dir(verifier.parent)
    st = verifier.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022 or sha(verifier) != VERIFIER_SHA:
        raise ValueError('untrusted or unexpected verifier')
    trusted_dir(PARENT.parent)
    try:
        PARENT.mkdir(mode=0o755)
    except FileExistsError:
        pass
    trusted_dir(PARENT)
    if DEST.exists() or DEST.is_symlink():
        raise FileExistsError('release path already exists; refusing to replace it')
    work = Path(tempfile.mkdtemp(prefix='.time-stage-', dir=str(PARENT)))
    try:
        copied_archive, copied_manifest = work / 'controller.tar.gz', work / 'manifest.json'
        copy_input(args.archive, copied_archive, 512 * 1024 * 1024)
        copy_input(args.manifest, copied_manifest, 1024 * 1024)
        subprocess.run(['/usr/bin/python3', str(verifier), '--archive', str(copied_archive),
                        '--manifest', str(copied_manifest), '--manifest-sha256', MANIFEST_SHA],
                       check=True, timeout=120)
        manifest = json.loads(copied_manifest.read_text())
        if manifest['project_revision'] != REVISION or manifest['runtime_prefix'] != str(DEST):
            raise ValueError('unexpected release identity')
        expected = {item['path']: item for item in manifest['files']}
        release = work / 'release'
        (release / 'sbin').mkdir(parents=True, mode=0o755)
        (release / 'lib/slurm').mkdir(parents=True, mode=0o755)
        with tarfile.open(copied_archive, 'r:gz') as archive:
            for item in archive:
                # The verifier already checked the immutable root-owned copy.
                target = release / item.name
                with archive.extractfile(item) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fchmod(dst.fileno(), 0o755)
                    os.fsync(dst.fileno())
                if sha(target) != expected[item.name]['sha256']:
                    raise ValueError('extracted member digest mismatch: ' + item.name)
        shutil.copyfile(copied_manifest, release / 'manifest.json')
        os.chmod(release / 'manifest.json', 0o644)
        with (release / 'manifest.json').open('rb') as f:
            os.fsync(f.fileno())
        for directory in (release / 'sbin', release / 'lib/slurm', release / 'lib', release):
            os.chmod(directory, 0o755)
            fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        rename_noreplace(release, DEST)
        fd = os.open(str(PARENT), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        print('Staged root-owned candidate:', DEST)
        print('No Slurm service, GPU, configuration or job was changed.')
    finally:
        shutil.rmtree(work)


if __name__ == '__main__':
    main()
