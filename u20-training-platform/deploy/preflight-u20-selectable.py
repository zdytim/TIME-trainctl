#!/usr/bin/env python3
"""Read-only preflight for the staged selectable-tier u20 upgrade.

This is point-in-time evidence, not a submission fence or deployment. Recheck
all state while fenced immediately before any subsequent cutover.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile

ROOT = Path('/opt/time/selectable-input-f417af5')
RELEASE = Path('/opt/time/slurm19-f417af53729e')
MARKER = Path('/var/lib/pro6000d-training/time-monthly-maintenance')
DB = Path('/var/lib/pro6000d-training/state.sqlite3')
CONFIG = Path('/etc/pro6000d-training/config.json')
LIVE_CONTROLLER = '/opt/time/slurm19-7a06cfff70a7/sbin/slurmctld'
MANIFEST_SHA = '0ce6a721608fc9f6f4668cf9fa54c3c281a5cfcf5d0e012a4c06cce8d15f7bf9'
PLATFORM_MANIFEST_SHA = '986b1f8d94691625ae4f3b6b84134af140c0bfc581f9c2ff51ef5bada91510e5'
PLATFORM_ARCHIVE_SHA = '185bb0b7f08f867b933f82df62f983bba7a28e5268b91a25a9ee6ddc0075dc4a'
BASELINE = {
    '/etc/systemd/system/slurmctld.service.d/time-monthly.conf': 'a1425b14dd252dc8491f6332da2027287992f687f2f7d2d9adc7babb1ff30689',
    '/usr/local/lib/pro6000d-training/pro6000d-training-api.py': '24afd69cff5eac856f2ccaaf8a97b95c1823c911c83f40338745cd4ab813e821',
    '/usr/local/lib/pro6000d-training/controller_bridge.py': '30ac4668f6ea6f0a413f4a4a834b27d42c5db069900600058d18da58e2b04a70',
    '/usr/local/lib/pro6000d-training/controller_view.py': '10be510b5bee9afaec955293dbff499140e099ff612eed1186e7e96e299a637d',
    '/usr/share/cockpit/pro6000d-training/app.js': '3a5f9a14bb92fc8d82a5c40e4e90bfb4d659e4b026ebc3d46ffa7651e959c262',
    '/usr/share/cockpit/pro6000d-training/index.html': 'c94c0ad64450bb20e014a19e65b6a849983590d5ec571be22b33f2353af0d8e6',
    '/usr/share/cockpit/pro6000d-training/styles.css': 'f9ef0f3c17b98b8185f6b3456c27a9b4514417021f449ac1bddbe1114440ac26',
    '/etc/slurm-llnl/job_submit.lua': '1ebf232989864ca67d6c7d1f37dce48ccee1b0afe1159cd13b8dc916cf39f0d7',
}
TERMINAL = ('COMPLETED','FAILED','CANCELLED','CANCELED','TIMEOUT','NODE_FAIL',
            'OUT_OF_MEMORY','PREEMPTED','BOOT_FAIL','DEADLINE','REJECTED','EXPIRED')


def trusted_file(path, expected=None):
    path = Path(path)
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('untrusted parent: '+str(parent))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
        raise ValueError('untrusted file: '+str(path))
    data = path.read_bytes()
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('unexpected file digest: '+str(path))
    return data


def command(argv, env=None):
    return subprocess.run(argv, check=True, text=True, capture_output=True, timeout=15, env=env).stdout.strip()


def validate_release():
    m=json.loads(trusted_file(RELEASE/'manifest.json', MANIFEST_SHA))
    if m.get('project_revision') != 'f417af53729e22bdddb8a54043df80fe07428d32' or m.get('runtime_prefix') != str(RELEASE):
        raise ValueError('unexpected controller release identity')
    files=m.get('files')
    if not isinstance(files,list) or not files: raise ValueError('missing controller manifest entries')
    seen=set()
    for item in files:
        name=item.get('path','')
        if name in seen or not (name == 'sbin/slurmctld' or re.fullmatch(r'lib/slurm/[A-Za-z0-9_]+[.]so',name)):
            raise ValueError('invalid controller manifest path')
        seen.add(name)
        value=trusted_file(RELEASE/name,item.get('sha256'))
        if len(value)!=item.get('size'): raise ValueError('controller file size mismatch: '+name)
    if 'sbin/slurmctld' not in seen or 'lib/slurm/libslurmfull.so' not in seen:
        raise ValueError('controller package incomplete')
    return len(seen)


def validate_platform():
    manifest=json.loads(trusted_file(ROOT/'platform-faa28a6.manifest.json',PLATFORM_MANIFEST_SHA))
    if manifest.get('project_revision')!='faa28a622fef97cc6d5b4cebd795eb16013bfc44' or manifest.get('archive_sha256')!=PLATFORM_ARCHIVE_SHA:
        raise ValueError('unexpected platform package identity')
    archive=ROOT/'platform-faa28a6.tar.gz'
    trusted_file(archive,PLATFORM_ARCHIVE_SHA)
    expected={}
    for item in manifest.get('files',[]):
        name=item.get('path',''); parts=PurePosixPath(name).parts
        if (not parts or parts[0] not in ('ui','backend','scheduler') or
            any(part in ('','.','..') for part in parts) or name.startswith('/') or name in expected or
            type(item.get('size')) is not int or not 0 <= item['size'] <= 16*1024*1024 or
            not re.fullmatch('[0-9a-f]{64}',item.get('sha256',''))):
            raise ValueError('invalid platform manifest path or digest')
        expected[name]=item
    if not expected or sum(item['size'] for item in expected.values()) > 128*1024*1024:
        raise ValueError('invalid platform package size')
    seen=set()
    with tarfile.open(archive,'r:gz') as tar:
        for member in tar:
            if (not member.isfile() or member.name not in expected or member.name in seen or
                member.uid != 0 or member.gid != 0 or member.size != expected[member.name]['size']):
                raise ValueError('invalid platform archive member')
            with tar.extractfile(member) as stream: data=stream.read(member.size+1)
            if len(data)!=member.size or hashlib.sha256(data).hexdigest()!=expected[member.name]['sha256']:
                raise ValueError('platform archive digest mismatch')
            seen.add(member.name)
    if seen != set(expected): raise ValueError('missing platform archive members')
    required={'backend/pro6000d-training-api.py','backend/controller_bridge.py','backend/controller_view.py',
              'scheduler/job_submit-enabled.lua','scheduler/job_submit-disabled.lua',
              'ui/app.js','ui/index.html','ui/styles.css','ui/manifest.json'}
    if not required.issubset(seen): raise ValueError('required platform members missing')
    return len(seen)


def main():
    if os.geteuid()!=0 or os.uname().nodename!='u20-Z790-S-DDR4':
        raise ValueError('root on u20 required')
    if MARKER.exists() or MARKER.is_symlink(): raise ValueError('maintenance fence already present')
    for path, digest in BASELINE.items(): trusted_file(path,digest)
    controller_files=validate_release(); platform_files=validate_platform()
    for service in ('slurmctld','slurmd','pro6000d-training'):
        if command(['/usr/bin/systemctl','is-active',service])!='active': raise ValueError('inactive service: '+service)
    pid=int(command(['/usr/bin/systemctl','show','slurmctld','-p','MainPID','--value']))
    if pid <= 0 or os.readlink('/proc/%d/exe'%pid)!=LIVE_CONTROLLER:
        raise ValueError('unexpected live controller')
    env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','SLURM_CONF':'/etc/slurm-llnl/slurm.conf'}
    if command(['/usr/bin/squeue','-h','-o','%i'],env): raise ValueError('Slurm jobs exist')
    settings=json.loads(trusted_file(CONFIG)); monthly=settings.get('monthly_controller',{})
    if not isinstance(monthly,dict) or not all(monthly.get(k) is True for k in ('read_enabled','grant_enabled','activation_verified')) or monthly.get('selectable_activation_verified') is True:
        raise ValueError('unexpected live monthly controller configuration')
    if settings.get('state_db') != str(DB): raise ValueError('unexpected platform database location')
    with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=5) as db:
        marks=','.join('?' for _ in TERMINAL)
        active=db.execute('SELECT COUNT(*) FROM jobs WHERE state IS NULL OR state NOT IN ('+marks+')',TERMINAL).fetchone()[0]
        if active: raise ValueError('active or ambiguous platform jobs exist')
        unresolved=db.execute("SELECT COUNT(*) FROM monthly_grant_outbox WHERE state IS NULL OR state!='APPLIED'").fetchone()[0]
        if unresolved: raise ValueError('unresolved monthly grant exists')
    if command(['/usr/bin/squeue','-h','-o','%i'],env): raise ValueError('Slurm queue changed during preflight')
    print(json.dumps({'preflight':'passed','controller_pid':pid,'controller_files':controller_files,
                      'platform_files':platform_files,'fenced':False,'activated':False,
                      'note':'point-in-time only; repeat under a durable submission fence'},sort_keys=True))


if __name__=='__main__':
    try: main()
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError,tarfile.TarError) as exc:
        print('Selectable preflight refused: '+str(exc),file=sys.stderr);sys.exit(1)
