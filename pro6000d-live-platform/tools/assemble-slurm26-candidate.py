#!/usr/bin/env python3
"""Reconstruct the experimental Slurm 26 source in a new directory.

This tool does not configure, build, install, start, or enable Slurm.
The candidate remains unsafe until the integration is completed and reviewed.
"""
import argparse
import hashlib
import pathlib
import shutil
import subprocess
import tarfile

SHA256 = '4680cafc82b0689865c885eb703441e7ebf764896c40fd03a50f2fcb60664f68'
PATCHES = (
    'slurm26-reservation-draft.patch', 'slurm26-aborted-reservation-draft.patch',
    'slurm26-recovery-fail-closed-draft.patch', 'slurm26-controller-lifecycle-draft.patch',
    'slurm26-preserve-witness-fd-draft.patch', 'slurm26-reservation-owner-scope-draft.patch',
    'slurm26-other-user-reservation-draft.patch', 'slurm26-reservation-release-draft.patch',
    'slurm26-gres-alloc-status-draft.patch', 'slurm26-gres-propagate-status-draft.patch',
    'slurm26-concurrent-commit-draft.patch', 'slurm26-allocation-transaction-draft.patch',
    'slurm26-preplugin-abort-draft.patch', 'slurm26-same-user-reservation-draft.patch',
    'slurm26-sluid-identity-draft.patch', 'slurm26-reservation-status-draft.patch',
    'slurm26-request-tombstones-draft.patch', 'slurm26-reservation-cancel-race-draft.patch',
    'slurm26-capacity-denial.patch', 'slurm26-request-shape-fix.patch',
    'slurm26-disabled-gate-fix.patch', 'slurm26-cpu-only-fix.patch',
    'slurm26-node-settlement-candidate.patch', 'slurm26-month-rollover-candidate.patch',
    'slurm26-readiness-candidate.patch', 'slurm26-priority-candidate.patch',
    'slurm26-cancel-bound-candidate.patch', 'slurm26-autocancel-candidate.patch',
    'slurm26-cancel-recovery-candidate.patch', 'slurm26-ending-metadata-candidate.patch',
    'slurm26-node-proof-candidate.patch', 'slurm26-selector-evidence-candidate.patch',
    'slurm26-recovery-defer-candidate.patch', 'slurm26-selector-rebuild-candidate.patch',
)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', required=True, type=pathlib.Path)
    p.add_argument('--output', required=True, type=pathlib.Path, help='new directory, must not exist')
    args = p.parse_args()
    archive = args.archive.resolve(strict=True)
    digest = hashlib.sha256()
    with archive.open('rb') as src:
        for block in iter(lambda: src.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != SHA256:
        raise ValueError('Slurm archive SHA-256 does not match the target source')
    output = args.output.absolute()
    repo = pathlib.Path(__file__).resolve().parents[2]
    patches = repo / 'research/pro6000d'
    output.mkdir(mode=0o700, exist_ok=False)
    try:
        with tarfile.open(archive, 'r:bz2') as tar:
            for item in tar:
                path = pathlib.PurePosixPath(item.name)
                if (not path.parts or path.parts[0] != 'slurm-26.05.5' or path.is_absolute()
                        or any(part in ('', '.', '..') for part in path.parts)
                        or not (item.isfile() or item.isdir())):
                    raise ValueError('unexpected archive member: ' + item.name)
                dest = output.joinpath(*path.parts)
                if item.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(item) as source, dest.open('xb') as target:
                        shutil.copyfileobj(source, target)
                    dest.chmod(item.mode & 0o777)
        source = output / 'slurm-26.05.5'
        for name in PATCHES:
            patch = patches / name
            if patch.is_symlink() or not patch.is_file():
                raise ValueError('missing patch or symbolic link: ' + str(patch))
            subprocess.run(['git', 'apply', '--unidiff-zero', str(patch)], cwd=source, check=True)
            print('applied', name, flush=True)
        print('assembled:', source)
    except BaseException:
        # Leave partial output for diagnosis; do not remove user files.
        print('assembly failed; partial output retained:', output, flush=True)
        raise


if __name__ == '__main__':
    main()
