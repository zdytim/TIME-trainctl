#!/usr/bin/env python3
"""Open the root-owned ledger witness, then exec slurmctld as the Slurm user.

This is installed as the systemd ExecStart only during a reviewed quota release.
It never creates or repairs a witness; an absent or untrusted witness prevents
startup instead of silently resetting accounting history.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import select
import stat
import sys

WITNESS_DIR = Path('/var/lib/pro6000d-monthly')
WITNESS = WITNESS_DIR / 'witness'
NODE_PROOF = WITNESS_DIR / 'node-proof.wal'
NODE_READY = WITNESS_DIR / 'node-ready'
CONTROLLER = '/opt/slurm/current/sbin/slurmctld'
SLURM_CONFIG = Path('/etc/slurm/slurm.conf')
GRES_CONFIG = Path('/etc/slurm/gres.conf')
CGROUP_CONFIG = Path('/etc/slurm/cgroup.conf')
NODE_DAEMON = '/opt/slurm/current/sbin/slurmd'
RELEASE_MANIFEST = Path('/etc/pro6000d-training/monthly-release.json')
CONTROLLER_CAPABILITY = b'PRO6000D_MANDATORY_GPU_ALLOCATION_GATE_BUILD_V1'
NODE_CAPABILITY = b'PRO6000D_DURABLE_NODE_CLEANUP_PROOF_BUILD_V1'


def _validate_slurm_config():
    # The wrapper exists only for the quota-enabled service.  Do not launch a
    # controller that could allocate GPUs without the mandatory gate.
    path = SLURM_CONFIG
    if not path.is_absolute():
        raise SystemExit('Slurm configuration path must be absolute')
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit('Slurm configuration directory is not root-managed')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
            raise SystemExit('Slurm configuration is not root-managed')
        if info.st_size > 1024 * 1024:
            raise SystemExit('Slurm configuration is too large')
        parameters = None
        for line in stream:
            line = line.split('#', 1)[0].strip()
            if re.match(r'^include(?:\s|=)', line, re.IGNORECASE):
                raise SystemExit('included Slurm configuration is not supported')
            match = re.fullmatch(r'SlurmctldParameters\s*=\s*(\S+)', line, re.IGNORECASE)
            if match:
                if parameters is not None:
                    raise SystemExit('duplicate SlurmctldParameters')
                parameters = match.group(1).lower().split(',')
        if parameters is None or 'monthlygpuquota' not in parameters:
            raise SystemExit('Slurm configuration lacks mandatory MonthlyGpuQuota')
    return str(path)


def _validate_release(executable, config, role="controller"):
    # The manifest is installed only after the paired candidate has been
    # reviewed and tested.  A root-owned *old* controller is not sufficient:
    # it might ignore the quota token entirely.
    path = RELEASE_MANIFEST
    if not path.is_absolute():
        raise SystemExit('release manifest path must be absolute')
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit('release manifest directory is not root-managed')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1 or info.st_size > 32768:
            raise SystemExit('release manifest is not root-managed')
        try:
            manifest = json.load(stream)
            if manifest.get('version') != 1:
                raise ValueError('unsupported manifest')
            for name, actual in ((role, executable), ('slurm_conf', Path(config)), ('gres_conf', GRES_CONFIG), ('cgroup_conf', CGROUP_CONFIG)):
                entry = manifest['files'][name]
                if set(entry) != {'path', 'sha256'} or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
                    raise ValueError('invalid artifact entry')
                if Path(entry['path']).resolve(strict=True) != actual:
                    raise ValueError('artifact path mismatch')
                digest = hashlib.sha256()
                marker_found = False
                tail = b''
                with actual.open('rb') as artifact:
                    info = os.fstat(artifact.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
                        raise ValueError('artifact is not root-managed')
                    for block in iter(lambda: artifact.read(1024 * 1024), b''):
                        digest.update(block)
                        if name == role:
                            marker = CONTROLLER_CAPABILITY if role == 'controller' else NODE_CAPABILITY
                            marker_found |= marker in tail + block
                            tail = (tail + block)[-len(marker):]
                if digest.hexdigest() != entry['sha256'] or (name == role and not marker_found):
                    raise ValueError('artifact digest or controller capability mismatch')
                if name == 'gres_conf':
                    for line in actual.read_text().splitlines():
                        if re.match(r'^\s*include(?:\s|=)', line, re.IGNORECASE):
                            raise ValueError('included GPU configuration is not supported')
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise SystemExit('release manifest does not match controller/configuration') from exc


def _open_verified_node_pidfd(config):
    # A pinned file on disk is insufficient: the running daemon must be the
    # one that checked Slurm's effective GPU map. pidfd prevents PID reuse.
    fd = os.open(NODE_READY, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 256:
            raise ValueError('untrusted node readiness record')
        data = os.read(fd, 257).decode('ascii').strip().split('|')
    finally:
        os.close(fd)
    if len(data) != 4 or data[0] != 'P6NR1' or not re.fullmatch(r'[1-9][0-9]*', data[1]) or not re.fullmatch(r'[1-9][0-9]*', data[2]):
        raise ValueError('invalid node readiness record')
    pid = int(data[1])
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if data[3] != boot:
        raise ValueError('stale node readiness boot')
    pidfd = os.pidfd_open(pid, 0)
    try:
        proc = Path('/proc') / str(pid)
        raw = (proc/'stat').read_text(); close = raw.rfind(')')
        fields = raw[close+2:].split() if close >= 0 else []
        if len(fields) <= 19 or fields[19] != data[2]:
            raise ValueError('stale node readiness process')
        executable = (proc/'exe').resolve(strict=True)
        _validate_release(executable, config, role='node')
        env = dict(item.split(b'=',1) for item in (proc/'environ').read_bytes().split(b'\0') if b'=' in item)
        if (env.get(b'SLURM_CONF') != os.fsencode(config) or
                env.get(b'TIME_QUOTA_NODE_PROOF_FILE') != os.fsencode(NODE_PROOF) or
                env.get(b'TIME_QUOTA_NODE_READY_FILE') != os.fsencode(NODE_READY)):
            raise ValueError('node process environment does not match release')
        poll = select.poll(); poll.register(pidfd, select.POLLIN)
        if poll.poll(0):
            raise ValueError('node process has already exited')
        return pidfd
    except BaseException:
        os.close(pidfd)
        raise


def main():
    if os.geteuid() != 0:
        raise SystemExit('quota controller wrapper requires root')
    config = _validate_slurm_config()
    executable = Path(CONTROLLER).resolve(strict=True)
    info = executable.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('untrusted controller executable')
    for parent in executable.parents:
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit('controller executable directory is not root-managed')
    _validate_release(executable, config)
    directory = WITNESS_DIR.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0 or directory.st_mode & 0o077:
        raise SystemExit('witness directory must be root-owned and private')
    try:
        node_pidfd = _open_verified_node_pidfd(config)
    except (OSError, ValueError, UnicodeError) as exc:
        raise SystemExit('verified node process is not ready') from exc
    fd = os.open(WITNESS, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
            info.st_mode & 0o077 or info.st_nlink != 1):
        raise SystemExit('witness must be a private root-owned regular file')
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # A crash during append may leave only an incomplete final record.
    # Such a record was never a complete proof; remove only that tail under
    # the same lock as node writers, then sync before handing off read-only.
    repair_fd = os.open(NODE_PROOF, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    proof_info = os.fstat(repair_fd)
    if (not stat.S_ISREG(proof_info.st_mode) or proof_info.st_uid != 0 or
            proof_info.st_mode & 0o077 or proof_info.st_nlink != 1 or
            os.pread(repair_fd, len(b'P6NP1|HEADER|1\n'), 0) != b'P6NP1|HEADER|1\n'):
        raise SystemExit('node proof must be a private root-owned ledger')
    fcntl.flock(repair_fd, fcntl.LOCK_EX)
    # Re-stat only after acquiring the writer lock; another node completion
    # may have appended between open and lock acquisition.
    proof_info = os.fstat(repair_fd)
    if os.pread(repair_fd, 1, proof_info.st_size - 1) != b'\n':
        tail_len = min(proof_info.st_size, 512)
        tail = os.pread(repair_fd, tail_len, proof_info.st_size - tail_len)
        last_newline = tail.rfind(b'\n')
        if last_newline < 0:
            raise SystemExit('node proof tail is too long or has no complete header')
        os.ftruncate(repair_fd, proof_info.st_size - tail_len + last_newline + 1)
    os.fsync(repair_fd)
    proof_fd = os.open(NODE_PROOF, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    reopened = os.fstat(proof_fd)
    if (reopened.st_dev, reopened.st_ino) != (proof_info.st_dev, proof_info.st_ino):
        raise SystemExit('node proof changed during handoff')
    fcntl.flock(repair_fd, fcntl.LOCK_UN)
    os.close(repair_fd)
    account = pwd.getpwnam('slurm')
    os.initgroups(account.pw_name, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
    os.set_inheritable(fd, True)
    os.set_inheritable(proof_fd, True)
    os.set_inheritable(node_pidfd, True)
    env = {key: value for key, value in os.environ.items()
           if key not in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME')}
    env['TIME_QUOTA_WITNESS_FD'] = str(fd)
    env['TIME_QUOTA_NODE_PROOF_FD'] = str(proof_fd)
    env['TIME_QUOTA_NODE_PIDFD'] = str(node_pidfd)
    env['SLURM_CONF'] = config
    os.execve(str(executable), [str(executable), '-D', '-f', config], env)


def node_main():
    if os.geteuid() != 0:
        raise SystemExit('quota node wrapper requires root')
    config = _validate_slurm_config()
    executable = Path(NODE_DAEMON).resolve(strict=True)
    info = executable.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('untrusted node executable')
    for parent in executable.parents:
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit('node executable directory is not root-managed')
    _validate_release(executable, config, role='node')
    directory = WITNESS_DIR.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0 or directory.st_mode & 0o077:
        raise SystemExit('proof directory must be root-owned and private')
    fd = os.open(NODE_PROOF, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or
                info.st_nlink != 1 or os.read(fd, len(b'P6NP1|HEADER|1\n')) != b'P6NP1|HEADER|1\n'):
            raise SystemExit('node proof must be a private root-owned ledger')
    finally:
        os.close(fd)
    env = {key: value for key, value in os.environ.items()
           if key not in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME')}
    env['SLURM_CONF'] = config
    env['TIME_QUOTA_NODE_PROOF_FILE'] = str(NODE_PROOF)
    env['TIME_QUOTA_NODE_READY_FILE'] = str(NODE_READY)
    os.execve(str(executable), [str(executable), '-D', '-f', config], env)


if __name__ == '__main__':
    if sys.argv[1:] == ['--node']:
        node_main()
    elif not sys.argv[1:]:
        main()
    else:
        raise SystemExit('invalid quota wrapper arguments')
