#!/usr/bin/python3
"""Restore a platform installation snapshot; retain database, logs and user data."""
import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess

def run(argv,check=True):
    result=subprocess.run(argv,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError('%s: %s'%(argv[0],result.stderr[-4000:]))
    return result

def fingerprint(path):
    path=Path(path)
    if path.is_symlink(): return {'link':os.readlink(path)}
    if path.is_file(): return {'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    if path.is_dir(): return {'tree':{str(p.relative_to(path)):fingerprint(p) for p in sorted(path.rglob('*')) if not p.is_dir()}}
    return {'missing':True}

def remove(path):
    if path.is_symlink() or path.is_file(): path.unlink()
    elif path.is_dir(): shutil.rmtree(path)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('backup')
    args=parser.parse_args()
    if os.geteuid()!=0: raise SystemExit('Run with sudo')
    backup=Path(args.backup).resolve()
    root=Path('/var/backups/pro6000d-training')
    if backup.parent!=root or backup.stat().st_uid!=0 or backup.stat().st_mode & 0o022:
        raise SystemExit('Expected a root-owned platform backup directory')
    manifest=json.loads((backup/'manifest.json').read_text())
    if manifest.get('rolled_back'): raise SystemExit('This snapshot has already been restored')
    # Do not silently erase post-install administrator edits.
    conflicts=[r['path'] for r in manifest['files'] if r.get('installed_fingerprint') and fingerprint(r['path'])!=r['installed_fingerprint']]
    if conflicts: raise SystemExit('Configuration changed since this snapshot; review before rollback:\n'+'\n'.join(conflicts))
    queue=run(['squeue','-h'],False)
    if queue.returncode==0 and queue.stdout.strip(): raise SystemExit('Active jobs exist; rollback refuses to stop them')
    if queue.returncode!=0 and run(['systemctl','is-active','slurmctld'],False).returncode==0:
        raise SystemExit('Queue status is unavailable; refusing scheduler shutdown')
    registry=Path('/var/lib/pro6000d-training/enrolled-users.json')
    enrolled=json.loads(registry.read_text()) if registry.exists() else {}
    before=manifest.get('enrolled_users_before',{})
    for name,record in enrolled.items():
        if run(['pgrep','-u',str(record['uid'])],False).returncode==0:
            raise SystemExit('Log out enrolled user %s before removing isolation'%name)
    services=manifest.get('managed_services',[])
    for service in services:
        run(['systemctl','stop',service],False)
        if manifest['services'].get(service,{}).get('enabled') not in ('enabled','enabled-runtime'):
            run(['systemctl','disable',service],False)
    # Remove only this deployment's scheduler ingress rule, never other chains.
    firewall=Path('/usr/local/libexec/pro6000d-loopback-firewall')
    if 'pro6000d-loopback-firewall.service' in services and firewall.exists():
        run([str(firewall),'stop'])
    for record in reversed(manifest['files']):
        target=Path(record['path'])
        if not target.is_absolute() or '..' in target.parts: raise RuntimeError('Invalid backup target')
        remove(target)
        if record['existed']:
            source=backup/'files'/str(target).lstrip('/')
            target.parent.mkdir(parents=True,exist_ok=True)
            if source.is_symlink(): os.symlink(os.readlink(source),target)
            elif source.is_dir(): shutil.copytree(source,target,symlinks=True)
            else: shutil.copy2(source,target)
    for name,record in enrolled.items():
        if name in before:
            continue
        target=Path(record['slice_file'])
        if target.exists():
            if target.read_text()!='[Slice]\nDevicePolicy=closed\n':
                raise RuntimeError('Refusing to remove edited slice file '+str(target))
            target.unlink()
        if not record.get('was_member'):
            run(['gpasswd','-d',name,'pro6000d-training'],False)
    if registry.exists():
        shutil.copy2(registry,backup/'enrolled-users-at-rollback.json')
        if before:
            registry.write_text(json.dumps(before,indent=2)+'\n');os.chmod(registry,0o600)
        else:
            registry.unlink()
    for user,group in manifest.get('admin_memberships_added',[]): run(['gpasswd','-d',user,group],False)
    # Restore exact original ACLs for devices present at installation.
    acl=backup/'gpu-acls.txt'
    if acl.exists(): run(['setfacl','--restore='+str(acl)])
    run(['systemctl','daemon-reload'])
    run(['udevadm','control','--reload-rules'])
    for service in services:
        prior=manifest['services'].get(service,{})
        if prior.get('enabled')=='enabled': run(['systemctl','enable',service],False)
        if prior.get('active')=='active': run(['systemctl','start',service])
    for name in manifest.get('new_groups',[]):
        try: group=grp.getgrnam(name)
        except KeyError: continue
        if not group.gr_mem and not any(p.pw_gid==group.gr_gid for p in pwd.getpwall()):
            run(['groupdel',name],False)
    manifest['rolled_back']=True;(backup/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('Restored configuration. Database, job logs, accounts and user files were retained.')
    print('Cockpit and prerequisite packages remain installed; they predate this platform snapshot.')

if __name__=='__main__':
    main()
