#!/usr/bin/env python3
"""Rehearse quota admission in a separate fake Slurm cluster on u20.

The fake GRES is a new null character device, not an NVIDIA device. The two
short-lived daemons use unique ports, config, spool, PID files and ledger.
No production Slurm command or GPU device is touched. Evidence is retained.
"""
import datetime as dt
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

INPUT = Path('/opt/time/selectable-input-f417af5')
HELPER = INPUT/'rehearsal.py'
EXPECTED_HELPER = 'df2f15b1fd6ffd9af08966a17831cd04120e0bb235ab74eb7d4a1704ac446a8b'
EXPECTED = {'fake-slurmd':'271d3fe8af8d3f4875f8d15cae3250a3f971e4702f53a8fcb536d0cd84a65598',
            'fake-slurmstepd':'3756f159a501023ecb6f6689dcd99e3a71a20d6fb6cf60322245c58b3b6f9675'}
CONTROLLER = Path('/opt/time/slurm19-f417af53729e/sbin/slurmctld')
CONTROLLER_HASH = '47e8210f486f1889a1cf88613ab4a0b5ecc53a003ebb1bc9ab330809ac9db5eb'
PLUGIN = '/opt/time/slurm19-f417af53729e/lib/slurm'


def checked(path, digest):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        raise RuntimeError('untrusted executable: '+str(path))
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RuntimeError('executable digest mismatch: '+str(path))


