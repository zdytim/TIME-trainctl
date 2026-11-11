#!/usr/bin/env python3
"""Fenced first activation of the reviewed pro6000d monthly quota release.

Run only from a verified committed release tree after the staged platform
upgrade. It refuses pre-existing monthly ledgers and any Slurm queue entries.
A failure before admission reopens restores the previous service/configuration;
the root-only backup retains the new quota ledger as audit evidence.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/var/lib/slurm/slurmctld')
WITNESS = Path('/var/lib/pro6000d-monthly')
CONF = Path('/etc/slurm/slurm.conf')
GRES = Path('/etc/slurm/gres.conf')
CGROUP = Path('/etc/slurm/cgroup.conf')
CONFIG = Path('/etc/pro6000d-training/config.json')
MANIFEST = Path('/etc/pro6000d-training/monthly-release.json')
CONTROLLER_DROPIN = Path('/etc/systemd/system/slurmctld.service.d/monthly.conf')
NODE_DROPIN = Path('/etc/systemd/system/slurmd.service.d/monthly.conf')
NODE_UNIT = Path('/etc/systemd/system/slurmd.service')
WRAPPER = Path('/usr/local/libexec/pro6000d-quota-controller')
CURRENT = Path('/opt/slurm/current')
PREFIX = Path('/opt/slurm/26.05.5-timequota-20261111')
SERVICE = 'pro6000d-training.service'
SAFE_ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin', 'LANG':'C', 'TZ':'UTC', 'SLURM_CONF':'/etc/slurm/slurm.conf'}


def run(*args, timeout=30):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, env=SAFE_ENV).stdout.strip()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()


def replace(path, content, mode):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.monthly-release-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, mode); os.replace(name, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        if os.path.exists(name): os.unlink(name)


def create_private(path, content, uid=0, gid=0):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'wb') as stream:
            fd = -1; stream.write(content); stream.flush(); os.fsync(stream.fileno())
    finally:
        if fd >= 0: os.close(fd)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def empty_queue():
    # All queued, running, suspended and completing jobs must finish naturally;
    # neither cancelling nor modifying an existing job is part of cutover.
    return not run('/usr/local/bin/squeue', '--all', '-h', '-o', '%i %T')


def require_single_idle_target():
    partitions=run('/usr/local/bin/sinfo','--all','--noheader','--format','%P').splitlines()
    if [part.rstrip('*') for part in partitions] != ['gpu']:
        raise RuntimeError('unexpected partitions; cannot fence the complete scheduler')
    nodes=run('/usr/local/bin/scontrol','show','nodes','-o').splitlines()
    if len(nodes)!=1 or not re.search(r'(?:^|\s)NodeName=pro6000d(?:\s|$)',nodes[0]) or not re.search(r'(?:^|\s)State=IDLE(?:\s|$)',nodes[0]):
        raise RuntimeError('target must be the only idle Slurm node')


def wait_single_idle_target(timeout=45):
    deadline=time.monotonic()+timeout
    while True:
        try:
            require_single_idle_target()
            return
        except RuntimeError:
            if time.monotonic() >= deadline: raise
            time.sleep(.5)


def platform_queue_empty(dbpath):
    active = ('WAITING_QUOTA','CANCELLING_RESERVATION','SUBMITTING','PENDING',
              'RUNNING','COMPLETING','SUSPENDED')
    with sqlite3.connect('file:'+str(dbpath)+'?mode=ro',uri=True,timeout=10) as db:
        return not db.execute('SELECT 1 FROM jobs WHERE state IN ('+','.join('?' for _ in active)+') LIMIT 1',active).fetchone()


def configured_conf(text, partition_state):
    if partition_state not in ('UP','INACTIVE'):
        raise ValueError('invalid release partition state')
    if re.search(r'^\s*include(?:\s|=)', text, re.I | re.M):
        raise RuntimeError('included Slurm configuration is unsupported')
    if re.search(r'^\s*(SlurmctldParameters|PluginDir|ReconfigFlags)\s*=', text, re.I | re.M):
        raise RuntimeError('existing controller parameters, reconfiguration flags or plugin directory need explicit migration')
    lines=text.splitlines()
    partitions=[i for i,line in enumerate(lines) if re.match(r'^\s*PartitionName\s*=',line,re.I)]
    if len(partitions)!=1 or not re.search(r'\bPartitionName\s*=\s*gpu(?:\s|$)',lines[partitions[0]],re.I):
        raise RuntimeError('release requires exactly the gpu partition')
    i=partitions[0]
    active, separator, comment = lines[i].partition('#')
    states=re.findall(r'\bState\s*=\s*(\S+)',active,re.I)
    if len(states)!=1 or states[0].upper()!='UP':
        raise RuntimeError('gpu partition must explicitly and uniquely be UP')
    active,count=re.subn(r'(\bState\s*=\s*)UP(?=\s|$)',lambda m:m.group(1)+partition_state,active,count=1,flags=re.I)
    if count!=1: raise RuntimeError('gpu partition state could not be rewritten')
    lines[i]=active+(separator+comment if separator else '')
    return ('\n'.join(lines).rstrip() + '\nSlurmctldParameters=MonthlyGpuQuota\nPluginDir=/opt/slurm/current/lib/slurm\n').encode()


def configured_node_unit(text):
    section = None
    removed = 0
    result = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped
        if section == '[Unit]' and stripped.startswith('After='):
            values = stripped[6:].split()
            removed += values.count('slurmctld.service')
            values = [value for value in values if value != 'slurmctld.service']
            line = 'After=' + ' '.join(values) + '\n'
        result.append(line)
    if removed != 1:
        raise RuntimeError('unexpected node service ordering; manual review required')
    return ''.join(result).encode()


def configured_gres(text):
    # NVML sorts by PCI links, which can differ from device-minor order.
    # The release binds bitmap positions to minors, so use explicit files.
    lines = text.splitlines()
    active = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith('#')]
    if active != ['AutoDetect=nvml', 'Name=gpu File=/dev/nvidia[0-3]']:
        raise RuntimeError('unexpected GRES configuration; manual review required')
    return ''.join('AutoDetect=off\n' if line.strip() == 'AutoDetect=nvml' else line + '\n'
                   for line in lines).encode()


def wait_enabled_admin_state(uid, gid, timeout=15):
    request = json.dumps({'method':'GET','path':'/api/v1/state'}) + '\n'
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            reply = subprocess.run(['setpriv','--reuid='+str(uid),'--regid='+str(gid),'--init-groups',
                                    '/usr/local/bin/pro6000d-training-client'],input=request,text=True,
                                   capture_output=True,timeout=max(.01,deadline-time.monotonic()),
                                   check=True,env=SAFE_ENV)
            data = json.loads(reply.stdout)
            q = data.get('data',{}).get('monthly_quota',{})
            if (data.get('ok') is not True or q.get('enforcement_ready') is not True or
                    q.get('selectable_tiers_ready') is not True or q.get('request_available') is not True):
                raise RuntimeError('platform did not report fully enabled monthly controls')
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
        remaining = deadline-time.monotonic()
        if remaining > 0: time.sleep(min(.5,remaining))
    raise RuntimeError('platform admin state did not become ready') from last_error


def check_release_bootstrap():
    # The operator verifies the uploaded archive hash before root extraction.
    # Independently reject writable paths before importing release helpers.
    for path in (ROOT/'deploy', ROOT, *ROOT.parents):
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('release source parent is not root-managed')
    for path in (Path(__file__),ROOT/'deploy/release_tree.py',ROOT/'deploy/git_release_proof.py'):
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
            raise RuntimeError('release bootstrap is not root-managed')


def check_installed_platform():
    for name in ('pro6000d-training-api.py', 'monthly_quota.py', 'monthly_reservation.py',
                 'controller_bridge.py', 'controller_view.py', 'activation_attestation.py'):
        if digest(ROOT/'backend'/name) != digest(Path('/usr/local/lib/pro6000d-training')/name):
            raise RuntimeError('staged backend differs from committed release: '+name)
    ui = ROOT/'cockpit-plugin/pro6000d-training'
    dest = Path('/usr/share/cockpit/pro6000d-training')
    for source in ui.rglob('*'):
        if source.is_symlink(): raise RuntimeError('unexpected UI symlink')
        if source.is_file() and digest(source) != digest(dest/source.relative_to(ui)):
            raise RuntimeError('staged UI differs from committed release')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--remote-ref-commit', required=True)
    parser.add_argument('--git-proof', required=True, type=Path)
    parser.add_argument('--controller-sha256', required=True)
    parser.add_argument('--node-sha256', required=True)
    parser.add_argument('--select-sha256', required=True)
    parser.add_argument('--scheduler-sha256', required=True)
    parser.add_argument('--submit-sha256', required=True)
    parser.add_argument('--cgroup-sha256', required=True)
    parser.add_argument('--tree-sha256', required=True)
    args = parser.parse_args()
    if not sys.flags.isolated: raise SystemExit('run the release script with /usr/bin/python3 -I')
    if os.geteuid() != 0 or os.uname().nodename != 'ubuntu': raise SystemExit('root on pro6000d required')
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit) or args.remote_ref_commit != args.commit:
        raise SystemExit('full commit and verified pushed target ref are required')
    checksums={'sbin/slurmctld':args.controller_sha256, 'sbin/slurmd':args.node_sha256,
               'lib/slurm/select_cons_tres.so':args.select_sha256, 'lib/slurm/sched_backfill.so':args.scheduler_sha256,
               'lib/slurm/job_submit_pro6000d.so':args.submit_sha256, 'lib/slurm/cgroup_v2.so':args.cgroup_sha256}
    if not all(re.fullmatch(r'[0-9a-f]{64}', h) for h in (*checksums.values(),args.tree_sha256)):
        raise SystemExit('candidate hashes required')
    base = args.candidate.resolve(strict=True)
    if not re.fullmatch(r'/tmp/time26-candidate-[A-Za-z0-9]{8}', str(base)):
        raise SystemExit('unexpected candidate directory')
    if (base/'build.exit').read_text().strip() != '0': raise SystemExit('candidate build not successful')
    staged = base/'stage'/str(PREFIX).lstrip('/')
    if any(digest(staged/path) != expected for path,expected in checksums.items()):
        raise SystemExit('candidate executable or plugin checksum mismatch')
    for file, marker in ((staged/'sbin/slurmctld',b'PRO6000D_MANDATORY_GPU_ALLOCATION_GATE_BUILD_V1'),
                         (staged/'sbin/slurmd',b'PRO6000D_DURABLE_NODE_CLEANUP_PROOF_BUILD_V1')):
        contents=file.read_bytes()
        if marker not in contents or b'TEST_ONLY_PRO6000D' in contents: raise SystemExit('candidate capability marker invalid')
    check_release_bootstrap()
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(ROOT/'deploy'))
    from release_tree import release_tree
    if hashlib.sha256(release_tree(staged)).hexdigest() != args.tree_sha256:
        raise SystemExit('candidate tree does not match reviewed build')
    from git_release_proof import verify_proof
    try:
        verify_proof(ROOT,args.git_proof,args.commit)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit('release files do not match the committed Git tree') from exc
    evidence=json.loads((ROOT.parent/'research/pro6000d/20261111-build-evidence.json').read_text())
    if (evidence.get('version') != 1 or evidence.get('slurm_version') != '26.05.5' or
            evidence.get('installed_tree_sha256') != args.tree_sha256 or evidence.get('artifacts') != checksums or
            digest(base/'source.tar.gz') != evidence.get('base_source_sha256') or
            digest(base/'overrides.tar.gz') != evidence.get('overlay_sha256')):
        raise SystemExit('build evidence does not match reviewed candidate')
    import importlib.util
    spec=importlib.util.spec_from_file_location('release_assembler',ROOT/'tools/assemble-slurm26-candidate.py')
    assembler=importlib.util.module_from_spec(spec); spec.loader.exec_module(assembler)
    PATCHES=assembler.PATCHES
    if evidence.get('official_archive_sha256') != assembler.SHA256:
        raise SystemExit('official source archive evidence mismatch')
    listed=evidence.get('patches',[])
    if [item.get('name') for item in listed] != list(PATCHES):
        raise SystemExit('build patch order differs from committed assembler')
    for item in listed:
        if digest(ROOT.parent/'research/pro6000d'/item['name']) != item.get('sha256'):
            raise SystemExit('build patch checksum mismatch')
    source_files=evidence.get('source_files',{})
    required_sources={'src/slurmctld/monthly_quota.h','src/slurmctld/monthly_quota.c',
        'src/slurmctld/read_config.c','src/slurmctld/proc_req.c','src/slurmctld/job_mgr.c',
        'src/slurmctld/agent.c','src/slurmctld/controller.c','src/slurmctld/node_scheduler.c',
        'src/slurmctld/monthly_quota_control.c','src/slurmctld/locks.c','src/slurmctld/locks.h',
        'src/slurmd/slurmd/req.c','src/slurmd/slurmd/slurmd.c','src/interfaces/select.c',
        'src/interfaces/select.h','src/plugins/select/linear/select_linear.c',
        'src/plugins/select/cons_tres/select_cons_tres.c','src/plugins/select/cons_tres/job_resources.c'}
    if set(source_files) != required_sources or any(digest(base/'source'/name) != expected for name,expected in source_files.items()):
        raise SystemExit('built source files differ from committed evidence')
    check_installed_platform()
    if not CURRENT.is_symlink() or CURRENT.resolve() != Path('/opt/slurm/26.05.5') or PREFIX.exists():
        raise SystemExit('unexpected existing Slurm installation')
    if any(path.exists() for path in (STATE/'monthly-quota.wal',STATE/'monthly-quota.checkpoint',WITNESS,MANIFEST,CONTROLLER_DROPIN,NODE_DROPIN,WRAPPER)):
        raise SystemExit('monthly state or service files already exist; explicit recovery is required')
    config=json.loads(CONFIG.read_text())
    if config.get('monthly_quota',{}).get('enabled') or config.get('monthly_controller',{}).get('activation_enabled'):
        raise SystemExit('monthly accounting is already enabled')
    dbpath=Path(config['state_db'])
    with sqlite3.connect('file:'+str(dbpath)+'?mode=ro',uri=True) as db:
        for table in ('monthly_usage','monthly_additions','monthly_allocations'):
            if db.execute('SELECT count(*) FROM '+table).fetchone()[0]:
                raise SystemExit('legacy monthly data requires migration')
    original_conf=CONF.read_text()
    original_node_unit=NODE_UNIT.read_text()
    final_node_unit=configured_node_unit(original_node_unit)
    final_gres=configured_gres(GRES.read_text())
    fenced_conf=configured_conf(original_conf,'INACTIVE')
    final_conf=configured_conf(original_conf,'UP')
    part=run('/usr/local/bin/scontrol','show','partition','gpu','-o')
    if not re.search(r'(?:^|\s)State=UP(?:\s|$)',part): raise SystemExit('gpu partition is not UP')
    for unit in ('slurmctld','slurmd',SERVICE): run('systemctl','is-active','--quiet',unit)
    if not empty_queue() or not platform_queue_empty(dbpath):
        raise SystemExit('Slurm or platform has active jobs; existing jobs must finish naturally')
    require_single_idle_target()
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup=Path('/var/backups/pro6000d-training')/('monthly-release-'+stamp)
    backup.mkdir(parents=True,mode=0o700)
    for path in (CONF,GRES,CONFIG,NODE_UNIT):
        dest=backup/'files'/str(path).lstrip('/'); dest.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(path,dest)
    with sqlite3.connect(str(dbpath)) as live, sqlite3.connect(str(backup/'state.sqlite3')) as saved:
        live.backup(saved)
    os.chmod(backup/'state.sqlite3',0o600)
    record={'commit':args.commit,'host':os.uname().nodename,'started_utc':stamp,
            'candidate':str(base),'backup':str(backup),'controller_sha256':args.controller_sha256,
            'pushed_ref':'origin/pro6000d-slurm26','pushed_ref_commit':args.remote_ref_commit,
            'node_sha256':args.node_sha256,'tree_sha256':args.tree_sha256,
            'source_archive_sha256':evidence['base_source_sha256'],
            'source_overlay_sha256':evidence['overlay_sha256'],
            'official_archive_sha256':evidence['official_archive_sha256'],
            'patches':evidence['patches'],'state':'prepared','rollback':'not needed'}
    def save(): replace(backup/'release.json',(json.dumps(record,indent=2)+'\n').encode(),0o600)
    save()
    stopped_backend = partition_down = services_stopped = changed = admission_open = False
    def mark(step):
        record['step']=step; save()
    try:
        mark('stopping platform dispatch')
        stopped_backend=True; run('systemctl','stop',SERVICE)
        if not empty_queue() or not platform_queue_empty(dbpath):
            raise RuntimeError('jobs appeared while fencing the platform')
        # INACTIVE disables both job submission and scheduling. The target
        # has exactly one partition, so no other entry point can allocate.
        mark('fencing all scheduler submission and allocation')
        partition_down=True; run('/usr/local/bin/scontrol','update','PartitionName=gpu','State=INACTIVE')
        part=run('/usr/local/bin/scontrol','show','partition','gpu','-o')
        if not re.search(r'(?:^|\s)State=INACTIVE(?:\s|$)',part):
            raise RuntimeError('scheduler fence could not be verified')
        if not empty_queue(): raise RuntimeError('jobs appeared before the scheduler fence')
        mark('stopping slurm services')
        services_stopped=True; run('systemctl','stop','slurmd','slurmctld')
        mark('installing verified candidate')
        temp_prefix=PREFIX.with_name('.'+PREFIX.name+'-'+stamp)
        shutil.copytree(staged,temp_prefix,symlinks=True)
        for root,dirs,files in os.walk(temp_prefix,followlinks=False):
            os.chown(root,0,0)
            for name in dirs+files: os.chown(Path(root)/name,0,0,follow_symlinks=False)
        if (any(digest(temp_prefix/path) != expected for path,expected in checksums.items()) or
                hashlib.sha256(release_tree(temp_prefix)).hexdigest() != args.tree_sha256):
            raise RuntimeError('installed candidate tree checksum mismatch')
        os.rename(temp_prefix,PREFIX); changed=True
        sync_dir(PREFIX.parent)
        mark('initializing new quota ledger')
        WITNESS.mkdir(mode=0o700)
        slurm=pwd.getpwnam('slurm'); header=b'U20MQ1|HEADER|1\n'; h=hashlib.sha256(header).hexdigest().encode()
        create_private(STATE/'monthly-quota.wal',header,slurm.pw_uid,slurm.pw_gid)
        create_private(STATE/'monthly-quota.checkpoint',b'U20MQ1|CHECKPOINT|0|'+h+b'\n',slurm.pw_uid,slurm.pw_gid)
        create_private(WITNESS/'witness',b'U20MQA1|0|'+h+b'\n')
        create_private(WITNESS/'node-proof.wal',b'P6NP1|HEADER|1\n')
        sync_dir(STATE); sync_dir(WITNESS); sync_dir(WITNESS.parent)
        replace(CONF,fenced_conf,stat.S_IMODE(CONF.stat().st_mode))
        replace(GRES,final_gres,stat.S_IMODE(GRES.stat().st_mode))
        replace(NODE_UNIT,final_node_unit,stat.S_IMODE(NODE_UNIT.stat().st_mode))
        replace(WRAPPER,(ROOT/'deploy/quota-controller-wrapper.py').read_bytes(),0o755)
        replace(CONTROLLER_DROPIN,(ROOT/'deploy/slurmctld-monthly.conf').read_bytes(),0o644)
        replace(NODE_DROPIN,(ROOT/'deploy/slurmd-monthly.conf').read_bytes(),0o644)
        target_tmp=CURRENT.with_name('.monthly-current-'+stamp)
        os.symlink(str(PREFIX),target_tmp); os.replace(target_tmp,CURRENT); sync_dir(CURRENT.parent)
        artifacts={'controller':PREFIX/'sbin/slurmctld','node':PREFIX/'sbin/slurmd',
                   'submit':PREFIX/'lib/slurm/job_submit_pro6000d.so',
                   'scheduler':PREFIX/'lib/slurm/sched_backfill.so',
                   'select':PREFIX/'lib/slurm/select_cons_tres.so',
                   'cgroup':PREFIX/'lib/slurm/cgroup_v2.so',
                   'slurm_conf':CONF,'gres_conf':GRES,'cgroup_conf':CGROUP}
        files={name:{'path':str(path.resolve(strict=True)),'sha256':digest(path)} for name,path in artifacts.items()}
        files['node_proof']={'path':str(WITNESS/'node-proof.wal')}
        release={'version':1,'files':files,'pidfiles':{'controller':'/run/slurmctld/slurmctld.pid','node':'/run/slurmd/slurmd.pid'}}
        replace(MANIFEST,(json.dumps(release,indent=2)+'\n').encode(),0o600)
        mark('starting paired slurm services')
        run('systemctl','daemon-reload')
        if 'slurmctld.service' in run('systemctl','show','slurmd.service','-p','After','--value').split():
            raise RuntimeError('node service still ordered after controller')
        run('systemctl','start','slurmd')
        sys.path.insert(0,str(ROOT/'backend'))
        from controller_bridge import ControllerBridge, ControllerUnavailable
        bridge=ControllerBridge(str(STATE/'time-quota.sock'),slurm.pw_uid)
        deadline=time.monotonic()+60
        while True:
            try:
                if bridge.snapshot(0): break
            except ControllerUnavailable: pass
            if time.monotonic() >= deadline: raise RuntimeError('paired controller did not start')
            time.sleep(.5)
        part=run('/usr/local/bin/scontrol','show','partition','gpu','-o')
        if not re.search(r'(?:^|\s)State=INACTIVE(?:\s|$)',part) or not empty_queue():
            raise RuntimeError('partition fence did not survive controller restart')
        mark('activating empty monthly ledger')
        bridge.selectable_cutover_reset_usage()
        if not bridge.readiness(): raise RuntimeError('controller did not attest selectable gate')
        from activation_attestation import verify_release
        if not verify_release(str(MANIFEST),slurm.pw_uid): raise RuntimeError('paired release attestation failed')
        # Prepare a second start from the final UP configuration.  The first
        # start is always fenced; after this one begins, a direct Slurm user
        # could submit, so automatic rollback must no longer stop services.
        wait_single_idle_target()
        if not empty_queue(): raise RuntimeError('unexpected job before final controller restart')
        mark('stopping fenced paired services for final configuration')
        run('systemctl','stop','slurmd','slurmctld')
        replace(CONF,final_conf,stat.S_IMODE(CONF.stat().st_mode))
        release['files']['slurm_conf']['sha256']=digest(CONF)
        replace(MANIFEST,(json.dumps(release,indent=2)+'\n').encode(),0o600)
        config['monthly_controller']={'read_enabled':True,'activation_enabled':True,'grant_enabled':True,
                                      'controller_user':'slurm','socket_path':str(STATE/'time-quota.sock'),
                                      'release_manifest':str(MANIFEST)}
        replace(CONFIG,(json.dumps(config,indent=2,ensure_ascii=False)+'\n').encode(),stat.S_IMODE(CONFIG.stat().st_mode))
        record.update(state='admission-boundary', rollback='automatic rollback disabled after final controller start begins')
        save(); admission_open=True
        run('systemctl','start','slurmd')
        deadline=time.monotonic()+60
        while True:
            try:
                if bridge.readiness() and verify_release(str(MANIFEST),slurm.pw_uid): break
            except ControllerUnavailable: pass
            if time.monotonic() >= deadline: raise RuntimeError('final paired controller did not become ready')
            time.sleep(.5)
        part=run('/usr/local/bin/scontrol','show','partition','gpu','-o')
        if not re.search(r'(?:^|\s)State=UP(?:\s|$)',part): raise RuntimeError('final gpu partition did not open')
        partition_down=False
        run('systemctl','start',SERVICE); stopped_backend=False
        admin=pwd.getpwnam('ubuntu')
        wait_enabled_admin_state(admin.pw_uid,admin.pw_gid)
        record.update(state='active',completed_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                      components=['slurmctld','slurmd','plugins','quota ledger','platform API','Cockpit'],
                      verification='empty queue, paired release attestation, controller READY1, admin API enabled; production allocation test pending')
        save()
        print('Monthly release activated; backup and evidence:',backup)
    except Exception as exc:
        record.update(state='failed',error=str(exc))
        if 'temp_prefix' in locals() and temp_prefix.exists():
            shutil.rmtree(temp_prefix)
        if admission_open:
            record['rollback']='not attempted after partition admission boundary; inspect queued and running jobs first'
            save(); raise
        try:
            if not stopped_backend: run('systemctl','stop',SERVICE)
            if changed:
                run('systemctl','stop','slurmd','slurmctld')
                temp=CURRENT.with_name('.rollback-current-'+stamp)
                os.symlink('/opt/slurm/26.05.5',temp); os.replace(temp,CURRENT); sync_dir(CURRENT.parent)
                if PREFIX.exists(): shutil.rmtree(PREFIX)
                for path in (MANIFEST,CONTROLLER_DROPIN,NODE_DROPIN,WRAPPER):
                    if path.exists(): path.unlink()
                for path in (CONF,GRES,CONFIG,NODE_UNIT): shutil.copy2(backup/'files'/str(path).lstrip('/'),path)
                run('systemctl','daemon-reload')
            if services_stopped: run('systemctl','start','slurmctld','slurmd')
            if partition_down: run('/usr/local/bin/scontrol','update','PartitionName=gpu','State=UP')
            run('systemctl','start',SERVICE)
            for unit in ('slurmctld','slurmd',SERVICE): run('systemctl','is-active','--quiet',unit)
            record['rollback']='previous Slurm and platform configuration restored; new quota ledger retained in place'
        except Exception as rollback_error:
            record['rollback']='FAILED: '+str(rollback_error)
        save()
        raise

if __name__ == '__main__': main()
