#!/usr/bin/env python3
"""Make/verify a Git proof for the platform and pro6000d research trees.

The proof contains commit/tree objects, not unrelated project blobs.
The pushed branch itself must still be verified separately against origin.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


def object_id(kind, data):
    return hashlib.sha1(kind.encode()+b' '+str(len(data)).encode()+b'\0'+data).hexdigest()


def git(repo, *args):
    return subprocess.run(['git','-C',str(repo),*args],check=True,stdout=subprocess.PIPE).stdout


def parse_tree(data):
    entries=[]; pos=0
    while pos<len(data):
        space=data.find(b' ',pos); zero=data.find(b'\0',space+1)
        if space<0 or zero<0 or zero+21>len(data): raise ValueError('invalid Git tree object')
        mode=data[pos:space].decode('ascii'); name=data[space+1:zero].decode('utf-8')
        oid=data[zero+1:zero+21].hex(); pos=zero+21
        if not name or name in ('.','..') or '/' in name or '\0' in name: raise ValueError('unsafe Git tree path')
        entries.append((mode,name,oid))
    if len({item[1] for item in entries})!=len(entries): raise ValueError('duplicate Git tree entry')
    return entries


def make_proof(repo, commit):
    if git(repo,'rev-parse',commit+'^{commit}').decode().strip()!=commit: raise ValueError('commit is not exact')
    raw=git(repo,'cat-file','commit',commit)
    if object_id('commit',raw)!=commit: raise ValueError('commit object mismatch')
    root=next(line[5:].decode() for line in raw.splitlines() if line.startswith(b'tree '))
    trees={}
    def get(oid):
        data=git(repo,'cat-file','tree',oid)
        if object_id('tree',data)!=oid: raise ValueError('tree object mismatch')
        trees[oid]=base64.b64encode(data).decode()
        return parse_tree(data)
    entries=get(root)
    found=[oid for mode,name,oid in entries if name=='pro6000d-live-platform' and mode in ('40000','040000')]
    if len(found)!=1: raise ValueError('platform tree not found')
    def recurse(oid):
        for mode,name,child in get(oid):
            if mode in ('40000','040000'): recurse(child)
            elif mode not in ('100644','100755','120000'): raise ValueError('unsupported platform Git mode')
    recurse(found[0])
    research=[oid for mode,name,oid in entries if name=='research' and mode in ('40000','040000')]
    if len(research)!=1: raise ValueError('research tree not found')
    target=[oid for mode,name,oid in get(research[0]) if name=='pro6000d' and mode in ('40000','040000')]
    if len(target)!=1: raise ValueError('pro6000d research tree not found')
    recurse(target[0])
    return {'version':1,'commit':commit,'commit_object':base64.b64encode(raw).decode(),'trees':trees}


def verify_proof(platform, proof_path, commit, *, research=None, owner=0, check_ancestors=True):
    platform=Path(platform).resolve(strict=True)
    research=Path(research if research is not None else platform.parent/'research/pro6000d').resolve(strict=True)
    proof_path=Path(proof_path)
    info=proof_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022 or info.st_nlink != 1:
        raise ValueError('untrusted Git proof file')
    if check_ancestors:
        for parent in (*platform.parents, *research.parents, *proof_path.resolve(strict=True).parents):
            info=parent.stat()
            if info.st_uid != owner or info.st_mode & 0o022:
                raise ValueError('untrusted release parent directory')
    data=json.loads(proof_path.read_text())
    if data.get('version')!=1 or data.get('commit')!=commit: raise ValueError('release proof commit mismatch')
    raw=base64.b64decode(data['commit_object'],validate=True)
    if object_id('commit',raw)!=commit: raise ValueError('release commit object mismatch')
    roots=[line[5:].decode('ascii') for line in raw.splitlines() if line.startswith(b'tree ')]
    if len(roots)!=1: raise ValueError('invalid commit tree')
    trees=data['trees']
    def get(oid):
        raw_tree=base64.b64decode(trees[oid],validate=True)
        if object_id('tree',raw_tree)!=oid: raise ValueError('release tree hash mismatch')
        return parse_tree(raw_tree)
    root_entries=get(roots[0])
    found=[oid for mode,name,oid in root_entries if name=='pro6000d-live-platform' and mode in ('40000','040000')]
    if len(found)!=1: raise ValueError('release platform tree missing')
    research_tree=[oid for mode,name,oid in root_entries if name=='research' and mode in ('40000','040000')]
    if len(research_tree)!=1: raise ValueError('release research tree missing')
    target=[oid for mode,name,oid in get(research_tree[0]) if name=='pro6000d' and mode in ('40000','040000')]
    if len(target)!=1: raise ValueError('release pro6000d research tree missing')
    def walk(oid,directory,ancestors):
        if oid in ancestors or len(ancestors)>64: raise ValueError('cyclic release tree')
        ancestors=ancestors | {oid}
        info=directory.stat()
        if info.st_uid != owner or info.st_mode & 0o022: raise ValueError('untrusted release directory')
        entries=get(oid)
        if set(os.listdir(directory)) != {name for _,name,_ in entries}:
            raise ValueError('unexpected release directory entries')
        for mode,name,child in entries:
            path=directory/name
            if mode in ('40000','040000'):
                if path.is_symlink() or not path.is_dir(): raise ValueError('release directory mismatch')
                walk(child,path,ancestors)
            else:
                info=path.lstat()
                if info.st_uid != owner or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                    raise ValueError('untrusted release artifact')
                if mode=='120000':
                    if not stat.S_ISLNK(info.st_mode): raise ValueError('release symlink mismatch')
                    content=os.fsencode(os.readlink(path))
                elif mode in ('100644','100755'):
                    if not stat.S_ISREG(info.st_mode) or bool(info.st_mode & 0o111)!=(mode=='100755'):
                        raise ValueError('release file mode mismatch')
                    content=path.read_bytes()
                else: raise ValueError('unsupported release Git mode')
                if object_id('blob',content)!=child: raise ValueError('release file hash mismatch: '+str(path))
    walk(found[0],platform,set())
    walk(target[0],research,set())
    return True


def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='action',required=True)
    m=sub.add_parser('make'); m.add_argument('--repo',required=True,type=Path); m.add_argument('--commit',required=True); m.add_argument('--output',required=True,type=Path)
    v=sub.add_parser('verify'); v.add_argument('--platform',required=True,type=Path); v.add_argument('--commit',required=True); v.add_argument('--proof',required=True,type=Path)
    args=p.parse_args()
    if args.action=='make': args.output.write_text(json.dumps(make_proof(args.repo,args.commit),sort_keys=True)+'\n')
    else:
        verify_proof(args.platform,args.proof,args.commit); print('Git platform subtree verified')

if __name__=='__main__': main()
