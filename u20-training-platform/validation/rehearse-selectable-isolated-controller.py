#!/usr/bin/env python3
"""Exercise a versioned controller in a separate fake cluster; no real GPU jobs.

Run as root on u20 only. Uses new ports, state, witness, config, and fake node;
never reads or changes the live controller state, config, sockets or services.
Evidence is retained in the newly created private probe directory.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import pwd
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

CONTROLLER = Path('/opt/time/slurm19-f417af53729e/sbin/slurmctld')
EXPECTED = '47e8210f486f1889a1cf88613ab4a0b5ecc53a003ebb1bc9ab330809ac9db5eb'
PLUGIN_DIR = '/opt/time/slurm19-f417af53729e/lib/slurm'
HEADER = b'U20MQ1|HEADER|1\n'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    if port in (6817, 6818) or port < 1024:
        raise RuntimeError('unsafe selected port')
    return port


def write_new(path, data, mode, uid=None, gid=None):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('short write')
            view = view[written:]
        os.fchmod(fd, mode)
        if uid is not None:
            os.fchown(fd, uid, gid)
        os.fsync(fd)
    finally:
        os.close(fd)


def rpc(path, service_uid, message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect(str(path))
        pid, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != service_uid or pid <= 0:
            raise RuntimeError('unexpected isolated controller socket peer')
        sock.sendall(message.encode('ascii') + b'\n')
        data = bytearray()
        while len(data) < 1024 and b'\n' not in data:
            chunk = sock.recv(512)
            if not chunk:
                break
            data.extend(chunk)
        if not data.endswith(b'\n') or data.count(b'\n') != 1:
            raise RuntimeError('incomplete controller response')
        return data.decode('ascii').strip()


def main():
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise RuntimeError('requires root on u20')
    if CONTROLLER.is_symlink() or not CONTROLLER.is_file() or hashlib.sha256(CONTROLLER.read_bytes()).hexdigest() != EXPECTED:
        raise RuntimeError('candidate controller identity mismatch')
    user = pwd.getpwnam('slurm')
    base = Path('/var/lib') / ('time-v2-isolated-' + uuid.uuid4().hex)
    base.mkdir(mode=0o755)
    state = base / 'state'; state.mkdir(mode=0o700); os.chown(state, user.pw_uid, user.pw_gid)
    spool = base / 'spool'; spool.mkdir(mode=0o700); os.chown(spool, user.pw_uid, user.pw_gid)
    digest = hashlib.sha256(HEADER).hexdigest().encode('ascii')
    write_new(state/'monthly-quota.wal', HEADER, 0o600, user.pw_uid, user.pw_gid)
    write_new(state/'monthly-quota.checkpoint', b'U20MQ1|CHECKPOINT|0|' + digest + b'\n', 0o600, user.pw_uid, user.pw_gid)
    witness = base/'witness.log'; write_new(witness, b'U20MQA1|0|' + digest + b'\n', 0o600)
    ctl_port, node_port = free_port(), free_port()
    if ctl_port == node_port:
        raise RuntimeError('isolated ports collide')
    hostname = os.uname().nodename
    config = base/'slurm.conf'
    text = f'''ClusterName=time-isolated-{uuid.uuid4().hex[:8]}
SlurmctldHost={hostname}(127.0.0.1)
SlurmUser=slurm
SlurmctldPort={ctl_port}
SlurmdPort={node_port}
StateSaveLocation={state}
SlurmctldPidFile={state}/controller.pid
SlurmdPidFile={spool}/slurmd.pid
SlurmdSpoolDir={spool}
PluginDir={PLUGIN_DIR}
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
SlurmctldTimeout=30
SlurmdTimeout=30
MessageTimeout=2
GresTypes=gpu
NodeName=time-fake-node CPUs=1 RealMemory=256 Gres=gpu:1 State=UNKNOWN
PartitionName=isolated Nodes=time-fake-node Default=YES MaxTime=01:00:00 State=UP
'''
    write_new(config, text.encode(), 0o644)
    logpath = base/'controller.log'
    print('isolated evidence directory:', base, flush=True)
    uid = user.pw_uid
    def cycle(cutover):
        fd = os.open(str(witness), os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, True)
        env = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'LANG':'C',
               'TIME_QUOTA_WITNESS_FD':str(fd), 'SLURM_CONF':str(config)}
        proc = None
        try:
            with logpath.open('ab', buffering=0) as log:
                proc = subprocess.Popen(['/usr/bin/timeout', '--kill-after=5s', '60s',
                                         str(CONTROLLER), '-D', '-f', str(config)],
                                        cwd='/', env=env, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=log, pass_fds=(fd,),
                                        start_new_session=True)
                endpoint = state/'time-quota.sock'
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError('isolated controller exited: %s' % proc.returncode)
                    try:
                        line = rpc(endpoint, uid, 'SNAP2 1001' if not cutover else 'SNAP 1001')
                        if line.startswith('OK '):
                            break
                    except (OSError, RuntimeError):
                        pass
                    time.sleep(.2)
                else:
                    raise RuntimeError('isolated controller did not become ready')
                if cutover:
                    if rpc(endpoint, uid, 'CUTOVER2_RESET_USAGE 0') != 'OK':
                        raise RuntimeError('isolated cutover refused')
                    line = rpc(endpoint, uid, 'SNAP2 1001')
                fields = line.split()
                if len(fields) != 18 or fields[:2] != ['OK', '2']:
                    raise RuntimeError('unexpected isolated SNAP2 response: '+line)
                numbers = [int(x) for x in fields[2:]]
                if any(numbers[i] != 0 for i in (1,2,3,4,5,6,7,8,9,10,14,15)):
                    raise RuntimeError('new selectable account is not empty')
                return numbers[13]
        finally:
            if proc is not None and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            os.close(fd)
    first = cycle(True)
    second = cycle(False)
    if first != second or first < 1:
        raise RuntimeError('cutover journal sequence changed unexpectedly across restart')
    print('PASS: isolated controller cutover and restart; WAL sequence', first, flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Isolated rehearsal failed; preserve its evidence: '+str(exc), file=sys.stderr)
        sys.exit(1)
