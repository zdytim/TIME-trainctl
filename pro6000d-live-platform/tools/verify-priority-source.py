#!/usr/bin/env python3
"""Verify staged source archive equals the committed assembler's output."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
OFFICIAL='4680cafc82b0689865c885eb703441e7ebf764896c40fd03a50f2fcb60664f68'


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''): h.update(part)
    return h.hexdigest()


def files(root):
    root=Path(root); out={}
    for current,dirs,names in os.walk(root,followlinks=False):
        for name in dirs+names:
            path=Path(current)/name
            if path.is_symlink(): raise ValueError('source symlink is unsupported')
            if path.is_file():
                relative=path.relative_to(root).as_posix()
                out[relative]=(digest(path),bool(path.stat().st_mode & 0o111))
            elif not path.is_dir(): raise ValueError('special source file is unsupported')
    return out


def verify(official, staged_archive):
    with tempfile.TemporaryDirectory(prefix='priority-source-verify-') as tmp:
        tmp=Path(tmp)
        official_copy=tmp/'official.tar.bz2'; staged_copy=tmp/'staged.tar.gz'
        shutil.copyfile(official,official_copy); shutil.copyfile(staged_archive,staged_copy)
        if digest(official_copy)!=OFFICIAL: raise ValueError('official source archive hash mismatch')
        source=tmp/'source'; source.mkdir(mode=0o700)
        seen=set(); total=0
        with tarfile.open(staged_copy,'r:gz') as tar:
            for entry in tar:
                path=PurePosixPath(entry.name)
                if (not path.parts or path.parts[0]!='slurm-26.05.5' or path.is_absolute() or
                    any(x in ('','.','..') for x in path.parts) or not (entry.isfile() or entry.isdir())):
                    raise ValueError('unexpected staged source archive member')
                if entry.name in seen: raise ValueError('duplicate staged source member')
                seen.add(entry.name); dest=source.joinpath(*path.parts)
                if entry.isdir(): dest.mkdir(parents=True,exist_ok=True)
                else:
                    total+=entry.size
                    if entry.size>100*1024*1024 or total>2*1024*1024*1024:
                        raise ValueError('source archive is too large')
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    with tar.extractfile(entry) as src, dest.open('xb') as out: shutil.copyfileobj(src,out)
                    dest.chmod(entry.mode & 0o777)
        assembled=tmp/'assembled'
        subprocess.run([sys.executable,'-I',str(ROOT/'tools/assemble-slurm26-candidate.py'),
                        '--archive',str(official_copy),'--output',str(assembled)],
                       check=True,stdout=subprocess.DEVNULL,timeout=180)
        expected=files(assembled/'slurm-26.05.5'); actual=files(source/'slurm-26.05.5')
        if expected!=actual: raise ValueError('staged source differs from committed assembly')
        return digest(staged_copy)


if __name__=='__main__':
    if len(sys.argv)!=3: raise SystemExit('usage: verify-priority-source.py OFFICIAL_ARCHIVE STAGED_SOURCE_TAR')
    print(verify(sys.argv[1],sys.argv[2]))
