#!/usr/bin/env python3
"""Emit hash evidence for a staged priority build; does not deploy anything."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
PREFIX='/opt/slurm/26.05.5-timequota-priority-20261116'
OFFICIAL='4680cafc82b0689865c885eb703441e7ebf764896c40fd03a50f2fcb60664f68'
ARTIFACTS=('sbin/slurmctld','sbin/slurmd','lib/slurm/job_submit_pro6000d.so',
           'lib/slurm/select_cons_tres.so','lib/slurm/sched_backfill.so','lib/slurm/cgroup_v2.so')


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''): h.update(part)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--official-archive',type=Path,required=True)
    args=parser.parse_args()
    if os.uname().machine!='x86_64': raise SystemExit('target x86_64 build required')
    base=args.candidate.resolve(strict=True)
    if not re.fullmatch(r'/tmp/time26-candidate-[A-Za-z0-9]{8}',str(base)):
        raise SystemExit('unexpected candidate path')
    if (base/'build.exit').read_text().strip()!='0': raise SystemExit('candidate build is not complete')
    verification=ROOT/'tools/verify-priority-source.py'
    spec=importlib.util.spec_from_file_location('priority_source_verifier',verification)
    verifier=importlib.util.module_from_spec(spec); spec.loader.exec_module(verifier)
    source_hash=verifier.verify(args.official_archive,base/'source.tar.gz')
    submit_hash=digest(base/'job_submit_pro6000d.c')
    if submit_hash != digest(ROOT/'deploy/job_submit_pro6000d.c'):
        raise SystemExit('submit source differs from release source')
    stage=base/'stage'/PREFIX.lstrip('/')
    if not stage.is_dir(): raise SystemExit('missing staged release')
    artifacts={}
    for name in ARTIFACTS:
        path=stage/name
        if path.is_symlink() or not path.is_file(): raise SystemExit('missing artifact: '+name)
        with path.open('rb') as f: header=f.read(20)
        if len(header)<20 or header[:6]!=b'\x7fELF\x02\x01' or header[18:20]!=b'\x3e\x00':
            raise SystemExit('not an x86_64 ELF: '+name)
        artifacts[name]=digest(path)
    for name,marker in (('sbin/slurmctld',b'PRO6000D_MANDATORY_GPU_ALLOCATION_GATE_BUILD_V1'),
                        ('sbin/slurmd',b'PRO6000D_DURABLE_NODE_CLEANUP_PROOF_BUILD_V1')):
        data=(stage/name).read_bytes()
        if marker not in data or b'TEST_ONLY_PRO6000D' in data:
            raise SystemExit('candidate capability marker invalid: '+name)
    helper=ROOT/'deploy/release_tree.py'
    spec=importlib.util.spec_from_file_location('release_tree',helper)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    print(json.dumps({'version':1,'architecture':'x86_64','official_source_sha256':OFFICIAL,
                      'source_archive_sha256':source_hash,
                      'submit_source_sha256':submit_hash,
                      'build_script_sha256':digest(ROOT/'validation/build-slurm26-target-staged.sh'),
                      'integration_patch_sha256':digest(ROOT.parent/'research/pro6000d/slurm26-strict-priority-integration.patch'),
                      'installed_tree_sha256':hashlib.sha256(module.release_tree(stage)).hexdigest(),
                      'prefix':PREFIX,'artifacts':artifacts},indent=2))


if __name__=='__main__': main()
