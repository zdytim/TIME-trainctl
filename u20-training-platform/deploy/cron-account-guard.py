#!/usr/bin/python3 -I
"""Deny non-admin human cron accounts; scheduled training goes through Slurm."""
import grp
import os
from pathlib import Path
import pwd
import syslog


def deny_human(name, uid):
    # Log only account identity, never command or environment. Logging failure
    # must not turn a denied account into an allowed one.
    try:
        syslog.openlog('time-cron-guard', syslog.LOG_PID, syslog.LOG_AUTHPRIV)
        syslog.syslog(syslog.LOG_WARNING, 'denied uid=%d user=%r' % (uid, name[:128]))
    except (OSError, ValueError):
        pass
    return 1


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
        return 0 if admin in os.getgrouplist(name, user.pw_gid) else deny_human(name, user.pw_uid)
    except (KeyError, OSError):
        return deny_human(name, user.pw_uid)


if __name__ == '__main__':
    raise SystemExit(main())
