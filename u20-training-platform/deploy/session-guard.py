#!/usr/bin/python3
"""Fail closed for enrolled login sessions that escaped their systemd user slice."""
import grp
import os
from pathlib import Path
import pwd

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
    expected = 'user-%d.slice' % user.pw_uid
    return 0 if any(expected in line.split(':',2)[-1].split('/') for line in Path('/proc/self/cgroup').read_text().splitlines()) else 1

if __name__ == '__main__':
    raise SystemExit(main())
