#!/usr/bin/env python3
"""Root-only Slurm 26 controller recovery check in the dedicated Colima VM.

Creates its own state, socket, listener ports and witness. No real node or GPU
is accessed. Evidence is retained in a fresh directory below /tmp.
"""
import datetime as dt
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import uuid

PREFIX = Path(os.environ.get('TIME26_VALIDATION_PREFIX', ''))
HEADER = b'U20MQ1|HEADER|1\n'


def rpc(path, message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        s.connect(str(path))
        pid, uid, _ = struct.unpack('3i', s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0 or pid <= 0:
            raise RuntimeError('unexpected controller peer')
        s.sendall(message.encode('ascii') + b'\n')
        data = b''
        while b'\n' not in data and len(data) <= 1024:
            block = s.recv(512)
            if not block: break
            data += block
        if not data.endswith(b'\n') or data.count(b'\n') != 1:
            raise RuntimeError('incomplete response')
        return data.decode('ascii').strip()


def fake_gpu_inventory(base, count=1):
    root=base/'gpu-inventory';root.mkdir(mode=0o700)
    for i in range(count):
        device=root/('0000:%02x:00.0'%(i+1));device.mkdir()
        (device/'information').write_text('GPU UUID: GPU-%08x-0000-0000-0000-%012x\nBus Location: 0000:%02x:00.0\nDevice Minor: %d\n'%(i+1,i+1,i+1,i))
    return root


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def validate_prefix():
    if not os.environ.get('TIME26_VALIDATION_PREFIX'):
        raise RuntimeError('TIME26_VALIDATION_PREFIX must explicitly select the candidate build')
    path = PREFIX.resolve(strict=True)
    if path.name != 'install' or path.parent.parent != Path('/tmp') or not path.parent.name.startswith('time-slurm26-'):
        raise RuntimeError('controller prefix is not an isolated /tmp/time-slurm26-* build')


def main():
    validate_prefix()
    virt = subprocess.check_output(['systemd-detect-virt'], text=True).strip()
    if (os.geteuid() != 0 or os.uname().nodename != 'colima' or virt != 'apple'
            or any(Path('/dev').glob('nvidia*')) or not (PREFIX/'sbin/slurmctld').is_file()):
        raise RuntimeError('this fixture is restricted to the dedicated GPU-free Colima VM')
    base = Path('/tmp') / ('time26-controller-probe-' + uuid.uuid4().hex)
    base.mkdir(mode=0o700)
    inventory=fake_gpu_inventory(base)
    (base/'gres.conf').write_text('NodeName=time-fake-node Name=gpu File=/dev/null\n')
    rollover = os.environ.get('TIME_TEST_MONTH_ROLLOVER') == '1'
    shifted_env = {}
    if rollover:
        tz=dt.timezone(dt.timedelta(hours=8))
        local=dt.datetime.now(tz)
        next_month=(local.replace(day=28,hour=0,minute=0,second=0,microsecond=0)+dt.timedelta(days=4)).replace(day=1)
        offset=int(next_month.timestamp()-time.time())-30
        offset_file=base/'clock-offset'; offset_file.write_text(str(offset))
        shim=base/'clock-shift.so'
        subprocess.run(['cc','-shared','-fPIC','-O2',str(Path(__file__).with_name('isolated-clock-shift.c')),'-ldl','-o',str(shim)],check=True,timeout=20)
        shifted_env={'LD_PRELOAD':str(shim),'TIME_FAKE_REALTIME_OFFSET_FILE':str(offset_file)}
    state = base/'state'; state.mkdir(mode=0o700)
    spool = base/'spool'; spool.mkdir(mode=0o700)
    digest = hashlib.sha256(HEADER).hexdigest().encode()
    for path, data in ((state/'monthly-quota.wal',HEADER),
                       (state/'monthly-quota.checkpoint', b'U20MQ1|CHECKPOINT|0|' + digest + b'\n'),
                       (base/'witness',b'U20MQA1|0|' + digest + b'\n'),
                       (base/'node-proof.wal',b'P6NP1|HEADER|1\n')):
        fd = os.open(path, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
    ctl, node = port(), port()
    if ctl == node or ctl in (6817,6818) or node in (6817,6818):
        raise RuntimeError('unsafe port selection')
    config = base/'slurm.conf'
    config.write_text(f'''ClusterName=time26-{uuid.uuid4().hex[:8]}
SlurmctldHost=colima(127.0.0.1)
SlurmUser=root
SlurmctldPort={ctl}
SlurmdPort={node}
StateSaveLocation={state}
SlurmctldPidFile={state}/controller.pid
SlurmdPidFile={spool}/slurmd.pid
SlurmdSpoolDir={spool}
PluginDir={PREFIX}/lib/slurm
AuthType=auth/munge
CredType=cred/munge
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
SchedulerType=sched/backfill
ProctrackType=proctrack/linuxproc
TaskPlugin=task/none
MpiDefault=none
AccountingStorageType=accounting_storage/none
JobCompType=jobcomp/none
SlurmctldParameters=MonthlyGpuQuota
SlurmctldTimeout=30
SlurmdTimeout=30
MessageTimeout=2
GresTypes=gpu
NodeName=time-fake-node CPUs=1 RealMemory=256 Gres=gpu:1 State=UNKNOWN
PartitionName=isolated Nodes=time-fake-node Default=YES MaxTime=01:00:00 State=UP
''')
    nodepid_fd=os.pidfd_open(os.getpid()); os.set_inheritable(nodepid_fd,True)
    logpath = base/'controller.log'
    print('isolated evidence:', base, flush=True)
    def cycle(cutover):
        fd = os.open(base/'witness', os.O_RDWR|os.O_APPEND|os.O_NOFOLLOW)
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        os.set_inheritable(fd, True)
        proof_fd=os.open(base/'node-proof.wal',os.O_RDONLY|os.O_NOFOLLOW);os.set_inheritable(proof_fd,True)
        env = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C', 'SLURM_CONF':str(config),
               'TIME_QUOTA_WITNESS_FD':str(fd), 'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd), 'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd), 'TIME26_TEST_GPU_INFO_DIR':str(inventory), 'LD_LIBRARY_PATH':str(PREFIX/'lib/slurm'), **shifted_env}
        proc = None
        try:
            with logpath.open('ab', buffering=0) as log:
                proc = subprocess.Popen([str(PREFIX/'sbin/slurmctld'),'-D','-f',str(config)],
                    cwd='/',env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,pass_fds=(fd,proof_fd,nodepid_fd),start_new_session=True)
                endpoint=state/'time-quota.sock'; deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    if proc.poll() is not None: raise RuntimeError('controller exited: '+str(proc.returncode))
                    try:
                        result=rpc(endpoint,'SNAP 1001' if cutover else 'SNAP2 1001')
                        if result.startswith('OK '): break
                    except (OSError,RuntimeError): pass
                    time.sleep(.2)
                else: raise RuntimeError('controller not ready')
                if cutover:
                    if not rpc(endpoint,'READY1 0').startswith('ERROR '): raise RuntimeError('readiness claimed before cutover')
                    if rpc(endpoint,'CUTOVER2_RESET_USAGE 0') != 'OK': raise RuntimeError('cutover failed')
                    if rpc(endpoint,'READY1 0') != 'OK TIME26_SELECTABLE_ENDING2_ROLL1_RANK1_CANCEL_BOUND1_AUTOCANCEL1_RECONCILE1_NODEPROOF1_SELECT_DONE1_GPUMAP1_CANCEL_EMPTY1': raise RuntimeError('controller readiness missing')
                    month = int(dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y%m'))
                    if rpc(endpoint, f'GRANT 1001 {month} 3600 fixture-grant uid-0') != 'OK':
                        raise RuntimeError('grant failed')
                    if rpc(endpoint, f'GRANT 1001 {month} 3600 fixture-grant uid-0') != 'OK':
                        raise RuntimeError('idempotent grant retry failed')
                    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
                    from controller_bridge import ControllerBridge
                    from monthly_reservation import ReservationCoordinator, ReservationDeferred
                    database=sqlite3.connect(':memory:',isolation_level=None)
                    database.row_factory=sqlite3.Row
                    coordinator=ReservationCoordinator(database,ControllerBridge(str(endpoint),0))
                    # Crash after SQLite intent and before socket send: retry the
                    # exact immutable identity against the real controller.
                    database.execute('INSERT INTO monthly_dispatch_holds VALUES (?,?,?,?,?,?,?,?)',('e'*32,1001,'normal',1,1,month,'REQUESTING',None))
                    coordinator.hold('e'*32,1001,'normal',1,1,month)
                    if rpc(endpoint,'RESERVE1 1001 '+('e'*32)+' 2 1 1') != 'OK 60000000000':
                        raise RuntimeError('same reservation identity is not idempotent')
                    coordinator.cancel('e'*32,1001)
                    database.execute('INSERT INTO monthly_dispatch_holds VALUES (?,?,?,?,?,?,?,?)',('f'*32,1001,'normal',1,1,month,'REQUESTING',None))
                    coordinator.cancel('f'*32,1001)
                    if rpc(endpoint,'RESERVE1 1001 '+('f'*32)+' 2 1 1').startswith('OK'):
                        raise RuntimeError('late reservation bypassed absent cancellation tombstone')
                    from concurrent.futures import ThreadPoolExecutor
                    for marker in ('7'*32,'8'*32,'9'*32):
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            reserve=pool.submit(rpc,endpoint,f'RESERVE1 1001 {marker} 2 1 1')
                            cancel=pool.submit(rpc,endpoint,f'CANCEL_REQUEST1 1001 {marker} 2 1 1')
                            reserve_reply, cancel_reply=reserve.result(),cancel.result()
                        if cancel_reply != 'OK' or not (reserve_reply.startswith('OK ') or reserve_reply.startswith('ERROR')):
                            raise RuntimeError('reserve/cancel race produced unexpected outcome')
                        final=rpc(endpoint,f'RESERVE2_GET 1001 {marker}').split()
                        if len(final)!=12 or final[-2]!='4': raise RuntimeError('reserve/cancel race left an active hold')
                    coordinator.hold('a'*32,1001,'debug',1,300,month)
                    try: coordinator.hold('b'*32,1001,'debug',1,1,month)
                    except ReservationDeferred: pass
                    else: raise RuntimeError('capacity denial not distinguished')
                    coordinator.cancel('a'*32,1001)
                    coordinator.hold('b'*32,1001,'debug',1,1,month)
                    coordinator.cancel('b'*32,1001)
                    if rollover:
                        old='c'*32; new='d'*32
                        coordinator.hold(old,1001,'debug',1,300,month)
                        offset_file.write_text(str(offset+60))
                        next_key=int(next_month.strftime('%Y%m'))
                        if rpc(endpoint,'SNAP2 1001').split()[2] != str(next_key): raise RuntimeError('test clock did not advance month')
                        coordinator.hold(new,1001,'debug',1,300,next_key)
                        try: coordinator.hold(old,1001,'debug',1,300,next_key)
                        except ReservationDeferred: pass
                        else: raise RuntimeError('rollover bypassed new-month held capacity')
                        if coordinator.bridge.reservation_status(1001,old)['month'] != month: raise RuntimeError('denied rollover mutated old hold')
                        coordinator.cancel(new,1001)
                        coordinator.hold(old,1001,'debug',1,300,next_key)
                        if coordinator.bridge.reservation_status(1001,old)['month'] != next_key: raise RuntimeError('rollover did not persist')
                    database.close()
                    result=rpc(endpoint,'SNAP2 1001')
                    # Reconfiguration must not silently replace this ledger owner.
                    result_reconf=subprocess.run([str(PREFIX/'bin/scontrol'),'reconfigure'], env=env,
                                                   capture_output=True,text=True,timeout=10)
                    if result_reconf.returncode == 0: raise RuntimeError('unsafe reconfiguration accepted')
                    os.kill(proc.pid, signal.SIGHUP); time.sleep(.5)
                    if proc.poll() is not None: raise RuntimeError('SIGHUP stopped the ledger owner')
                    if not rpc(endpoint,'SNAP2 1001').startswith('OK 2 '): raise RuntimeError('SIGHUP lost ledger')
                fields=result.split()
                if len(fields)!=18 or fields[:2]!=['OK','2']: raise RuntimeError('invalid SNAP2 '+result)
                if int(fields[3]) != (0 if rollover else 3600) or int(fields[4]) != 0:
                    raise RuntimeError('grant missing or duplicated: '+result)
                if not cutover and rpc(endpoint,'READY1 0') != 'OK TIME26_SELECTABLE_ENDING2_ROLL1_RANK1_CANCEL_BOUND1_AUTOCANCEL1_RECONCILE1_NODEPROOF1_SELECT_DONE1_GPUMAP1_CANCEL_EMPTY1':
                    raise RuntimeError('controller readiness missing after recovery')
                if not cutover:
                    tombstone=rpc(endpoint,'RESERVE2_GET 1001 '+('f'*32)).split()
                    if len(tombstone)!=12 or tombstone[-2]!='4': raise RuntimeError('absent cancellation tombstone lost on restart')
                    if rpc(endpoint,'RESERVE1 1001 '+('f'*32)+' 2 1 1').startswith('OK'):
                        raise RuntimeError('replayed tombstone allowed late reservation')
                if rollover and not cutover:
                    result_hold=rpc(endpoint,'RESERVE2_GET 1001 '+'c'*32).split()
                    if len(result_hold)!=12 or result_hold[3]!=next_month.strftime('%Y%m') or result_hold[-2]!='0':
                        raise RuntimeError('rolled reservation did not recover: '+' '.join(result_hold))
                return int(fields[15])  # WAL sequence in protocol v2
        finally:
            if proc is not None and proc.poll() is None:
                os.killpg(proc.pid,signal.SIGTERM)
                try: proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGKILL); proc.wait(timeout=5)
            os.close(fd);os.close(proof_fd)
    first=cycle(True); second=cycle(False)
    if first != second or first < 6: raise RuntimeError('recovered WAL sequence differs')
    # Corrupt only this isolated fixture. Both the ledger and durable node
    # proof must fail closed instead of advertising a usable control socket.
    corrupt_proof=os.environ.get('TIME_TEST_CORRUPT_PROOF') == '1'
    corrupt_path=(base/'node-proof.wal') if corrupt_proof else (state/'monthly-quota.wal')
    with corrupt_path.open('ab') as wal:
        wal.write(b'corrupt\n'); wal.flush(); os.fsync(wal.fileno())
    fd=os.open(base/'witness',os.O_RDWR|os.O_APPEND|os.O_NOFOLLOW)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); os.set_inheritable(fd,True)
    proof_fd=os.open(base/'node-proof.wal',os.O_RDONLY|os.O_NOFOLLOW);os.set_inheritable(proof_fd,True)
    env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','SLURM_CONF':str(config),
         'TIME_QUOTA_WITNESS_FD':str(fd),'TIME_QUOTA_NODE_PROOF_FD':str(proof_fd), 'TIME_QUOTA_NODE_PIDFD':str(nodepid_fd),'TIME26_TEST_GPU_INFO_DIR':str(inventory),'LD_LIBRARY_PATH':str(PREFIX/'lib/slurm'),**shifted_env}
    proc=None
    try:
        with logpath.open('ab',buffering=0) as log:
            proc=subprocess.Popen([str(PREFIX/'sbin/slurmctld'),'-D','-f',str(config)],cwd='/',env=env,
                                  stdin=subprocess.DEVNULL,stdout=log,stderr=log,pass_fds=(fd,proof_fd,nodepid_fd),start_new_session=True)
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if proc.poll() is not None: raise RuntimeError('corrupt-ledger controller exited unexpectedly')
                check=subprocess.run([str(PREFIX/'bin/scontrol'),'ping'],env=env,capture_output=True,text=True,timeout=3)
                if check.returncode==0: break
                time.sleep(.2)
            else: raise RuntimeError('corrupt-ledger controller did not start')
            time.sleep(.5)
            if (state/'time-quota.sock').exists():
                try: value=rpc(state/'time-quota.sock','SNAP2 1001')
                except (OSError,RuntimeError): value='unavailable'
                if value.startswith('OK '): raise RuntimeError('corrupt ledger reported ready')
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired: os.killpg(proc.pid,signal.SIGKILL); proc.wait(timeout=5)
        os.close(fd);os.close(proof_fd)
    os.close(nodepid_fd)
    print('PASS: corrupt node proof fails closed' if corrupt_proof else 'PASS: real broker reservation, rollover' if rollover else 'PASS: real broker reservation, capacity retry, cancellation, grant, restart and corrupt-ledger fail-closed', first)


if __name__ == '__main__':
    try: main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print('isolated fixture failed:',exc,file=sys.stderr); sys.exit(1)
