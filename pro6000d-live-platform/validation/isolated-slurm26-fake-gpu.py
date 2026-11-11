#!/usr/bin/env python3
"""Run quota gate and settlement against a GPU-free Colima VM only."""
import datetime as dt
import fcntl
import hashlib
import os
from pathlib import Path
import pwd
import re
import signal
import shutil
import subprocess
import sys
import time
import uuid

from importlib.util import spec_from_file_location, module_from_spec
spec=spec_from_file_location('controller_fixture', Path(__file__).with_name('isolated-slurm26-controller.py'))
helper=module_from_spec(spec); spec.loader.exec_module(helper)
P=helper.PREFIX

def main():
    helper.validate_prefix()
    if (os.geteuid()!=0 or os.uname().nodename!='colima'
            or subprocess.check_output(['systemd-detect-virt'],text=True).strip()!='apple'
            or any(Path('/dev').glob('nvidia*'))):
        raise RuntimeError('requires GPU-free dedicated Colima VM')
    disabled=os.environ.get('TIME_TEST_QUOTA_DISABLED') == '1'
    cpu_only=os.environ.get('TIME_TEST_CPU_ONLY') == '1'
    cgroup=os.environ.get('TIME_TEST_CGROUP') == '1'
    fail_epilog=os.environ.get('TIME_TEST_FAIL_EPILOG') == '1'
    restart_running=os.environ.get('TIME_TEST_RESTART_RUNNING') == '1'
    old_node=os.environ.get('TIME_TEST_OLD_NODE') == '1'
    saved_completing=os.environ.get('TIME_TEST_SAVED_COMPLETING') == '1'
    reconfig_completing=os.environ.get('TIME_TEST_RECONFIG_COMPLETING') == '1'
    restart_completing=os.environ.get('TIME_TEST_RESTART_COMPLETING') == '1' or saved_completing or reconfig_completing
    full_tier=os.environ.get('TIME_TEST_FULL_TIER') == '1'
    active_rollover=os.environ.get('TIME_TEST_ACTIVE_ROLLOVER') == '1'
    priority_test=os.environ.get('TIME_TEST_PRIORITY') == '1'
    attest_test=os.environ.get('TIME_TEST_ATTESTATION') == '1'
    cancel_pending=os.environ.get('TIME_TEST_CANCEL_PENDING') == '1'
    purge_pending=os.environ.get('TIME_TEST_PURGE_PENDING') == '1'
    stale_cancel=os.environ.get('TIME_TEST_STALE_CANCEL') == '1'
    missing_script=os.environ.get('TIME_TEST_MISSING_SCRIPT') == '1'
    stale_release=os.environ.get('TIME_TEST_STALE_RELEASE') == '1'
    require_recovery=os.environ.get('TIME_TEST_REQUIRE_AUTO_RECOVERY') == '1'
    ending_metadata=os.environ.get('TIME_TEST_ENDING_METADATA') == '1'
    running_cancel=os.environ.get('TIME_TEST_RUNNING_CANCEL') == '1'
    running_failure=os.environ.get('TIME_TEST_RUNNING_FAILURE') == '1'
    proof_failure=os.environ.get('TIME_TEST_PROOF_FAILURE') == '1'
    selector_silent=os.environ.get('TIME_TEST_SELECTOR_SILENT') == '1'
    selector_partial=os.environ.get('TIME_TEST_SELECTOR_PARTIAL') == '1' or selector_silent
    selector_failure=os.environ.get('TIME_TEST_SELECTOR_ROLLBACK') == '1' or selector_partial
    rollback_test=os.environ.get('TIME_TEST_COMMIT_ROLLBACK') == '1' or selector_failure
    live_gpu=os.environ.get('TIME_TEST_RECOVERY_LIVE_GPU') == '1' or rollback_test
    gpu_identity_change=os.environ.get('TIME_TEST_GPU_ID_CHANGE') == '1'
    nodepid_exit=os.environ.get('TIME_TEST_NODE_PIDFD_EXIT') == '1'
    node_restart=os.environ.get('TIME_TEST_NODE_RESTART') == '1'
    if live_gpu and not (restart_completing or rollback_test): raise ValueError('live GPU recovery test requires restart scenario')
    if (purge_pending or stale_cancel or missing_script) and not cancel_pending: raise ValueError('purge test requires pending cancellation')
    sched_mode=os.environ.get('TIME_TEST_SCHEDULER','combined')
    if sched_mode not in ('combined','builtin','backfill'): raise ValueError('unknown fixture scheduler')
    if (fail_epilog or old_node or restart_completing) and (disabled or cpu_only): raise ValueError('epilog failure test requires the monthly GPU gate')
    trainer=pwd.getpwnam('nobody')
    base=Path('/tmp')/('time26-gpu-probe-'+uuid.uuid4().hex); base.mkdir(mode=0o755)
    inventory=helper.fake_gpu_inventory(base,2 if live_gpu else 1)
    shifted_env={}
    if active_rollover:
        local=dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
        next_month=(local.replace(day=28,hour=0,minute=0,second=0,microsecond=0)+dt.timedelta(days=4)).replace(day=1)
        offset=int(next_month.timestamp()-time.time())-101*3600-30
        offset_file=base/'clock-offset'; offset_file.write_text(str(offset))
        shim=base/'clock-shift.so'
        subprocess.run(['cc','-shared','-fPIC','-O2',str(Path(__file__).with_name('isolated-clock-shift.c')),'-ldl','-o',str(shim)],check=True,timeout=20)
        shifted_env={'LD_PRELOAD':str(shim),'TIME_FAKE_REALTIME_OFFSET_FILE':str(offset_file)}
    proof_dir=base/'proof';proof_dir.mkdir(mode=0o700)
    proof=proof_dir/'node-proof.wal';proof.write_bytes(b'P6NP1|HEADER|1\n');proof.chmod(0o600)
    state=base/'state'; state.mkdir(mode=0o700)
    spool=base/'spool'; spool.mkdir(mode=0o700)
    public=base/'public'; public.mkdir(mode=0o777); public.chmod(0o777)
    digest=hashlib.sha256(helper.HEADER).hexdigest().encode()
    for path,data in ((state/'monthly-quota.wal',helper.HEADER),
                      (state/'monthly-quota.checkpoint',b'U20MQ1|CHECKPOINT|0|'+digest+b'\n'),
                      (base/'witness',b'U20MQA1|0|'+digest+b'\n')):
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
    ctl,node,dbd=helper.port(),helper.port(),helper.port()
    if len({ctl,node,dbd}) != 3 or any(x in (6817,6818,6819) for x in (ctl,node,dbd)): raise RuntimeError('unsafe ports')
    cluster='time26_'+uuid.uuid4().hex[:8]
    database='time26_'+uuid.uuid4().hex[:12]
    subprocess.run(['/usr/bin/mariadb','-uroot','-e','CREATE DATABASE `'+database+'`'],check=True,timeout=10)
    epilog=base/'failing-epilog'
    if fail_epilog or restart_completing:
        epilog.write_text('#!/bin/sh\n'+('touch '+str(base/'epilog-started')+'\nsleep 15\nexit 0\n' if restart_completing else 'exit 1\n')); epilog.chmod(0o755)
    submit_plugin=P/'lib/slurm/job_submit_pro6000d.so'
    if attest_test:
        subprocess.run(['cc','-shared','-fPIC','-I'+str(P/'include'),
            str(Path(__file__).resolve().parents[1]/'deploy/job_submit_pro6000d.c'),'-o',str(submit_plugin)],check=True,timeout=20)
    conf=base/'slurm.conf'
    conf.write_text(f'''ClusterName={cluster}
SlurmctldHost=colima(127.0.0.1)
SlurmUser=root
SlurmctldPort={ctl}
SlurmdPort={node}
StateSaveLocation={state}
SlurmctldPidFile={state}/controller.pid
SlurmdPidFile={spool}/slurmd.pid
SlurmdSpoolDir={spool}
PluginDir={P}/lib/slurm
AuthType=auth/munge
CredType=cred/munge
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
SchedulerType=sched/{'builtin' if sched_mode == 'builtin' else 'backfill'}
{'JobSubmitPlugins=pro6000d' if attest_test else ''}
{'SchedulerParameters=defer,bf_interval=1' if sched_mode == 'backfill' else ''}
ProctrackType={'proctrack/cgroup' if cgroup else 'proctrack/linuxproc'}
TaskPlugin={'task/cgroup,task/affinity' if cgroup else 'task/none'}
MpiDefault=none
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageHost=127.0.0.1
AccountingStoragePort={dbd}
AccountingStorageTRES=gres/gpu
JobCompType=jobcomp/none
{'SlurmctldParameters=MonthlyGpuQuota' if not disabled else ''}
{'Epilog='+str(epilog) if fail_epilog or restart_completing else ''}
SlurmdParameters=config_overrides
{'MinJobAge=1' if purge_pending or live_gpu else ''}
SlurmctldTimeout=30
SlurmdTimeout=30
MessageTimeout=2
GresTypes=gpu
NodeName=colima NodeAddr=127.0.0.1 NodeHostname=colima CPUs=2 Sockets=1 CoresPerSocket=2 ThreadsPerCore=1 RealMemory=1024 Gres=gpu:{2 if live_gpu else 1} State=UNKNOWN
PartitionName=isolated Nodes=colima Default=YES MaxTime=06:00:00 State=UP
''')
    dbconf=base/'slurmdbd.conf'
    dbconf.write_text(f'''AuthType=auth/munge
DbdHost=colima
DbdAddr=127.0.0.1
DbdPort={dbd}
SlurmUser=root
PluginDir={P}/lib/slurm
PidFile={base}/dbd.pid
StorageType=accounting_storage/mysql
StorageHost=localhost
StorageUser=root
StorageLoc={database}
''')
    dbconf.chmod(0o600)
    gpu_topology=os.environ.get('TIME_TEST_GPU_TOPOLOGY') == '1'
    (base/'gres.conf').write_text('NodeName=colima Name=gpu File=/dev/null'+(' Cores=0-1' if gpu_topology else '')+'\n'+('NodeName=colima Name=gpu File=/dev/zero'+(' Cores=0-1' if gpu_topology else '')+'\n' if live_gpu else ''))
    (base/'cgroup.conf').write_text('CgroupPlugin=cgroup/v2\nEnableControllers=yes\nConstrainDevices=yes\n' if cgroup else 'CgroupPlugin=disabled\n')
    env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','SLURM_CONF':str(conf), 'LD_LIBRARY_PATH':str(P/'lib/slurm'),'TIME_QUOTA_NODE_PROOF_FILE':str(proof),'TIME_QUOTA_NODE_READY_FILE':str(proof_dir/'node-ready'),'TIME26_TEST_ABORT_COMMIT_FILE':str(base/'abort-commit') if rollback_test and not selector_failure else '', 'TIME26_TEST_SELECTOR_FAIL_FILE':str(base/'abort-commit') if selector_failure else '', **({'TIME26_TEST_SELECTOR_PARTIAL':'1'} if selector_partial and not selector_silent else {}), **({'TIME26_TEST_SELECTOR_SILENT':'1'} if selector_silent else {}), 'TIME26_TEST_GPU_INFO_DIR':str(inventory)}
    logfile=(base/'daemons.log').open('ab',buffering=0); processes=[]; fd=-1;proof_fd=-1
    sentinel=subprocess.Popen(['/bin/sleep','300']) if nodepid_exit else None
    nodepid_fd=os.pidfd_open(sentinel.pid if sentinel else os.getpid()); os.set_inheritable(nodepid_fd,True)
    print('isolated fake GPU evidence:',base,flush=True)
    def spawn(argv,env,pass_fds=()):
        p=subprocess.Popen(argv,cwd='/',env=env,stdin=subprocess.DEVNULL,stdout=logfile,stderr=logfile,pass_fds=pass_fds,start_new_session=True)
        processes.append(p); return p
    def command(*argv):
        return subprocess.run(list(argv),env=env,cwd=str(public),text=True,capture_output=True,timeout=8,check=True).stdout.strip()
    def wait_for(predicate,seconds,what):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            for p in processes:
                if p.poll() is not None: raise RuntimeError('daemon exited: '+str(p.returncode))
            try:
                answer=predicate()
                if answer: return answer
            except (OSError,RuntimeError,subprocess.SubprocessError): pass
            time.sleep(.25)
        raise RuntimeError('timed out: '+what)
    try:
        spawn([str(P/'sbin/slurmdbd'),'-D'],env)
        def dbd_ready():
            with __import__('socket').create_connection(('127.0.0.1',dbd),timeout=1): return True
        wait_for(dbd_ready,30,'isolated slurmdbd')
        command(str(P/'bin/sacctmgr'),'-i','add','cluster',cluster)
        fd=os.open(base/'witness',os.O_RDWR|os.O_APPEND|os.O_NOFOLLOW); fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); os.set_inheritable(fd,True)
        proof_fd=os.open(proof,os.O_RDONLY|os.O_NOFOLLOW);os.set_inheritable(proof_fd,True)
        controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
        endpoint=state/'time-quota.sock'
        if disabled:
            wait_for(lambda: 'UP' in command(str(P/'bin/scontrol'),'ping'),30,'controller')
        else:
            wait_for(lambda: helper.rpc(endpoint,'SNAP 65534').startswith('OK '),30,'controller')
            if helper.rpc(endpoint,'CUTOVER2_RESET_USAGE 0')!='OK': raise RuntimeError('cutover failed')
        node_bin=Path('/tmp/time-slurm26-isolated/install/sbin/slurmd') if old_node else P/'sbin/slurmd'
        if old_node and not node_bin.is_file(): raise RuntimeError('baseline node daemon is missing')
        node_process=spawn([str(node_bin),'-D','-N','colima','-f',str(conf)],{**env,**shifted_env})
        wait_for(lambda: 'State=IDLE' in command(str(P/'bin/scontrol'),'show','node','colima','-o'),30,'fake node')
        if os.environ.get('TIME_TEST_PARTITION_FENCE') == '1':
            command(str(P/'bin/scontrol'),'update','PartitionName=isolated','State=INACTIVE')
            text=conf.read_text().replace('PartitionName=isolated Nodes=colima Default=YES MaxTime=06:00:00 State=UP',
                                         'PartitionName=isolated Nodes=colima Default=YES MaxTime=06:00:00 State=INACTIVE')
            conf.write_text(text)
            os.killpg(controller.pid,signal.SIGTERM); controller.wait(timeout=15); processes.remove(controller)
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            wait_for(lambda: 'UP' in command(str(P/'bin/scontrol'),'ping'),30,'controller after fenced restart')
            if 'State=INACTIVE' not in command(str(P/'bin/scontrol'),'show','partition','isolated','-o'):
                raise RuntimeError('partition fence did not survive controller restart')
            result=subprocess.run([str(P/'bin/sbatch'),'--parsable','--uid='+str(trainer.pw_uid),
                '--gid='+str(trainer.pw_gid),'--partition=isolated','--gres=gpu:1',
                '--time=00:01:00','--chdir='+str(public),'--wrap=exec sleep 1'],env=env,
                cwd=str(public),text=True,capture_output=True,timeout=10)
            if result.returncode == 0: raise RuntimeError('inactive partition accepted a new submission')
            if command(str(P/'bin/squeue'),'--all','-h','-o','%i %T'):
                raise RuntimeError('inactive-partition submission left a queued job')
            wait_for(lambda:'State=IDLE' in command(str(P/'bin/scontrol'),'show','node','colima','-o'),30,'node registration after fenced controller restart')
            conf.write_text(conf.read_text().replace('PartitionName=isolated Nodes=colima Default=YES MaxTime=06:00:00 State=INACTIVE',
                                                     'PartitionName=isolated Nodes=colima Default=YES MaxTime=06:00:00 State=UP'))
            os.killpg(controller.pid,signal.SIGTERM); controller.wait(timeout=15); processes.remove(controller)
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            wait_for(lambda:helper.rpc(endpoint,'READY1 0').startswith('OK '),30,'controller after final UP configuration')
            if 'State=UP' not in command(str(P/'bin/scontrol'),'show','partition','isolated','-o'):
                raise RuntimeError('final partition did not reopen')
            marker='6'*32
            if not helper.rpc(endpoint,f'RESERVE1 {trainer.pw_uid} {marker} 1 1 1').startswith('OK '):
                raise RuntimeError('post-cutover reservation failed')
            result=command(str(P/'bin/sbatch'),'--parsable','--uid='+str(trainer.pw_uid),'--gid='+str(trainer.pw_gid),
                '--partition=isolated','--nodes=1-1','--comment=pro6000d-job:'+marker+':tier=important',
                '--gres=gpu:1','--time=00:01:00','--chdir='+str(public),'--wrap=exec sleep 3')
            jobid=result.split(';',1)[0]
            if not jobid.isdigit() or helper.rpc(endpoint,f'BIND1 {trainer.pw_uid} {marker} {jobid}')!='OK':
                raise RuntimeError('post-cutover submission binding failed')
            wait_for(lambda:helper.rpc(endpoint,f'RESERVE2_GET {trainer.pw_uid} {marker}').split()[-2]=='5',40,'post-cutover allocation settlement')
            print('PASS: inactive partition survives restart; final UP restart allocates and settles',flush=True)
            return
        if node_restart:
            # Bind this isolated controller to the actual slurmd process.
            os.killpg(controller.pid,signal.SIGTERM); controller.wait(timeout=15); processes.remove(controller)
            os.close(nodepid_fd); nodepid_fd=os.pidfd_open(node_process.pid); os.set_inheritable(nodepid_fd,True)
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            wait_for(lambda: helper.rpc(endpoint,'READY1 0').startswith('OK '),30,'actual node process binding')
            wait_for(lambda: 'State=IDLE' in command(str(P/'bin/scontrol'),'show','node','colima','-o'),30,'node after process binding')
        if attest_test:
            import json
            sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
            from activation_attestation import verify_release
            paths={'controller':P/'sbin/slurmctld','node':P/'sbin/slurmd','submit':submit_plugin,
                   'scheduler':P/'lib/slurm/sched_backfill.so','select':P/'lib/slurm/select_cons_tres.so',
                   'cgroup':P/'lib/slurm/cgroup_v2.so','slurm_conf':conf,'cgroup_conf':base/'cgroup.conf','gres_conf':base/'gres.conf','node_proof':proof}
            manifest=base/'manifest.json'
            manifest.write_text(json.dumps({'version':1,'files':{k:({'path':str(v)} if k=='node_proof' else {'path':str(v),'sha256':hashlib.sha256(v.read_bytes()).hexdigest()}) for k,v in paths.items()},
                    'pidfiles':{'controller':str(state/'controller.pid'),'node':str(spool/'slurmd.pid')}}))
            if not verify_release(str(manifest),0,owner=0,check_ancestors=False):
                raise RuntimeError('paired running artifacts did not pass attestation')
            api_spec=spec_from_file_location('live_fixture_api',Path(__file__).resolve().parents[1]/'backend/pro6000d-training-api.py')
            api=module_from_spec(api_spec);api_spec.loader.exec_module(api)
            api.verify_release=lambda path,uid,**kwargs: verify_release(path,uid,owner=0,check_ancestors=False,**kwargs)
            service=api.Service.__new__(api.Service)
            service.config={'monthly_controller':{'activation_enabled':True,'read_enabled':True,
                'controller_user':'root','socket_path':str(endpoint),'release_manifest':str(manifest)}}
            view=service.monthly_controller_snapshot(65534)
            if not view.get('enforcement_ready') or not view.get('selectable_tiers_ready'):
                raise RuntimeError('real controller did not activate API view')
            bad_manifest=json.loads(manifest.read_text());bad_manifest['files']['node']['sha256']='0'*64
            manifest.write_text(json.dumps(bad_manifest))
            if service.monthly_controller_snapshot(65534).get('enforcement_ready'):
                raise RuntimeError('API advertised readiness with mismatched node')
            bad_manifest['files']['node']['sha256']=hashlib.sha256(paths['node'].read_bytes()).hexdigest()
            conf.write_text(conf.read_text()+'\n# changed after daemon startup\n')
            bad_manifest['files']['slurm_conf']['sha256']=hashlib.sha256(conf.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(bad_manifest))
            if verify_release(str(manifest),0,owner=0,check_ancestors=False):
                raise RuntimeError('post-start configuration change passed attestation')
            print('PASS: running artifacts and controller attest API readiness; mismatches fail closed',flush=True)
        if restart_completing:
            # Seed Slurm's own state files before simulating a later hard crash.
            os.killpg(controller.pid,signal.SIGTERM); controller.wait(timeout=15); processes.remove(controller)
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            wait_for(lambda: helper.rpc(endpoint,'SNAP2 65534').startswith('OK 2 '),30,'seeded controller restart')
            wait_for(lambda: 'State=IDLE' in command(str(P/'bin/scontrol'),'show','node','colima','-o'),30,'node after restart')
        uid=trainer.pw_uid; full='1'*32; denied='2'*32; allowed='3'*32
        def submit(marker,tier):
            result=command(str(P/'bin/sbatch'),'--parsable','--uid='+str(uid),'--gid='+str(trainer.pw_gid),
                           '--partition=isolated','--comment=pro6000d-job:'+marker+':tier='+tier,
                           '--nodes=1-1',*([] if cpu_only else ['--gres=gpu:1']),'--cpus-per-task=1','--mem=128M',('--time=05:00:00' if full_tier else '--time=00:01:00'),
                           '--chdir='+str(public),'--output='+str(public/(tier+'-%j.out')),'--wrap=echo $$ > '+str(public/('pid-'+marker))+'; '+
                           ('sleep 6; exit 7' if running_failure and marker==allowed else 'exec sleep '+
                            ('30' if live_gpu and marker=='8'*32 else '20' if (live_gpu or gpu_identity_change or nodepid_exit or node_restart) and marker==allowed else '50' if (stale_cancel or missing_script or stale_release) and marker==allowed else '40' if purge_pending and marker==allowed else '20' if restart_running or ((priority_test or cancel_pending or running_cancel) and marker==allowed) else '6')))
            job=result.split(';',1)[0]
            if not job.isdigit(): raise RuntimeError('invalid job id')
            return job
        if disabled or cpu_only:
            jobid=submit(allowed,'normal')
            def disabled_job(): return command(str(P/'bin/scontrol'),'show','job',jobid,'-o')
            try: wait_for(lambda: 'JobState=RUNNING' in disabled_job(),30,'disabled gate allows job')
            except RuntimeError as exc: raise RuntimeError(str(exc)+'; job='+disabled_job()) from exc
            job_cgroup=None
            if cgroup:
                pidfile=public/('pid-'+allowed)
                wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(),15,'job PID')
                pid=int(pidfile.read_text().strip())
                membership=Path(f'/proc/{pid}/cgroup').read_text().strip()
                part=membership.split('::',1)[1]
                prefix='/system.slice/slurmstepd.scope/'
                if not part.startswith(prefix): raise RuntimeError('unexpected cgroup path '+membership)
                sluid=part[len(prefix):].split('/',1)[0]
                job_cgroup=Path('/sys/fs/cgroup')/prefix.lstrip('/')/sluid
                before=(job_cgroup/'cgroup.events').read_text().strip()
                if 'populated 1' not in before: raise RuntimeError('running job cgroup not populated: '+before)
                print('running job cgroup:',job_cgroup,before.replace('\n',','),flush=True)
            wait_for(lambda: 'JobState=COMPLETED' in disabled_job(),30,'disabled gate job completed')
            if job_cgroup is not None:
                after=(job_cgroup/'cgroup.events').read_text().strip() if job_cgroup.exists() else 'removed'
                print('finished job cgroup:',after.replace('\n',','),flush=True)
            print('PASS: CPU-only scheduling' if cpu_only else 'PASS: disabled monthly gate leaves GPU scheduling unchanged',flush=True)
            return
        if full_tier:
            if not helper.rpc(endpoint,f'RESERVE1 {uid} {allowed} 0 1 300').startswith('OK '): raise RuntimeError('full-tier hold failed')
            good=submit(allowed,'debug')
            if helper.rpc(endpoint,f'BIND1 {uid} {allowed} {good}')!='OK': raise RuntimeError('full-tier binding failed')
            def full_job(): return command(str(P/'bin/scontrol'),'show','job',good,'-o')
            wait_for(lambda: 'JobState=RUNNING' in full_job(),30,'full-tier reservation starts')
            wait_for(lambda: 'JobState=COMPLETED' in full_job(),30,'full-tier reservation settles')
            status=helper.rpc(endpoint,f'RESERVE2_GET {uid} {allowed}').split()
            if len(status)!=12 or status[-2]!='5': raise RuntimeError('full-tier reservation not released')
            print('PASS: a reservation exactly equal to selected-tier quota starts and settles',flush=True)
            return
        if not helper.rpc(endpoint,f'RESERVE1 {uid} {full} 0 1 300').startswith('OK '): raise RuntimeError('full debug hold failed')
        if helper.rpc(endpoint,f'RESERVE1 {uid} {denied} 0 1 1').startswith('OK '): raise RuntimeError('exhausted tier allowed a hold')
        bad=submit(denied,'debug')
        if not helper.rpc(endpoint,f'RESERVE1 {uid} {allowed} 1 1 1').startswith('OK '): raise RuntimeError('important hold failed')
        good=submit(allowed,'important')
        binding=helper.rpc(endpoint,f'BIND1 {uid} {allowed} {good}')
        if binding!='OK':
            detail=command(str(P/'bin/scontrol'),'show','job',good,'-o')
            status=helper.rpc(endpoint,f'RESERVE2_GET {uid} {allowed}')
            raise RuntimeError('binding failed: '+binding+'; '+status+'; '+detail)
        def job(j): return command(str(P/'bin/scontrol'),'show','job',j,'-o')
        try:
            wait_for(lambda: 'JobState=RUNNING' in job(good),30,'eligible job running')
        except RuntimeError as exc:
            raise RuntimeError(str(exc)+'; allowed='+job(good)+'; denied='+job(bad)) from exc
        if os.environ.get('TIME_TEST_SELECTOR_AUDIT') == '1' or live_gpu:
            if helper.rpc(endpoint,f'VERIFY_SELECTOR1 0 {good}') != 'OK': raise RuntimeError('live selector audit rejected a valid allocation')
            if helper.rpc(endpoint,'VERIFY_SELECTOR1 0 4294967294').startswith('OK'): raise RuntimeError('selector audit accepted a missing job')
            print('PASS: selector audit accepts the live allocation and rejects an unknown job',flush=True)
        denied_info=job(bad)
        if 'JobState=PENDING' not in denied_info or not re.search(r'NodeList=\(null\)',denied_info):
            raise RuntimeError('exhausted job received nodes: '+denied_info)
        if rollback_test:
            marker='8'*32
            if not helper.rpc(endpoint,f'RESERVE1 {uid} {marker} 1 1 1').startswith('OK '): raise RuntimeError('rollback hold failed')
            pending=submit(marker,'important')
            (base/'abort-commit').touch()
            if helper.rpc(endpoint,f'BIND1 {uid} {marker} {pending}')!='OK': raise RuntimeError('rollback bind failed')
            def rolled_back():
                detail=job(pending)
                node=command(str(P/'bin/scontrol'),'show','node','colima','-o')
                allocation=re.search(r'AllocTRES=([^ ]+)',node)
                return ('JobState=PENDING' in detail and 'NodeList=(null)' in detail and allocation and
                        'gres/gpu=1' in allocation.group(1) and 'gres/gpu=2' not in allocation.group(1) and
                        ('test-only injected selector failure' if selector_failure else 'test-only injected commit failure') in (base/'daemons.log').read_text())
            try: wait_for(rolled_back,12,'failed commit rolls back selector GPU')
            except RuntimeError as exc: raise RuntimeError(str(exc)+'; job='+job(pending)+'; node='+command(str(P/'bin/scontrol'),'show','node','colima','-o')) from exc
            if 'JobState=RUNNING' not in job(good): raise RuntimeError('rollback interrupted existing task')
            if selector_failure and helper.rpc(endpoint,f'VERIFY_SELECTOR1 0 {good}') != 'OK':
                raise RuntimeError('rollback damaged the existing task selector state')
            print('PASS: selector failure restores GPU while existing task continues' if selector_failure else 'PASS: failed commit releases selector GPU while existing task continues',flush=True)
            return
        if node_restart:
            pidfile=public/('pid-'+allowed)
            wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(),15,'running task PID')
            task_pid=int(pidfile.read_text().strip())
            os.kill(node_process.pid,signal.SIGTERM); node_process.wait(timeout=15); processes.remove(node_process)
            try: ready=helper.rpc(endpoint,'READY1 0')
            except (OSError,RuntimeError): ready='unavailable'
            if ready.startswith('OK '): raise RuntimeError('controller remained ready after node process exited')
            os.kill(task_pid,0)
            node_process=spawn([str(node_bin),'-D','-N','colima','-f',str(conf)],{**env,**shifted_env})
            wait_for(lambda: (proof_dir/'node-ready').exists() and ('|'+str(node_process.pid)+'|') in (proof_dir/'node-ready').read_text(),30,'new verified node process')
            os.killpg(controller.pid,signal.SIGTERM); controller.wait(timeout=15); processes.remove(controller)
            os.close(nodepid_fd); nodepid_fd=os.pidfd_open(node_process.pid); os.set_inheritable(nodepid_fd,True)
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            wait_for(lambda:helper.rpc(endpoint,'READY1 0').startswith('OK '),30,'controller bound to restarted node')
            os.kill(task_pid,0)
            if 'JobState=RUNNING' not in job(good): raise RuntimeError('node restart interrupted existing task')
            wait_for(lambda:'JobState=COMPLETED' in job(good),35,'existing task finishes after node restart')
            wait_for(lambda:helper.rpc(endpoint,f'RESERVE2_GET {uid} {allowed}').split()[-2]=='5',20,'post-restart task settlement')
            print('PASS: restarted node and rebound controller preserve running task and settle it',flush=True)
            return
        if nodepid_exit:
            sentinel.terminate(); sentinel.wait(timeout=5)
            if helper.rpc(endpoint,'READY1 0').startswith('OK '): raise RuntimeError('dead node process reported ready')
            if 'JobState=RUNNING' not in job(good): raise RuntimeError('running task did not survive node process evidence loss')
            if 'JobState=PENDING' not in job(bad) or not re.search(r'NodeList=\(null\)',job(bad)):
                raise RuntimeError('pending task allocated after node evidence loss')
            print('PASS: dead node pidfd closes admission and preserves running task',flush=True)
            return
        if gpu_identity_change:
            # Change only the fake UUID in the GPU-free VM, never a real driver.
            info=next(inventory.glob('*/information'))
            info.write_text(info.read_text().replace('GPU-00000001-', 'GPU-00000009-'))
            os.killpg(controller.pid,signal.SIGTERM);controller.wait(timeout=15);processes.remove(controller)
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            wait_for(lambda:'UP' in command(str(P/'bin/scontrol'),'ping'),30,'controller after fake UUID change')
            try: ready=helper.rpc(endpoint,'READY1 0')
            except (OSError,RuntimeError): ready='unavailable'
            if ready.startswith('OK '): raise RuntimeError('changed hardware identity reopened admission')
            if 'JobState=RUNNING' not in job(good): raise RuntimeError('GPU task did not survive identity mismatch')
            print('PASS: changed GPU UUID keeps admission closed and preserves running task',flush=True)
            return
        if running_cancel:
            command(str(P/'bin/scancel'),good)
        if cancel_pending:
            if helper.rpc(endpoint,f'CANCEL_BOUND1 {uid} {allowed} {good}').startswith('OK'):
                raise RuntimeError('active allocation accepted unstarted cancellation')
            for marker,bind in (('8'*32,True),('9'*32,False)):
                if not helper.rpc(endpoint,f'RESERVE1 {uid} {marker} 1 1 1').startswith('OK '): raise RuntimeError('pending cancellation hold failed')
                pending=submit(marker,'important')
                if bind and helper.rpc(endpoint,f'BIND1 {uid} {marker} {pending}')!='OK': raise RuntimeError('pending cancellation bind failed')
                if bind and (stale_cancel or missing_script):
                    # Save a real pending Slurm checkpoint, then simulate a
                    # crash leaving that checkpoint behind the quota WAL.
                    os.killpg(controller.pid,signal.SIGTERM);controller.wait(timeout=15);processes.remove(controller)
                    saved=(state/'job_state').read_bytes()
                    controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
                    wait_for(lambda: helper.rpc(endpoint,'READY1 0').startswith('OK '),30,'controller before simulated crash')
                    if 'JobState=PENDING' not in job(pending): raise RuntimeError('checkpoint did not retain pending job')
                    if stale_cancel:
                        command(str(P/'bin/scancel'),pending)
                        if helper.rpc(endpoint,f'RESERVE2_GET {uid} {marker}').split()[-2]!='6': raise RuntimeError('cancel not journalled')
                    else:
                        candidates=list(state.glob('hash.*/job.'+pending+'/script'))
                        if len(candidates)!=1: raise RuntimeError('pending job script not found uniquely')
                        shutil.rmtree(candidates[0].parent)
                    os.killpg(controller.pid,signal.SIGKILL);controller.wait(timeout=15);processes.remove(controller)
                    with (state/'job_state').open('wb') as f: f.write(saved);f.flush();os.fsync(f.fileno())
                    controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
                    wait_for(lambda: helper.rpc(endpoint,'READY1 0').startswith('OK '),30,'reconcile terminal reservation on restart')
                    expected='JobState=CANCELLED' if stale_cancel else 'JobState=FAILED'
                    wait_for(lambda: expected in job(pending),15,'recovered terminal job state')
                    if 'JobState=RUNNING' not in job(good): raise RuntimeError('unrelated running job did not survive')
                    print('PASS: stale cancelled checkpoint reconciled' if stale_cancel else 'PASS: startup script failure releases reservation',flush=True)
                else:
                    command(str(P/'bin/scancel'),pending)
                    wait_for(lambda: 'JobState=CANCELLED' in job(pending),30,'pending cancellation')
                automatic=helper.rpc(endpoint,f'RESERVE2_GET {uid} {marker}').split()
                if len(automatic)!=12 or automatic[-2]!='6':
                    raise RuntimeError('controller failed to record cancellation before broker reconciliation')
                if purge_pending and bind:
                    def purged():
                        result=subprocess.run([str(P/'bin/scontrol'),'show','job',pending],env=env,capture_output=True,text=True,timeout=8)
                        return result.returncode != 0
                    wait_for(purged,25,'controller purges the finished unstarted job')
                    print('PASS: durable cancellation survives Slurm job-record purge before broker recovery',flush=True)
                import sqlite3
                sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
                from controller_bridge import ControllerBridge
                from monthly_reservation import ReservationCoordinator
                db=sqlite3.connect(':memory:',isolation_level=None); db.row_factory=sqlite3.Row
                coordinator=ReservationCoordinator(db,ControllerBridge(str(endpoint),0))
                month=int(helper.rpc(endpoint,f'SNAP2 {uid}').split()[2])
                db.execute('INSERT INTO monthly_dispatch_holds VALUES (?,?,?,?,?,?,?,?)',
                           (marker,uid,'important',1,1,month,'BOUND' if bind else 'BINDING',int(pending)))
                try: coordinator.finish(marker,uid,int(pending))
                except Exception as exc: raise RuntimeError('broker cancellation rejected: '+str(exc)+'; '+job(pending)) from exc
                if db.execute('SELECT state FROM monthly_dispatch_holds').fetchone()[0]!='CLOSED': raise RuntimeError('broker did not close hold')
                db.close()
                if helper.rpc(endpoint,f'CANCEL_BOUND1 {uid} {marker} {pending}')!='OK': raise RuntimeError('unstarted cancellation retry failed')
                status=helper.rpc(endpoint,f'RESERVE2_GET {uid} {marker}').split()
                if len(status)!=12 or status[8]!=pending or status[-2]!='6': raise RuntimeError('unstarted cancellation identity mismatch')
            print('PASS: never-started bound and unbound scheduler jobs release holds; active job is protected',flush=True)
        if stale_release:
            # Preserve an actual RUNNING Slurm checkpoint before the durable
            # quota RELEASE2, then replay it to model a crash in between.
            os.killpg(controller.pid,signal.SIGTERM);controller.wait(timeout=15);processes.remove(controller)
            stale_state=(state/'job_state').read_bytes()
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            wait_for(lambda: helper.rpc(endpoint,'READY1 0').startswith('OK '),30,'running checkpoint recovery')
            if 'JobState=RUNNING' not in job(good): raise RuntimeError('running task did not survive first restart')
        if priority_test:
            if helper.rpc(endpoint,f'CANCEL1 {uid} {full}')!='OK': raise RuntimeError('cannot free debug test capacity')
            low,high='6'*32,'7'*32
            if not helper.rpc(endpoint,f'RESERVE1 {uid} {low} 2 1 1').startswith('OK '): raise RuntimeError('normal hold failed')
            low_id=submit(low,'normal')
            if helper.rpc(endpoint,f'BIND1 {uid} {low} {low_id}')!='OK': raise RuntimeError('normal bind failed')
            if not helper.rpc(endpoint,f'RESERVE1 {uid} {high} 0 1 1').startswith('OK '): raise RuntimeError('debug hold failed')
            high_id=submit(high,'debug')
            if helper.rpc(endpoint,f'BIND1 {uid} {high} {high_id}')!='OK': raise RuntimeError('debug bind failed')
            if 'JobState=PENDING' not in job(low_id) or 'JobState=PENDING' not in job(high_id):
                raise RuntimeError('priority jobs did not queue behind running task')
        if active_rollover:
            queued='5'*32
            if not helper.rpc(endpoint,f'RESERVE1 {uid} {queued} 1 1 1').startswith('OK '): raise RuntimeError('queued old-month hold failed')
            queued_id=submit(queued,'important')
            if helper.rpc(endpoint,f'BIND1 {uid} {queued} {queued_id}')!='OK': raise RuntimeError('queued old-month binding failed')
            if 'JobState=PENDING' not in job(queued_id): raise RuntimeError('second job was not queued')
            offset_file.write_text(str(offset+101*3600+35))
            current=helper.rpc(endpoint,f'SNAP2 {uid}').split()
            if (len(current)!=18 or current[2]!=next_month.strftime('%Y%m') or
                    (int(current[7])==0 and int(current[8])==0) or int(current[9])!=0 or int(current[10])!=0):
                raise RuntimeError('running allocation did not reset to its selected tier: '+' '.join(current))
            new='4'*32
            if not helper.rpc(endpoint,f'RESERVE1 {uid} {new} 0 1 1').startswith('OK '):
                raise RuntimeError('prior-month active or queued hold blocked new-month capacity')
            if helper.rpc(endpoint,f'CANCEL1 {uid} {new}')!='OK': raise RuntimeError('new-month hold cancellation failed')
            if helper.rpc(endpoint,f'ROLL1 {uid} {queued}')!='OK': raise RuntimeError('bound pending reservation did not roll')
            rolled=helper.rpc(endpoint,f'RESERVE2_GET {uid} {queued}').split()
            if len(rolled)!=12 or rolled[3]!=next_month.strftime('%Y%m') or rolled[-2]!='1':
                raise RuntimeError('bound queued rollover identity mismatch: '+' '.join(rolled))
        if restart_running:
            os.killpg(controller.pid,signal.SIGTERM); controller.wait(timeout=15); processes.remove(controller)
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            def live_recovered():
                value=helper.rpc(endpoint,f'RESERVE2_GET {uid} {allowed}').split()
                return value if len(value)==12 and value[-2]=='3' else None
            wait_for(live_recovered,30,'running allocation ledger recovery')
            if 'JobState=RUNNING' not in job(good): raise RuntimeError('running job did not survive controller restart')
        if restart_completing:
            unrelated=None
            second_gpu=None
            if live_gpu:
                other_marker='8'*32
                if not helper.rpc(endpoint,f'RESERVE1 {uid} {other_marker} 2 1 1').startswith('OK '): raise RuntimeError('second GPU reservation failed')
                second_gpu=submit(other_marker,'normal')
                if helper.rpc(endpoint,f'BIND1 {uid} {other_marker} {second_gpu}')!='OK': raise RuntimeError('second GPU binding failed')
                wait_for(lambda:'JobState=RUNNING' in job(second_gpu),25,'second GPU task started')
                if helper.rpc(endpoint,f'VERIFY_SELECTOR1 0 {good}') != 'OK' or helper.rpc(endpoint,f'VERIFY_SELECTOR1 0 {second_gpu}') != 'OK':
                    raise RuntimeError('selector audit rejected distinct concurrent GPU allocations')
                # Persist a checkpoint containing both running allocations.
                os.killpg(controller.pid,signal.SIGTERM);controller.wait(timeout=15);processes.remove(controller)
                controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
                wait_for(lambda:helper.rpc(endpoint,'READY1 0').startswith('OK '),30,'two-GPU checkpoint recovery')
                if 'JobState=RUNNING' not in job(good) or 'JobState=RUNNING' not in job(second_gpu): raise RuntimeError('GPU tasks did not survive checkpoint restart')
            if os.environ.get('TIME_TEST_UNRELATED_RUNNING') == '1':
                unrelated=command(str(P/'bin/sbatch'),'--parsable','--uid='+str(uid),'--gid='+str(trainer.pw_gid),
                    '--partition=isolated','--nodes=1-1','--cpus-per-task=1','--mem=128M',
                    '--time=00:02:00','--chdir='+str(public),'--output='+str(public/'other-%j.out'),'--wrap=exec sleep 45').split(';',1)[0]
                wait_for(lambda: 'JobState=RUNNING' in job(unrelated),25,'unrelated CPU task started')
            wait_for(lambda: (base/'epilog-started').exists(),30,'node epilog started')
            if 'JobState=COMPLETING' not in job(good): raise RuntimeError('job not completing at crash point')
            if reconfig_completing:
                result=subprocess.run([str(P/'bin/scontrol'),'reconfigure'],env=env,cwd=str(public),text=True,capture_output=True,timeout=8)
                if result.returncode == 0: raise RuntimeError('unsafe live reconfiguration was not rejected')
                if not helper.rpc(endpoint,'READY1 0').startswith('OK '): raise RuntimeError('rejected reconfiguration closed a healthy ledger')
                wal=(state/'monthly-quota.wal').read_text()
                if wal.count('|ENDING2|')!=1 or '|UNKNOWN4|' in wal: raise RuntimeError('reconfiguration repeated quota termination')
                wait_for(lambda: helper.rpc(endpoint,f'RESERVE2_GET {uid} {allowed}').split()[-2]=='5',40,'settlement after reconfiguration')
                if 'JobState=COMPLETED' not in job(good): raise RuntimeError('job did not finish after reconfiguration')
                print('PASS: unsafe reconfiguration rejected and settlement succeeds',flush=True)
                return
            # A graceful stop saves the COMPLETING checkpoint while the
            # separately running node epilog is still sleeping. The hard-crash
            # scenario instead uses the older RUNNING checkpoint.
            os.killpg(controller.pid,signal.SIGTERM if saved_completing else signal.SIGKILL)
            controller.wait(timeout=15); processes.remove(controller)
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            wait_for(lambda: 'UP' in command(str(P/'bin/scontrol'),'ping'),30,'controller restart while completing')
            if unrelated and 'JobState=RUNNING' not in job(unrelated): raise RuntimeError('unrelated running task did not survive controller restart')
            log=(base/'daemons.log').read_text()
            if 'monthly quota recovery waiting for cleanup reconciliation' not in log or 'ledger entering fail-closed state' in log:
                raise RuntimeError('recoverable termination did not enter the distinct waiting state')
            if not saved_completing:
                for action in (('scancel',good),('scontrol','requeue',good),('scontrol','update','JobId='+good,'Comment=changed')):
                    result=subprocess.run([str(P/'bin'/action[0]),*action[1:]],env=env,cwd=str(public),text=True,capture_output=True,timeout=8)
                    if result.returncode == 0: raise RuntimeError('stale terminated task accepted '+action[0])
            if saved_completing:
                log=(base/'daemons.log').read_text()
                if 'deferring quota recovery for JobId='+good not in log:
                    raise RuntimeError('COMPLETING checkpoint was not deferred by the quota recovery guard')
                wal=(state/'monthly-quota.wal').read_text()
                if wal.count('|ENDING2|')!=1 or '|UNKNOWN4|' in wal:
                    raise RuntimeError('restart repeated quota termination or wrote an unknown-release event')
                for action in (('scancel',good),('scontrol','requeue',good),('scontrol','update','JobId='+good,'Comment=changed')):
                    result=subprocess.run([str(P/'bin'/action[0]),*action[1:]],env=env,cwd=str(public),text=True,capture_output=True,timeout=8)
                    if result.returncode == 0: raise RuntimeError('quarantined task accepted '+action[0])
                if 'JobState=COMPLETING' not in job(good): raise RuntimeError('quarantined job state was changed')
                if os.environ.get('TIME_TEST_DEFER_NODE_DOWN') == '1':
                    command(str(P/'bin/scontrol'),'update','NodeName=colima','State=DOWN','Reason=isolated-recovery-test')
                    if 'State=DOWN' not in command(str(P/'bin/scontrol'),'show','node','colima','-o'):
                        raise RuntimeError('node DOWN event was suppressed')
                    if 'JobState=COMPLETING' not in job(good): raise RuntimeError('node DOWN corrupted deferred job')
                print('PASS: recovered COMPLETING checkpoint avoids duplicate deallocation and termination',flush=True)
            try:
                answer=helper.rpc(endpoint,f'SNAP2 {uid}')
                if not answer.startswith('ERROR '): raise RuntimeError('unsafe ledger response after restart: '+answer)
            except (OSError, TimeoutError):
                pass  # no socket is also fail-closed
            if f'|RELEASE2|{good}|' in (state/'monthly-quota.wal').read_text(): raise RuntimeError('interrupted cleanup was released before node proof')
            wait_for(lambda: 'epilog for JobId='+good in (base/'daemons.log').read_text(),30,'interrupted node epilog finished')
            wait_for(lambda: f'|RELEASE2|{good}|' in (state/'monthly-quota.wal').read_text(),15,'proven recovered allocation settled')
            def verify_proof_cutoff(job_id):
                lines=[line.split('|') for line in proof.read_text().splitlines() if line.startswith('P6NP1|'+job_id+'|')]
                releases=[line.split('|') for line in (state/'monthly-quota.wal').read_text().splitlines() if '|RELEASE2|'+job_id+'|' in line]
                if len(lines)!=1 or len(releases)!=1 or (lines[0][8],lines[0][9]) != (releases[0][5],releases[0][6]):
                    raise RuntimeError('recovery did not settle exactly at the verified node proof')
            verify_proof_cutoff(good)
            if unrelated and 'JobState=RUNNING' not in job(unrelated): raise RuntimeError('unrelated running task was interrupted during recovery')
            if second_gpu:
                def second_has_proof():
                    wal=(state/'monthly-quota.wal').read_text()
                    return f'|ENDING2|{second_gpu}|' in wal and f'|SELECT_DONE1|{second_gpu}|' in wal and f'P6NP1|{second_gpu}|' in proof.read_text()
                wait_for(second_has_proof,45,'second running GPU job records durable ending and node proof while waiting')
                wal=(state/'monthly-quota.wal').read_text()
                wait_for(lambda:f'|RELEASE2|{second_gpu}|' in (state/'monthly-quota.wal').read_text(),15,'second recovered allocation settled')
                if 'ledger entering fail-closed state' in (base/'daemons.log').read_text():
                    raise RuntimeError('waiting state lost a live task completion')
                verify_proof_cutoff(second_gpu)
                if 'JobState=COMPLETED' not in job(second_gpu): raise RuntimeError('second task did not finish naturally')
                def second_purged():
                    result=subprocess.run([str(P/'bin/scontrol'),'show','job',second_gpu],env=env,capture_output=True,text=True,timeout=8)
                    return result.returncode != 0
                wait_for(second_purged,30,'second completed job purged')
                os.killpg(controller.pid,signal.SIGTERM);controller.wait(timeout=15);processes.remove(controller)
                controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
                wait_for(lambda: 'UP' in command(str(P/'bin/scontrol'),'ping'),30,'restart after completed job purge')
                log=(base/'daemons.log').read_text()
                if 'ledger entering fail-closed state' in log:
                    raise RuntimeError('purged completed job lost its recovery evidence after restart')
                wait_for(lambda: helper.rpc(endpoint,'READY1 0').startswith('OK '),20,'reopened controller after purge')
                for marker in (allowed, other_marker):
                    status=helper.rpc(endpoint,f'RESERVE2_GET {uid} {marker}').split()
                    if len(status)!=12 or status[-2]!='5': raise RuntimeError('settlement tombstone lost after job purge and restart')
                verify_proof_cutoff(good); verify_proof_cutoff(second_gpu)
                print('PASS: another GPU job finishes; proof and settlement survive job purge and controller restart',flush=True)
            if require_recovery:
                wait_for(lambda: helper.rpc(endpoint,'READY1 0').startswith('OK '),20,'automatic recovery after verified cleanup')
                if second_gpu:
                    def original_completion():
                        result=subprocess.run([str(P/'bin/sacct'),'-n','-P','-j',good,'-o','JobIDRaw,State,ExitCode'],env=env,cwd=str(public),text=True,capture_output=True,timeout=8)
                        return result.returncode == 0 and any(line.startswith(good+'|COMPLETED|0:0') for line in result.stdout.splitlines())
                    wait_for(original_completion,20,'recovered terminal state in accounting after job purge')
                elif 'JobState=COMPLETED' not in job(good): raise RuntimeError('recovered job did not reach its original terminal state')
                print('PASS: controller recovered verified cleanup without interrupting other jobs',flush=True)
                return
            if saved_completing:
                wait_for(lambda: 'deferring epilog completion for quota recovery JobId='+good in (base/'daemons.log').read_text(),10,'deferred epilog RPC')
                log=(base/'daemons.log').read_text()
                if 'comp_job_cnt underflow' in log or 'deferring epilog completion for quota recovery JobId='+good not in log:
                    raise RuntimeError('node completion was processed before recovered resources were reconciled')
                if 'JobState=COMPLETING' not in job(good): raise RuntimeError('deferred job lost its completing state')
                print('PASS: cleanup proof is retained without prematurely changing restored node state',flush=True)
            print('PASS: recovered cleanup settled at node proof; admission remains closed pending Slurm repair',flush=True)
            print('recovered scheduler job:',job(good),flush=True)
            return
        if proof_failure:
            proof.rename(proof_dir/'withheld.wal')
        if fail_epilog or old_node or proof_failure:
            wait_for(lambda: helper.rpc(endpoint,f'SNAP2 {uid}').startswith('ERROR '),30,'unverified node cleanup closes ledger')
            wal=(state/'monthly-quota.wal').read_text()
            if '|RELEASE2|' in wal: raise RuntimeError('failed cleanup prematurely released the allocation')
            print('PASS: unverified node cleanup leaves allocation unreleased and ledger unavailable',flush=True)
            return
        final_state='CANCELLED' if running_cancel else 'FAILED' if running_failure else 'COMPLETED'
        wait_for(lambda: 'JobState='+final_state in job(good),70 if stale_release else 60 if purge_pending or stale_cancel or missing_script else 30,'eligible job reached final state')
        wait_for(lambda: helper.rpc(endpoint,f'RESERVE2_GET {uid} {allowed}').split()[-2]=='5',30,'running allocation settlement')
        if priority_test:
            wait_for(lambda: 'JobState=RUNNING' in job(high_id),40,'debug job starts before normal')
            if 'JobState=PENDING' not in job(low_id): raise RuntimeError('normal job ran before queued debug job')
            wait_for(lambda: 'JobState=COMPLETED' in job(high_id),30,'debug priority job completed')
            wait_for(lambda: 'JobState=COMPLETED' in job(low_id),40,'normal priority job completed')
            print('PASS: '+sched_mode+' scheduler started debug before an earlier normal job',flush=True)
        if active_rollover:
            wait_for(lambda: 'JobState=COMPLETED' in job(queued_id),40,'rolled queued job completed')
            if helper.rpc(endpoint,f'RESERVE2_GET {uid} {queued}').split()[-2]!='5':
                raise RuntimeError('rolled queued job was not settled')
        if stale_release:
            if helper.rpc(endpoint,f'RESERVE2_GET {uid} {allowed}').split()[-2]!='5': raise RuntimeError('release was not durable')
            os.killpg(controller.pid,signal.SIGKILL);controller.wait(timeout=15);processes.remove(controller)
            with (state/'job_state').open('wb') as f: f.write(stale_state);f.flush();os.fsync(f.fileno())
            controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
            wait_for(lambda:'UP' in command(str(P/'bin/scontrol'),'ping'),30,'stale released job recovery')
            try: ready=helper.rpc(endpoint,'READY1 0')
            except (OSError,RuntimeError): ready='unavailable'
            if not require_recovery:
                log=(base/'daemons.log').read_text()
                if 'monthly quota recovery waiting for cleanup reconciliation' not in log or 'ledger entering fail-closed state' in log:
                    raise RuntimeError('released tombstone did not enter the distinct waiting state')
                for action in (('scancel',good),('scontrol','requeue',good),('scontrol','update','JobId='+good,'Comment=changed')):
                    result=subprocess.run([str(P/'bin'/action[0]),*action[1:]],env=env,cwd=str(public),text=True,capture_output=True,timeout=8)
                    if result.returncode == 0: raise RuntimeError('stale released task accepted '+action[0])
                if 'JobState=RUNNING' not in job(good): raise RuntimeError('stale released job was changed')
            if require_recovery:
                wait_for(lambda: helper.rpc(endpoint,'READY1 0').startswith('OK '),20,'automatic recovery after durable release')
                if 'JobState=COMPLETED' not in job(good): raise RuntimeError('released job retained stale running state')
                print('PASS: durable release reconciled stale Slurm checkpoint',flush=True)
                return
            if ready.startswith('OK '): raise RuntimeError('stale running job incorrectly advertised readiness')
            print('PASS: stale RUNNING checkpoint after RELEASE2 fails closed; automatic repair still required',flush=True)
            return
        snapshot=helper.rpc(endpoint,f'SNAP2 {uid}')
        if not snapshot.startswith('OK 2 '): raise RuntimeError('ledger unavailable after job completion: '+snapshot)
        fields=snapshot.split()
        if int(fields[7])==0 and int(fields[8])==0: raise RuntimeError('actual important tier usage was not settled: '+snapshot)
        status=helper.rpc(endpoint,f'RESERVE2_GET {uid} {allowed}').split()
        if ending_metadata:
            events=[line.split('|') for line in (state/'monthly-quota.wal').read_text().splitlines() if '|ENDING2|' in line]
            if (len(events)!=1 or len(events[0])!=17 or events[0][3]!=good or events[0][5]!=status[-1] or
                    events[0][6]!=str(uid) or events[0][-1]!='colima' or int(events[0][12])>int(events[0][13])):
                raise RuntimeError('durable termination identity and outcome are incomplete: '+str(events))
            if running_failure and int(events[0][10]) != 7*256: raise RuntimeError('failed job exit code was not retained')
            wal=(state/'monthly-quota.wal').read_text().splitlines()
            positions={name:next((i for i,line in enumerate(wal) if '|'+name+'|' in line),None) for name in ('GPU_MAP1','COMMIT5','ENDING2','SELECT_DONE1','RELEASE2')}
            if any(value is None for value in positions.values()) or not positions['GPU_MAP1'] < positions['COMMIT5'] < positions['ENDING2'] < positions['SELECT_DONE1'] < positions['RELEASE2']:
                raise RuntimeError('allocation identity and release were not durably recorded in order: '+str(positions))
            gpu_map=wal[positions['GPU_MAP1']].split('|')
            if len(gpu_map)!=10 or gpu_map[3]!=good or gpu_map[5]!=status[-1] or gpu_map[6]!='colima' or gpu_map[7].count('1')!=1:
                raise RuntimeError('durable GPU device identity mismatch: '+str(gpu_map))
            evidence=[line.split('|') for line in proof.read_text().splitlines() if line.startswith('P6NP1|') and '|HEADER|' not in line]
            if (not evidence or len(evidence[0])!=11 or evidence[0][1]!=good or evidence[0][2]!=status[-1] or
                    evidence[0][3]!=str(uid) or evidence[0][-1]!='colima'):
                raise RuntimeError('durable node proof missing or mismatched: '+str(evidence))
            print('PASS: '+final_state+' termination metadata and node cleanup proof persisted before release',flush=True)
        if len(status)!=12 or status[-2]!='5' or status[8]!=good or int(status[-1])==0:
            raise RuntimeError('reservation not released with job and run identity: '+' '.join(status))
        os.killpg(controller.pid,signal.SIGTERM); controller.wait(timeout=15); processes.remove(controller)
        controller=spawn([str(P/'sbin/slurmctld'),'-D','-f',str(conf)],{**env,**shifted_env,'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd),'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd)},(fd,proof_fd,nodepid_fd))
        def recovered_snapshot():
            value=helper.rpc(endpoint,f'SNAP2 {uid}')
            return value if value.startswith('OK 2 ') else None
        recovered=wait_for(recovered_snapshot,30,'controller ledger recovery')
        before,after=snapshot.split(),recovered.split()
        if before[:13]!=after[:13] or before[15:]!=after[15:]:
            raise RuntimeError('settled ledger changed after restart: '+recovered+' vs '+snapshot)
        recovered_status=helper.rpc(endpoint,f'RESERVE2_GET {uid} {allowed}').split()
        if recovered_status!=status: raise RuntimeError('released reservation changed after restart')
        if cancel_pending:
            for marker in ('8'*32,'9'*32):
                if helper.rpc(endpoint,f'RESERVE2_GET {uid} {marker}').split()[-2]!='6':
                    raise RuntimeError('cancelled bound reservation lost on restart')
        print('PASS: exhausted job unallocated; eligible job settled and recovered after controller restart',flush=True)
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                os.killpg(p.pid,signal.SIGTERM)
                try: p.wait(timeout=15)
                except subprocess.TimeoutExpired: os.killpg(p.pid,signal.SIGKILL); p.wait(timeout=5)
        if fd>=0: os.close(fd)
        if proof_fd>=0: os.close(proof_fd)
        os.close(nodepid_fd)
        if sentinel and sentinel.poll() is None: sentinel.terminate(); sentinel.wait(timeout=5)
        logfile.close()

if __name__=='__main__':
    try: main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print('isolated fake GPU fixture failed:',exc,file=sys.stderr); sys.exit(1)
