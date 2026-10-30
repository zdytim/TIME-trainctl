#!/usr/bin/python3
"""Install the reviewed test platform. Does not install packages or touch passwords."""
import argparse
import datetime
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import stat
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ('pro6000d-training.service','pro6000d-gpu-acl.service','pro6000d-loopback-firewall.service','slurmctld.service','slurmd.service')
PAM_MARKER = '# BEGIN pro6000d training policy'

def run(argv, check=True):
    result = subprocess.run(argv, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError('%s exited %s: %s' % (argv[0],result.returncode,result.stderr[-4000:]))
    return result

def fingerprint(path):
    path=Path(path)
    if path.is_symlink(): return {'link':os.readlink(path)}
    if path.is_file(): return {'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    if path.is_dir(): return {'tree':{str(p.relative_to(path)):fingerprint(p) for p in sorted(path.rglob('*')) if not p.is_dir()}}
    return {'missing':True}

class Installer:
    def __init__(self):
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        self.backup = Path('/var/backups/pro6000d-training') / stamp
        self.backup.mkdir(parents=True,mode=0o700)
        os.chmod(self.backup,0o700)
        self.manifest = {'version':1,'files':[],'services':{},'new_groups':[],'admin_memberships_added':[],'completed':False}
        registry=Path('/var/lib/pro6000d-training/enrolled-users.json')
        self.manifest['enrolled_users_before']=json.loads(registry.read_text()) if registry.exists() else {}
        for service in SERVICES:
            self.manifest['services'][service] = {key:run(['systemctl',verb,service],False).stdout.strip() for key,verb in [('active','is-active'),('enabled','is-enabled')]}
        self.save()

    def save(self):
        (self.backup/'manifest.json').write_text(json.dumps(self.manifest,indent=2)+'\n')

    def preserve(self, path):
        path = Path(path)
        if any(record['path'] == str(path) for record in self.manifest['files']): return
        exists = path.exists() or path.is_symlink()
        self.manifest['files'].append({'path':str(path),'existed':exists})
        if exists:
            dest = self.backup/'files'/str(path).lstrip('/')
            dest.parent.mkdir(parents=True,exist_ok=True)
            if path.is_symlink():
                os.symlink(os.readlink(path),dest)
            elif path.is_dir():
                shutil.copytree(path,dest,symlinks=True)
            else:
                shutil.copy2(path,dest)
        self.save()

    def write(self,path,content,mode=0o644):
        path = Path(path)
        self.preserve(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.install-',dir=path.parent)
        with os.fdopen(fd,'w') as stream: stream.write(content)
        os.chmod(name,mode); os.chown(name,0,0); os.replace(name,path)

    def copy(self,source,target,mode=0o644):
        self.write(target,Path(source).read_text(),mode)

    def tree(self,source,target):
        source,target=Path(source),Path(target)
        if any(p.is_symlink() for p in source.rglob('*')):
            raise RuntimeError('Plugin source may not contain symlinks')
        self.preserve(target)
        if target.exists(): shutil.rmtree(target)
        shutil.copytree(source,target)
        for path in [target]+list(target.rglob('*')):
            os.chown(path,0,0); os.chmod(path,0o755 if path.is_dir() else 0o644)

    def group(self,name):
        try: return grp.getgrnam(name)
        except KeyError:
            run(['groupadd','--system',name])
            self.manifest['new_groups'].append(name);self.save()
            return grp.getgrnam(name)

    def add_member(self,user,group):
        p=pwd.getpwnam(user)
        if grp.getgrnam(group).gr_gid not in os.getgrouplist(user,p.pw_gid):
            run(['usermod','-a','-G',group,user])
            self.manifest['admin_memberships_added'].append([user,group]);self.save()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--admin',default='u20')
    parser.add_argument('--skip-slurm',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0: raise SystemExit('Run with sudo')
    if socket.gethostname()!='u20-Z790-S-DDR4': raise SystemExit('This installer is restricted to the u20 test host')
    admin=pwd.getpwnam(args.admin)
    if admin.pw_uid<1000: raise SystemExit('Expected an existing human administrator')
    required=['systemctl','nvidia-smi','getfacl','setfacl','udevadm']
    if not args.skip_slurm: required+=['slurmd','slurmctld','squeue','iptables','ip6tables']
    missing=[name for name in required if not shutil.which(name)]
    if missing: raise SystemExit('Install prerequisites first: '+', '.join(missing))
    for name in ['backend/pro6000d-training-api.py','backend/pro6000d-training-client.py','backend/job_submit.lua','backend/config.example.json','cockpit-plugin/pro6000d-training/manifest.json']:
        if not (ROOT/name).is_file(): raise SystemExit('Missing source '+name)
    if not args.skip_slurm and Path('/etc/slurm-llnl/slurm.conf').exists():
        queue=run(['squeue','-h'],False)
        if queue.returncode==0 and queue.stdout.strip(): raise SystemExit('Active Slurm jobs exist; refusing scheduler reconfiguration')
        if queue.returncode!=0 and run(['systemctl','is-active','slurmctld'],False).returncode==0:
            raise SystemExit('Cannot establish empty queue; refusing to replace Slurm config')
    install=Installer()
    install.manifest['managed_services']=['pro6000d-training.service','pro6000d-gpu-acl.service']+([] if args.skip_slurm else ['pro6000d-loopback-firewall.service','slurmctld.service','slurmd.service'])
    install.save()
    print('Backup:',install.backup,flush=True)
    for group in ('pro6000d-training','pro6000d-admin','pro6000d-gpu'): install.group(group)
    install.add_member(args.admin,'pro6000d-training'); install.add_member(args.admin,'pro6000d-admin')
    for path in ('/var/lib/pro6000d-training','/var/lib/pro6000d-training/jobs'):
        Path(path).mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(path,0o700)
    cfg=json.loads((ROOT/'backend/config.example.json').read_text())
    cpus=max(1,os.cpu_count() or 1)
    total_mem=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:')))//1024
    usable_mem=max(1024,total_mem-16384)
    inventory=run(['nvidia-smi','--query-gpu=index','--format=csv,noheader']).stdout.strip().splitlines()
    gpu_count=len(inventory)
    if gpu_count!=1 or inventory!=['0']: raise RuntimeError('u20 test policy expects exactly GPU index 0')
    cfg.update({'socket_path':'/run/pro6000d-training/backend.sock','socket_group':'pro6000d-training','training_group':'pro6000d-training','admin_group':'pro6000d-admin','job_group':'pro6000d-gpu','partition':'gpu','admission_required':True,'admission_plugin':not args.skip_slurm,'admission_plugin_path':'/etc/slurm-llnl/job_submit.lua','slurm_conf':'/etc/slurm-llnl/slurm.conf','jobcomp_path':'/var/log/slurm/jobcomp.log','max_requested_gpu':gpu_count,'max_cpu':cpus,'max_memory_mb':usable_mem,'max_time_minutes':4320,'default_quota':{'gpu_limit':1,'concurrent_job_limit':1,'concurrent_gpu_limit':1,'active_job_limit':8},'temporary_authorizations':{'enabled':not args.skip_slurm}})
    if pwd.getpwnam(args.admin).pw_uid == 0:
        raise SystemExit('GPU monitoring requires a non-root administrator account')
    cfg.update({'capacity':{'gpu':gpu_count,'cpu':cpus,'memory_mb':usable_mem},'max_job_minutes':4320,'jobcomp_log':'/var/log/slurm/jobcomp.log','gpu_probe_user':args.admin})
    existing=Path('/etc/pro6000d-training/config.json')
    if existing.exists():
        previous=json.loads(existing.read_text())
        for key in ('default_quota','qos','user_roots','gpu_probe_user'):
            if key in previous: cfg[key]=previous[key]
    install.write(existing,json.dumps(cfg,indent=2,ensure_ascii=False)+'\n',0o640)
    install.copy(ROOT/'backend/pro6000d-training-api.py','/usr/local/lib/pro6000d-training/pro6000d-training-api.py',0o644)
    install.copy(ROOT/'backend/pro6000d-training-client.py','/usr/local/bin/pro6000d-training-client',0o755)
    install.copy(ROOT/'deploy/gpu-acl.py','/usr/local/libexec/pro6000d-gpu-acl',0o755)
    install.copy(ROOT/'deploy/enroll-user.py','/usr/local/sbin/pro6000d-enroll-user',0o755)
    install.copy(ROOT/'deploy/session-guard.py','/usr/local/libexec/pro6000d-session-guard',0o755)
    install.copy(ROOT/'deploy/backend.service','/etc/systemd/system/pro6000d-training.service')
    install.write('/etc/systemd/system/pro6000d-training.service.d/slurm.conf','[Service]\nEnvironment=SLURM_CONF=/etc/slurm-llnl/slurm.conf\n')
    install.copy(ROOT/'deploy/gpu-acl.service','/etc/systemd/system/pro6000d-gpu-acl.service')
    install.tree(ROOT/'cockpit-plugin/pro6000d-training','/usr/share/cockpit/pro6000d-training')
    devices=[str(p) for p in Path('/dev').glob('nvidia*') if p.is_char_device()]+[str(p) for p in Path('/dev/nvidia-caps').glob('*') if p.is_char_device()]
    if devices:
        acl=run(['getfacl','-p','--']+devices).stdout
        (install.backup/'gpu-acls.txt').write_text(acl)
    install.write('/etc/udev/rules.d/90-pro6000d-training-gpu.rules','ACTION=="add|change", KERNEL=="nvidia*", RUN+="/usr/local/libexec/pro6000d-gpu-acl $env{DEVNAME}"\n')
    # Runs after the distribution's optional pam_systemd registration. Only
    # training accounts fail closed if the resulting session is not in their slice.
    pam=Path('/etc/pam.d/common-session')
    text=pam.read_text()
    if PAM_MARKER not in text:
        install.write(pam,text+'\n'+PAM_MARKER+'\nsession required pam_exec.so quiet /usr/local/libexec/pro6000d-session-guard\n# END pro6000d training policy\n')
    for name in ('cron','atd'):
        pam=Path('/etc/pam.d')/name
        if pam.exists() and PAM_MARKER not in pam.read_text():
            install.write(pam,PAM_MARKER+'\naccount [success=1 default=ignore] pam_succeed_if.so quiet user ingroup pro6000d-admin\naccount requisite pam_succeed_if.so quiet user notingroup pro6000d-training\n# END pro6000d training policy\n'+pam.read_text())
    if not args.skip_slurm:
        slurm=grp.getgrnam('slurm'); slurm_user=pwd.getpwnam('slurm')
        for dirname in ('/var/lib/slurm','/var/lib/slurm/ctld','/var/log/slurm'):
            path=Path(dirname);path.mkdir(parents=True,exist_ok=True);os.chown(path,slurm_user.pw_uid,slurm.gr_gid)
            # Batch scripts run as the job owner and must traverse the shared
            # parent; controller state and logs remain private to Slurm.
            os.chmod(path,0o751 if dirname == '/var/lib/slurm' else 0o750)
        Path('/var/lib/slurm/slurmd').mkdir(parents=True,exist_ok=True)
        jobcomp=Path('/var/log/slurm/jobcomp.log');jobcomp.touch(exist_ok=True);os.chown(jobcomp,slurm_user.pw_uid,slurm.gr_gid);os.chmod(jobcomp,0o640)
        host=socket.gethostname()
        # Flat logical CPU topology is deliberate for this hybrid P/E-core CPU.
        # No cpuset/affinity enforcement is claimed: memory and GPU devices are
        # constrained, CPUs are scheduler reservations in this initial test host.
        config='''ClusterName=u20-training
SlurmctldHost={host}(127.0.0.1)
SlurmUser=slurm
SlurmdUser=root
SlurmctldPort=16817
SlurmdPort=16818
AuthType=auth/munge
CredType=cred/munge
UsePAM=0
StateSaveLocation=/var/lib/slurm/ctld
SlurmdSpoolDir=/var/lib/slurm/slurmd
SlurmctldPidFile=/run/slurmctld.pid
SlurmdPidFile=/run/slurmd.pid
SlurmctldLogFile=/var/log/slurm/slurmctld.log
SlurmdLogFile=/var/log/slurm/slurmd.log
SlurmctldTimeout=120
SlurmdTimeout=300
ReturnToService=2
SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_CPU_Memory
TaskPlugin=task/cgroup
ProctrackType=proctrack/cgroup
JobAcctGatherType=jobacct_gather/linux
JobAcctGatherFrequency=30
JobCompType=jobcomp/filetxt
JobCompLoc=/var/log/slurm/jobcomp.log
AccountingStorageType=accounting_storage/none
JobSubmitPlugins=lua
GresTypes=gpu
MinJobAge=86400
MaxJobCount=10000
SlurmdParameters=config_overrides
NodeName={host} NodeAddr=127.0.0.1 CPUs={cpus} Sockets=1 CoresPerSocket={cpus} ThreadsPerCore=1 RealMemory={memory} Gres=gpu:1 State=UNKNOWN
PartitionName=gpu Nodes={host} Default=YES MaxTime=3-00:00:00 State=UP OverSubscribe=NO DefMemPerCPU=2048
'''.format(host=host,cpus=cpus,memory=usable_mem)
        install.write('/etc/slurm-llnl/slurm.conf',config)
        install.write('/etc/slurm-llnl/gres.conf','NodeName=%s Name=gpu File=/dev/nvidia0\n' % host)
        install.write('/etc/slurm-llnl/cgroup.conf','CgroupAutomount=yes\nConstrainCores=no\nConstrainRAMSpace=yes\nConstrainSwapSpace=yes\nAllowedSwapSpace=0\nConstrainDevices=yes\nAllowedDevicesFile=/etc/slurm-llnl/cgroup_allowed_devices_file.conf\n')
        install.write('/etc/slurm-llnl/cgroup_allowed_devices_file.conf','/dev/null\n/dev/zero\n/dev/full\n/dev/random\n/dev/urandom\n/dev/tty\n/dev/ptmx\n/dev/pts/*\n/dev/nvidiactl\n/dev/nvidia-uvm\n/dev/nvidia-uvm-tools\n')
        install.copy(ROOT/'backend/job_submit.lua','/etc/slurm-llnl/job_submit.lua')
        install.copy(ROOT/'deploy/loopback-firewall.sh','/usr/local/libexec/pro6000d-loopback-firewall',0o755)
        install.copy(ROOT/'deploy/firewall.service','/etc/systemd/system/pro6000d-loopback-firewall.service')
        for service in ('slurmctld','slurmd'):
            install.write('/etc/systemd/system/%s.service.d/platform.conf'%service,'[Unit]\nRequires=pro6000d-loopback-firewall.service pro6000d-gpu-acl.service\nAfter=pro6000d-loopback-firewall.service pro6000d-gpu-acl.service\n')
    run(['systemctl','daemon-reload'])
    run(['udevadm','control','--reload-rules'])
    run(['/usr/local/libexec/pro6000d-gpu-acl'])
    run(['systemctl','enable','--now','pro6000d-gpu-acl.service'])
    if not args.skip_slurm:
        run(['systemctl','enable','--now','munge.service','pro6000d-loopback-firewall.service'])
        run(['systemctl','enable','slurmctld.service','slurmd.service'])
        run(['systemctl','restart','slurmctld.service','slurmd.service'])
    run(['systemctl','enable','pro6000d-training.service'])
    run(['systemctl','restart','pro6000d-training.service'])
    for _ in range(100):
        if Path('/run/pro6000d-training/backend.sock').exists(): break
        time.sleep(.1)
    else: raise RuntimeError('Backend socket did not appear')
    # Existing administrator Cockpit sessions predate the new group membership.
    run(['setfacl','-m','u:%d:rw'%admin.pw_uid,'/run/pro6000d-training/backend.sock'])
    for record in install.manifest['files']:
        record['installed_fingerprint']=fingerprint(record['path'])
    install.manifest['completed']=True;install.save()
    print('Installed. Cockpit: https://127.0.0.1:19093/pro6000d-training/')
    print('Source:',ROOT)
    print('Rollback: sudo python3 %s/deploy/rollback-u20.py %s' % (ROOT,install.backup))

if __name__=='__main__':
    main()
