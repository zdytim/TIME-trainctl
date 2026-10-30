#!/usr/bin/env python3
"""Exercise packaging against disposable local fixtures only."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

tool = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location('packager', tool)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
revision = 'a' * 40
with tempfile.TemporaryDirectory(prefix='time-package-check-') as tmp:
    base = Path(tmp)
    stage = base / ('slurm19-' + revision[:12])
    (stage / 'sbin').mkdir(parents=True)
    (stage / 'lib/slurm').mkdir(parents=True)
    binary = b'fixture-controller'
    (stage / 'sbin/slurmctld').write_bytes(binary)
    for name in module.REQUIRED:
        (stage / 'lib/slurm' / (name + '.so')).write_bytes(name.encode())
    def package(out):
        return subprocess.run([sys.executable, '-B', str(tool), '--stage', str(stage),
            '--output', str(out), '--revision', revision,
            '--controller-sha256', hashlib.sha256(binary).hexdigest()], capture_output=True, text=True)
    first, second = base / 'one', base / 'two'
    assert package(first).returncode == 0
    assert package(second).returncode == 0
    a, b = (first / 'controller.tar.gz').read_bytes(), (second / 'controller.tar.gz').read_bytes()
    assert a == b
    manifest = json.loads((first / 'manifest.json').read_text())
    assert manifest['archive_sha256'] == hashlib.sha256(a).hexdigest()
    with tarfile.open(first / 'controller.tar.gz', 'r:gz') as archive:
        members = archive.getmembers()
        assert len(members) == len(module.REQUIRED) + 1
        assert all(member.isfile() and member.uid == 0 and member.gid == 0 for member in members)
        assert {member.name for member in members} == {item['path'] for item in manifest['files']}
    assert package(first).returncode != 0  # never replace an output directory
    missing = stage / 'lib/slurm/auth_munge.so'
    missing.unlink()
    assert package(base / 'missing').returncode != 0
    missing.symlink_to(stage / 'lib/slurm/cred_munge.so')
    assert package(base / 'symlink').returncode != 0
    print('PASS: reproducible candidate archive, manifest, missing and symlink refusal')
