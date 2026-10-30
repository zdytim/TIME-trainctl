#!/usr/bin/env python3
"""Verify a pinned controller package without extracting or installing it."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile

MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024


def sha_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--manifest-sha256', required=True, help='digest from independently reviewed record')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{64}', args.manifest_sha256):
        raise ValueError('invalid pinned manifest digest')
    for path in (args.archive, args.manifest):
        if path.is_symlink() or not path.is_file():
            raise ValueError('package inputs must be regular files')
    if sha_file(args.manifest) != args.manifest_sha256:
        raise ValueError('manifest digest does not match the pinned record')
    manifest = json.loads(args.manifest.read_text())
    revision = manifest.get('project_revision', '')
    if manifest.get('format') != 1 or not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('invalid manifest format or revision')
    if manifest.get('runtime_prefix') != '/opt/time/slurm19-' + revision[:12]:
        raise ValueError('invalid runtime prefix')
    if sha_file(args.archive) != manifest.get('archive_sha256'):
        raise ValueError('archive digest mismatch')
    expected = {}
    for item in manifest.get('files', []):
        name, size, digest = item.get('path'), item.get('size'), item.get('sha256')
        if (not isinstance(name, str) or not (name == 'sbin/slurmctld' or
                re.fullmatch(r'lib/slurm/[A-Za-z0-9_]+[.]so', name)) or name in expected or
                type(size) is not int or not 0 < size <= MAX_FILE or
                not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest) or
                item.get('mode') != '0755'):
            raise ValueError('invalid or duplicate manifest entry')
        expected[name] = (size, digest)
    if 'sbin/slurmctld' not in expected or 'lib/slurm/libslurmfull.so' not in expected:
        raise ValueError('controller or runtime library missing')
    if sum(size for size, _ in expected.values()) > MAX_TOTAL:
        raise ValueError('package exceeds uncompressed size limit')
    seen = set()
    with tarfile.open(args.archive, 'r:gz') as archive:
        for item in archive:
            if (item.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or item.name not in expected or item.name in seen or
                    item.uid != 0 or item.gid != 0 or item.mode != 0o755 or
                    item.size != expected[item.name][0]):
                raise ValueError('unexpected or unsafe archive entry')
            digest = hashlib.sha256()
            size = 0
            with archive.extractfile(item) as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    size += len(block)
                    digest.update(block)
            if size != item.size or digest.hexdigest() != expected[item.name][1]:
                raise ValueError('archive member digest mismatch')
            seen.add(item.name)
    if seen != set(expected):
        raise ValueError('archive members do not match manifest')
    print('Verified candidate revision', revision, 'with', len(seen), 'members; no files installed.')


if __name__ == '__main__':
    main()
