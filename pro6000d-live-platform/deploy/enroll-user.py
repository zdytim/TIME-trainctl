#!/usr/bin/python3
"""Enroll an existing, logged-out, unprivileged Linux account for scheduled GPU use."""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import re
import subprocess

def run(argv, check=True):
    return subprocess.run(argv, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('username')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('root required')
    user = pwd.getpwnam(args.username)
    if user.pw_uid < 1000 or user.pw_uid == 65534:
        raise SystemExit('Only ordinary human accounts may be enrolled')
    groups = {grp.getgrgid(gid).gr_name for gid in os.getgrouplist(user.pw_name, user.pw_gid)}
    forbidden = {'sudo','admin','root','docker','lxd','incus-admin','disk','video','render','pro6000d-admin','pro6000d-gpu'}
    if groups & forbidden:
        raise SystemExit('Account has privileged groups: ' + ', '.join(sorted(groups & forbidden)))
    processes = run(['pgrep','-u',str(user.pw_uid)], check=False)
    if processes.returncode == 0:
        raise SystemExit('Account still has running processes; log it out first. Nothing changed.')
    # Some sudo releases return zero even when listing an explicit denial.
    # Accept only that exact, locale-independent denial; errors or grants must
    # never silently qualify an account for the ordinary-user boundary.
    env = dict(os.environ, LC_ALL='C', LANG='C', LANGUAGE='C')
    sudo = subprocess.run(['/usr/bin/sudo','-n','-l','-U',user.pw_name],
                          check=False, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env)
    response = '\n'.join(part.strip() for part in (sudo.stdout, sudo.stderr) if part.strip())
    denial = r'User ' + re.escape(user.pw_name) + r' is not allowed to run sudo on [^\s]+\.?'
    if sudo.returncode not in (0, 1) or not re.fullmatch(denial, response):
        raise SystemExit('Cannot confirm absence of sudo grants; review explicit sudo permissions before enrollment')
    with open('/etc/pro6000d-training/config.json') as stream:
        config = json.load(stream)
    group = config.get('training_group','pro6000d-training')
    target = Path('/etc/systemd/system/user-%d.slice.d/80-pro6000d-training.conf' % user.pw_uid)
    expected = '[Slice]\nDevicePolicy=closed\n'
    if target.exists() and target.read_text() != expected:
        raise SystemExit('Refusing to overwrite an unrelated slice override')
    registry = Path('/var/lib/pro6000d-training/enrolled-users.json')
    enrolled = json.loads(registry.read_text()) if registry.exists() else {}
    if user.pw_name not in enrolled:
        enrolled[user.pw_name] = {'uid':user.pw_uid,'slice_file':str(target),'was_member':group in groups}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(expected); os.chmod(target,0o644)
    run(['usermod','-a','-G',group,user.pw_name])
    run(['systemctl','daemon-reload'])
    run(['/usr/local/libexec/pro6000d-gpu-acl'])
    registry.write_text(json.dumps(enrolled,indent=2)+'\n'); os.chmod(registry,0o600)
    print('Enrolled %s. New login required; use the training page to launch GPU jobs.' % user.pw_name)

if __name__ == '__main__':
    main()
