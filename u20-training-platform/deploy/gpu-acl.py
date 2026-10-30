#!/usr/bin/python3
"""Apply platform NVIDIA device ACL entries and deny unenrolled others."""
import glob
import grp
import json
import os
import pwd
import re
import stat
import subprocess
import sys

def main():
    if os.geteuid() != 0:
        raise SystemExit('root required')
    with open('/etc/pro6000d-training/config.json') as stream:
        config = json.load(stream)
    training = config.get('training_group', 'pro6000d-training')
    runtime = config.get('job_group', 'pro6000d-gpu')
    admin = grp.getgrnam(config.get('admin_group', 'pro6000d-admin'))
    admins = set(admin.gr_mem)
    admins.update(p.pw_name for p in pwd.getpwall() if p.pw_gid == admin.gr_gid)
    entries = ['g:%s:---' % training, 'g:%s:rw' % runtime, 'o::---']
    entries += ['u:%d:rw' % pwd.getpwnam(name).pw_uid for name in sorted(admins)]
    targets = sys.argv[1:] or glob.glob('/dev/nvidia*') + glob.glob('/dev/nvidia-caps/*')
    for path in targets:
        # Capability nodes can convey privileged driver operations. Never
        # widen those permissions, or the display/modeset device permissions.
        if not re.fullmatch(r'/dev/nvidia(?:[0-9]+|ctl|-uvm|-uvm-tools)', path) or os.path.islink(path):
            continue
        try:
            mode = os.stat(path).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISCHR(mode):
            subprocess.run(['/usr/bin/setfacl', '-m', ','.join(entries), '--', path], check=True)

if __name__ == '__main__':
    main()
