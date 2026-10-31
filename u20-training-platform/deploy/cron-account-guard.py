#!/usr/bin/python3 -I
"""Deny non-admin human cron accounts; scheduled training goes through Slurm."""
import grp
import os
from pathlib import Path
import pwd


def main():
    # This helper is only valid for PAM account management.
    if os.environ.get('PAM_TYPE') != 'account':
        return 1
    name = os.environ.get('PAM_USER', '')
    try:
        user = pwd.getpwnam(name)
    except KeyError:
        return 1
    if user.pw_uid < 1000 or Path(user.pw_shell).name in ('nologin', 'false', 'sync', 'shutdown', 'halt'):
        return 0
    try:
        admin = grp.getgrnam('pro6000d-admin').gr_gid
        return 0 if admin in os.getgrouplist(name, user.pw_gid) else 1
    except (KeyError, OSError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