def main():
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise RuntimeError('requires root on u20')
    checked(HELPER, EXPECTED_HELPER)
    checked(CONTROLLER, CONTROLLER_HASH)
    for name, digest in EXPECTED.items(): checked(INPUT/name, digest)
    spec = importlib.util.spec_from_file_location('time_isolated_helper', HELPER)
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    slurm = pwd.getpwnam('slurm'); trainer = pwd.getpwnam('timzhang')
    base = Path('/var/lib')/('time-v2-fakegpu-'+uuid.uuid4().hex)
    base.mkdir(mode=0o755)
    state=base/'state'; state.mkdir(mode=0o700); os.chown(state,slurm.pw_uid,slurm.pw_gid)
    spool=base/'spool'; spool.mkdir(mode=0o700)
    public=base/'public'; public.mkdir(mode=0o700); os.chown(public,trainer.pw_uid,trainer.pw_gid)
    fake=base/'fakegpu'; os.mknod(fake, stat.S_IFCHR|0o666, os.makedev(1,3)); os.chmod(fake,0o666)
    now=int(time.time()); tz=dt.timezone(dt.timedelta(hours=8)); local=dt.datetime.fromtimestamp(now,tz)
    month_start=int(local.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp())
    start=now-4600; end=start+4500
    if start < month_start or end >= now: raise RuntimeError('not enough time in current month for synthetic fixture')
    uid=trainer.pw_uid
    events=['CUTOVER2_RESET_USAGE',
            f'PREPARE2|900001|{uid}|1|4|{start}|0|0|0123456789abcdef0123456789abcdef',
            'COMMIT|900001|1', f'ADVANCE2|{end}|0', f'RELEASE2|900001|1|{end}|0']
    wal=bytearray(b'U20MQ1|HEADER|1\n'); witnesses=[b'U20MQA1|0|'+hashlib.sha256(wal).hexdigest().encode()+b'\n']
    for seq,event in enumerate(events,1):
        wal.extend(f'U20MQ1|{seq}|{event}\n'.encode())
        witnesses.append(f'U20MQA1|{seq}|{hashlib.sha256(wal).hexdigest()}\n'.encode())
    digest=hashlib.sha256(wal).hexdigest().encode()
    helper.write_new(state/'monthly-quota.wal', bytes(wal),0o600,slurm.pw_uid,slurm.pw_gid)
    helper.write_new(state/'monthly-quota.checkpoint',b'U20MQ1|CHECKPOINT|5|'+digest+b'\n',0o600,slurm.pw_uid,slurm.pw_gid)
    witness=base/'witness.log';helper.write_new(witness,b''.join(witnesses),0o600)
    ctl_port,node_port=helper.free_port(),helper.free_port()
    if ctl_port==node_port: raise RuntimeError('isolated ports collide')
    cluster='time-fake-'+uuid.uuid4().hex[:8]
    config=base/'slurm.conf'
    text=f'''ClusterName={cluster}
SlurmctldHost={os.uname().nodename}(127.0.0.1)
SlurmUser=slurm
SlurmctldPort={ctl_port}
SlurmdPort={node_port}
StateSaveLocation={state}
SlurmctldPidFile={state}/controller.pid
SlurmdPidFile={spool}/slurmd.pid
SlurmdSpoolDir={spool}
PluginDir={PLUGIN}
AuthType=auth/munge
CryptoType=crypto/munge
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
SchedulerType=sched/backfill
ProctrackType=proctrack/linuxproc
TaskPlugin=task/none
MpiDefault=none
AccountingStorageType=accounting_storage/none
JobCompType=jobcomp/none
SlurmctldParameters=MonthlyGpuQuota
SlurmctldDebug=info
SlurmdDebug=info
SlurmctldTimeout=30
SlurmdTimeout=30
MessageTimeout=2
GresTypes=gpu
NodeName=time-fake-node NodeAddr=127.0.0.1 NodeHostname={os.uname().nodename} CPUs=32 Sockets=1 CoresPerSocket=32 ThreadsPerCore=1 RealMemory=112186 Gres=gpu:1 State=UNKNOWN
PartitionName=isolated Nodes=time-fake-node Default=YES MaxTime=00:05:00 State=UP
'''
    helper.write_new(config,text.encode(),0o644)
    helper.write_new(base/'gres.conf',f'NodeName=time-fake-node Name=gpu File={fake}\n'.encode(),0o644)
    env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','SLURM_CONF':str(config)}
    log=(base/'daemons.log').open('ab',buffering=0)
    children=[]; fd=-1
    print('isolated fake GPU evidence:',base,flush=True)
    def start_daemon(argv, environment, pass_fds=()):
        p=subprocess.Popen(['/usr/bin/timeout','--kill-after=5s','90s']+argv,cwd='/',env=environment,
                           stdin=subprocess.DEVNULL,stdout=log,stderr=log,pass_fds=pass_fds,start_new_session=True)
        children.append(p);return p
    def command(argv):
        return subprocess.run(argv,env=env,cwd='/tmp',text=True,capture_output=True,timeout=8,check=True).stdout.strip()
    try:
        fd=os.open(witness,os.O_RDWR|os.O_APPEND|os.O_NOFOLLOW);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);os.set_inheritable(fd,True)
        cenv={**env,'TIME_QUOTA_WITNESS_FD':str(fd)}
        controller=start_daemon([str(CONTROLLER),'-D','-f',str(config)],cenv,(fd,))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if controller.poll() is not None: raise RuntimeError('isolated controller exited')
            try:
                fields=helper.rpc(state/'time-quota.sock',slurm.pw_uid,f'SNAP2 {uid}').split()
                if len(fields)==18 and fields[:2]==['OK','2'] and int(fields[5])==18000: break
            except (OSError,RuntimeError,ValueError): pass
            time.sleep(.2)
        else: raise RuntimeError('isolated controller snapshot not ready or fixture wrong')
        node=start_daemon([str(INPUT/'fake-slurmd'),'-D','-N','time-fake-node','-f',str(config),'-d',str(INPUT/'fake-slurmstepd')],env)
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if node.poll() is not None: raise RuntimeError('isolated slurmd exited')
            try:
                data=command(['/usr/bin/scontrol','show','node','time-fake-node','-o'])
                if 'State=IDLE' in data and 'Gres=gpu:1' in data: break
            except (OSError,subprocess.SubprocessError): pass
            time.sleep(.3)
        else: raise RuntimeError('isolated fake node did not become idle')
        def submit(tier,hexid):
            out=command(['/usr/bin/sbatch','--parsable','--uid='+str(uid),'--gid='+str(trainer.pw_gid),
                         '--partition=isolated','--comment=pro6000d-job:'+hexid+':tier='+tier,
                         '--gres=gpu:1','--cpus-per-task=1','--mem=128M','--time=00:01:00',
                         '--chdir='+str(public),'--output='+str(public/(tier+'-%j.out')),'--wrap=sleep 12'])
            job=out.split(';',1)[0]
            if not job.isdigit(): raise RuntimeError('invalid isolated job id')
            return job
        denied=submit('debug','11111111111111111111111111111111')
        allowed=submit('important','22222222222222222222222222222222')
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            bad=command(['/usr/bin/scontrol','show','job',denied,'-o'])
            good=command(['/usr/bin/scontrol','show','job',allowed,'-o'])
            if 'JobState=RUNNING' in good: break
            if any('JobState='+s in good for s in ('FAILED','CANCELLED','TIMEOUT','COMPLETED')):
                raise RuntimeError('allowed job did not reach running: '+good)
            time.sleep(.3)
        else: raise RuntimeError('allowed job failed to start')
        if 'JobState=PENDING' not in bad or not re.search(r'NodeList=\(null\)',bad):
            raise RuntimeError('exhausted job was allocated or not pending: '+bad)
        if 'AllocTRES=' in bad and not re.search(r'AllocTRES=(?:\(null\)|N/A|cpu=0(?:,|\s))',bad):
            raise RuntimeError('exhausted job has allocated resources: '+bad)
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            good=command(['/usr/bin/scontrol','show','job',allowed,'-o'])
            if 'JobState=COMPLETED' in good: break
            if any('JobState='+s in good for s in ('FAILED','CANCELLED','TIMEOUT')):
                raise RuntimeError('isolated allowed job did not complete: '+good)
            time.sleep(.3)
        else: raise RuntimeError('isolated allowed job did not finish')
        print('PASS: exhausted debug stayed pending with no node; important ran and completed on fake GPU',flush=True)
    finally:
        for p in reversed(children):
            if p.poll() is None:
                try: os.killpg(p.pid,signal.SIGTERM)
                except ProcessLookupError: pass
                try: p.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    try: os.killpg(p.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                    p.wait(timeout=5)
        if fd>=0: os.close(fd)
        log.close()


if __name__=='__main__':
    try: main()
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as exc:
        print('Isolated fake GPU rehearsal failed; preserve evidence: '+str(exc),file=sys.stderr)
        sys.exit(1)
