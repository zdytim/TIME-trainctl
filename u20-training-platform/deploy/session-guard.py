#!/usr/bin/python3
"""Fail closed for enrolled login sessions that escaped their systemd user slice."""
import grp
import glob
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess


def isolated_session(uid, cgroups, properties, device_rules, gpu_devices):
    """Require the effective slice policy and devices hierarchy for this session."""
    if properties.get('DevicePolicy') != 'closed' or not gpu_devices:
        return False
    unit_path = properties.get('ControlGroup', '')
    expected = 'user-%d.slice' % uid
    if not unit_path.startswith('/') or unit_path.rstrip('/').split('/')[-1] != expected:
        return False
    for line in cgroups.splitlines():
        parts = line.split(':', 2)
        if len(parts) != 3 or 'devices' not in parts[1].split(','):
            continue
        path = parts[2].rstrip('/')
        if not (path == unit_path or path.startswith(unit_path.rstrip('/') + '/')):
            return False
        for rule in device_rules.splitlines():
            match = re.fullmatch(r'([abc])\s+(\*|[0-9]+):(\*|[0-9]+)\s+([rwm]+)', rule.strip())
            if not match:
                return False
            kind, major, minor, _permissions = match.groups()
            if kind in ('a', 'c') and any((major == '*' or int(major) == maj) and
                                         (minor == '*' or int(minor) == min_) for maj, min_ in gpu_devices):
                return False
        return bool(device_rules.strip())
    # This policy is currently designed for u20's cgroup v1 devices controller.
    return False

def main():
    if os.environ.get('PAM_TYPE') not in ('open_session', None):
        return 0
    name = os.environ.get('PAM_USER','')
    try:
        user = pwd.getpwnam(name)
        memberships = set(os.getgrouplist(name,user.pw_gid))
        training = grp.getgrnam('pro6000d-training').gr_gid
        admin = grp.getgrnam('pro6000d-admin').gr_gid
    except KeyError:
        return 0
    if training not in memberships or admin in memberships:
        return 0
    try:
        query = subprocess.run(['/usr/bin/systemctl', 'show', 'user-%d.slice' % user.pw_uid,
                                '--property=DevicePolicy',
                                '--property=ControlGroup', '--no-pager'],
                               check=True, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=3)
        properties = dict(line.split('=', 1) for line in query.stdout.splitlines() if '=' in line)
        cgroups = Path('/proc/self/cgroup').read_text()
        device_paths = [line.split(':', 2)[2] for line in cgroups.splitlines()
                        if len(line.split(':', 2)) == 3 and 'devices' in line.split(':', 2)[1].split(',')]
        if len(device_paths) != 1 or not device_paths[0].startswith('/') or '..' in Path(device_paths[0]).parts:
            return 1
        rules = Path('/sys/fs/cgroup/devices', device_paths[0].lstrip('/'), 'devices.list').read_text()
        gpu_devices = set()
        for name in glob.glob('/dev/nvidia*') + glob.glob('/dev/nvidia-caps/*'):
            info = os.stat(name)
            if stat.S_ISCHR(info.st_mode):
                gpu_devices.add((os.major(info.st_rdev), os.minor(info.st_rdev)))
        return 0 if isolated_session(user.pw_uid, cgroups, properties, rules, gpu_devices) else 1
    except (OSError, subprocess.SubprocessError, ValueError):
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
