"""Verify paired, root-managed Slurm release artifacts before advertising quota.

The root-owned release manifest is written only by the reviewed deployment.
A healthy controller protocol alone cannot prove that the running node daemon
and loaded plugins implement the corresponding allocation and cleanup rules.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

REQUIRED = frozenset(('controller', 'node', 'submit', 'scheduler', 'select',
                      'cgroup', 'slurm_conf', 'cgroup_conf', 'gres_conf', 'node_proof'))
MAPPED = ('submit', 'scheduler', 'select')


def _trusted_file(path, owner, check_ancestors):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('release path must be absolute')
    path = path.resolve(strict=True)
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != owner or st.st_mode & 0o022:
        raise ValueError('release file is not root-managed')
    if check_ancestors:
        for parent in path.parents:
            info = parent.stat()
            if info.st_uid != owner or info.st_mode & 0o022:
                raise ValueError('release directory is not root-managed')
    return path, st


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _process_started_ns(proc):
    raw = (proc/'stat').read_text()
    close = raw.rfind(')')
    fields = raw[close + 2:].split()
    if close < 0 or len(fields) <= 19:
        raise ValueError('invalid process stat')
    ticks = int(fields[19])
    hz = os.sysconf('SC_CLK_TCK')
    boot_epoch_ns = time.time_ns() - time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    return boot_epoch_ns + ticks * 1_000_000_000 // hz


def _configured_path(proc):
    env = dict(item.split(b'=', 1) for item in (proc/'environ').read_bytes().split(b'\0') if b'=' in item)
    args = (proc/'cmdline').read_bytes().split(b'\0')
    path = env.get(b'SLURM_CONF')
    for i, arg in enumerate(args):
        if arg == b'-f' and i + 1 < len(args):
            path = args[i+1]
        elif arg.startswith(b'-f') and len(arg) > 2:
            path = arg[2:]
    if not path:
        raise ValueError('process has no explicit Slurm config')
    return Path(os.fsdecode(path)).resolve(strict=True)


def _maps(proc):
    mappings = set()
    for line in (proc/'maps').read_text().splitlines():
        fields = line.split(None, 5)
        if len(fields) >= 5 and ':' in fields[3] and fields[4].isdigit():
            major, minor = fields[3].split(':', 1)
            mappings.add((int(major, 16), int(minor, 16), int(fields[4])))
    return mappings


def _options(path):
    options = {}
    for line in path.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if re.match(r'^include(?:\s|=)', line, re.IGNORECASE):
            raise ValueError('included configuration requires independent verification')
        match = re.match(r'^([A-Za-z][A-Za-z0-9]*)\s*=\s*(\S+)', line)
        if match:
            key, value = match.group(1).lower(), match.group(2).lower()
            tracked = {'slurmctldparameters', 'jobsubmitplugins', 'proctracktype', 'taskplugin',
                       'schedulertype', 'selecttype', 'prologflags', 'cgroupplugin', 'constraindevices'}
            if key not in tracked:
                continue
            if key in options:
                raise ValueError('duplicate configuration option')
            options[key] = value
    return options


def verify_release(manifest, controller_uid, *, proc_root='/proc', owner=0, check_ancestors=True, return_controller_pid=False):
    """Return False on missing, changed, untrusted, or not-yet-running artifacts."""
    try:
        path, _ = _trusted_file(manifest, owner, check_ancestors)
        if path.stat().st_size > 32768:
            return False
        data = json.loads(path.read_text())
        files, pidfiles = data['files'], data['pidfiles']
        if data.get('version') != 1 or set(files) != REQUIRED or set(pidfiles) != {'controller', 'node'}:
            return False
        verified = {}
        for name, entry in files.items():
            if name == 'node_proof':
                if set(entry) != {'path'}: return False
                artifact, info = _trusted_file(entry['path'], owner, check_ancestors)
                if info.st_mode & 0o077 or info.st_nlink != 1: return False
                with artifact.open('rb') as proof:
                    if proof.read(len(b'P6NP1|HEADER|1\n')) != b'P6NP1|HEADER|1\n': return False
                verified[name] = (artifact, info, None)
                continue
            if set(entry) != {'path', 'sha256'} or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
                return False
            artifact, info = _trusted_file(entry['path'], owner, check_ancestors)
            if _digest(artifact) != entry['sha256']:
                return False
            verified[name] = (artifact, info, entry['sha256'])
        conf = _options(verified['slurm_conf'][0])
        cgroup_conf = _options(verified['cgroup_conf'][0])
        # GRES files support recursive Include; only a single pinned file is
        # accepted so its digest captures the complete device mapping.
        _options(verified['gres_conf'][0])
        if ('include' in conf or 'include' in cgroup_conf or
                'monthlygpuquota' not in conf.get('slurmctldparameters', '').split(',') or
                'pro6000d' not in conf.get('jobsubmitplugins', '').split(',') or
                conf.get('proctracktype') != 'proctrack/cgroup' or
                'task/cgroup' not in conf.get('taskplugin', '').split(',') or
                conf.get('schedulertype') != 'sched/backfill' or
                conf.get('selecttype') != 'select/cons_tres' or
                'runinjob' in conf.get('prologflags', '').split(',') or
                cgroup_conf.get('cgroupplugin') != 'cgroup/v2' or
                cgroup_conf.get('constraindevices') != 'yes' or
                verified['cgroup_conf'][0].parent != verified['slurm_conf'][0].parent or
                verified['gres_conf'][0].parent != verified['slurm_conf'][0].parent):
            return False
        controller_pid = None
        for role, uid in (('controller', controller_uid), ('node', 0)):
            pid_path = Path(pidfiles[role])
            if not pid_path.is_absolute():
                return False
            pid_text = pid_path.read_text().strip()
            if not re.fullmatch(r'[1-9][0-9]{0,9}', pid_text):
                return False
            if role == 'controller': controller_pid = int(pid_text)
            proc = Path(proc_root) / pid_text
            status = (proc/'status').read_text()
            match = re.search(r'^Uid:\s+(\d+)\s', status, re.MULTILINE)
            if (not match or int(match.group(1)) != uid or _digest(proc/'exe') != verified[role][2] or
                    _configured_path(proc) != verified['slurm_conf'][0]):
                return False
            started = _process_started_ns(proc)
            if any(verified[name][1].st_mtime_ns > started for name in ('slurm_conf', 'cgroup_conf', 'gres_conf')):
                return False
            if role == 'controller':
                proof_stat = verified['node_proof'][1]
                proof_open = False
                for descriptor in (proc/'fd').iterdir():
                    try:
                        opened = descriptor.stat()
                        if (opened.st_dev, opened.st_ino) != (proof_stat.st_dev, proof_stat.st_ino): continue
                        info = (proc/'fdinfo'/descriptor.name).read_text()
                        flags = re.search(r'^flags:\s+([0-7]+)', info, re.MULTILINE)
                        if flags and int(flags.group(1), 8) & os.O_ACCMODE == os.O_RDONLY:
                            proof_open = True; break
                    except (OSError, ValueError):
                        continue
                if not proof_open: return False
            else:
                env = dict(item.split(b'=',1) for item in (proc/'environ').read_bytes().split(b'\0') if b'=' in item)
                node_path = env.get(b'TIME_QUOTA_NODE_PROOF_FILE', b'/var/lib/pro6000d-monthly/node-proof.wal')
                if Path(os.fsdecode(node_path)).resolve(strict=True) != verified['node_proof'][0]: return False
            mappings = _maps(proc)
            for name in (MAPPED if role == 'controller' else ('cgroup',)):
                st = verified[name][1]
                if (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino) not in mappings:
                    return False
        return controller_pid if return_controller_pid else True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
        return False
