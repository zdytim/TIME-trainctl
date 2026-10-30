#!/usr/bin/env python3
"""Package a built u20 controller and plugins for review; never install them.

The manifest must be reviewed and committed independently before it can be
used as a release identity. This does not prove binary provenance or behavior.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tarfile

REQUIRED = ('auth_munge', 'cred_munge', 'job_submit_lua', 'sched_backfill',
            'select_cons_tres', 'gres_gpu', 'task_cgroup', 'proctrack_cgroup',
            'jobacct_gather_linux', 'accounting_storage_none', 'acct_gather_energy_none',
            'ext_sensors_none', 'mpi_none', 'preempt_none', 'priority_basic',
            'switch_none', 'topology_none', 'jobcomp_filetxt', 'job_container_none',
            'launch_slurm', 'libslurmfull')


def read_stable(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 64 * 1024 * 1024:
            raise ValueError('unsafe or oversized file: ' + str(path))
        chunks = []
        while True:
            part = os.read(fd, 1024 * 1024)
            if not part:
                break
            chunks.append(part)
            if sum(map(len, chunks)) > 64 * 1024 * 1024:
                raise ValueError('file grew beyond limit: ' + str(path))
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('file changed while reading: ' + str(path))
        data = b''.join(chunks)
        if len(data) != before.st_size:
            raise ValueError('file size changed: ' + str(path))
        return data
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new directory')
    parser.add_argument('--revision', required=True, help='full project build commit')
    parser.add_argument('--controller-sha256', required=True)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.revision) or not re.fullmatch('[0-9a-f]{64}', args.controller_sha256):
        raise ValueError('invalid revision or digest')
    if args.stage.name != 'slurm19-' + args.revision[:12]:
        raise ValueError('stage prefix does not match revision')
    for directory in (args.stage, args.stage / 'sbin', args.stage / 'lib', args.stage / 'lib/slurm'):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('stage directory is missing or a symlink: ' + str(directory))
    libdir = args.stage / 'lib/slurm'
    plugins = sorted(path for path in libdir.iterdir() if path.name.endswith('.so'))
    if any(not re.fullmatch('[A-Za-z0-9_]+[.]so', path.name) for path in plugins):
        raise ValueError('unexpected plugin filename')
    names = {path.name for path in plugins}
    missing = sorted(name + '.so' for name in REQUIRED if name + '.so' not in names)
    if missing:
        raise ValueError('required plugins missing: ' + ', '.join(missing))
    inputs = [('sbin/slurmctld', args.stage / 'sbin/slurmctld')]
    inputs.extend(('lib/slurm/' + path.name, path) for path in plugins)
    files = []
    for name, path in inputs:
        data = read_stable(path)
        digest = hashlib.sha256(data).hexdigest()
        if name == 'sbin/slurmctld' and digest != args.controller_sha256:
            raise ValueError('controller digest differs from reviewed build')
        files.append((name, data, digest))
    args.output.mkdir(mode=0o700)
    archive = args.output / 'controller.tar.gz'
    with archive.open('xb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as tar:
                for name, data, _digest in files:
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.uid, info.gid, info.mtime = len(data), 0o755, 0, 0, 0
                    info.uname = info.gname = 'root'
                    tar.addfile(info, io.BytesIO(data))
        raw.flush()
        os.fsync(raw.fileno())
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {'format': 1, 'project_revision': args.revision,
                'runtime_prefix': '/opt/time/slurm19-' + args.revision[:12],
                'archive_sha256': archive_hash,
                'files': [{'path': name, 'size': len(data), 'sha256': digest, 'mode': '0755'}
                          for name, data, digest in files],
                'note': 'candidate package only; no installation or behavioral certification'}
    manifest_path = args.output / 'manifest.json'
    with manifest_path.open('x') as output:
        json.dump(manifest, output, indent=2, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    print('Candidate package:', archive)
    print('Manifest:', manifest_path)
    print('Files:', len(files), 'archive SHA-256:', archive_hash)


if __name__ == '__main__':
    main()
