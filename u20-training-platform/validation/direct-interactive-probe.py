#!/usr/bin/env python3
"""Check live Slurm rejects forged interactive submissions by timzhang.

All attempted jobs have unique names and a future start. If one is accepted,
only jobs matching the exact generated name and current user are cancelled.
"""
import os
import pwd
import re
import subprocess
import sys
import uuid

user = pwd.getpwuid(os.geteuid()).pw_name
if user != 'timzhang':
    raise SystemExit('run only as the designated timzhang test account')


def cleanup(name):
    queue = subprocess.run(['squeue', '--noheader', '-u', user, '-o', '%i|%j'],
                           capture_output=True, text=True, timeout=10, check=True)
    ids = []
    for line in queue.stdout.splitlines():
        fields = line.strip().split('|', 1)
        if len(fields) == 2 and fields[1] == name:
            if not re.fullmatch(r'[0-9]+', fields[0]):
                raise RuntimeError('unexpected matching job identity; inspect queue')
            ids.append(fields[0])
    if ids:
        subprocess.run(['scancel', *ids], capture_output=True, text=True,
                       timeout=10, check=True)
        raise RuntimeError('direct submission created job(s), cancelled: ' + ', '.join(ids))


failed = False
for command in ('srun', 'salloc'):
    name = 'time-direct-' + uuid.uuid4().hex
    marker = 'pro6000d-job:' + uuid.uuid4().hex
    argv = [command, '--immediate=2', '--begin=now+10minutes', '--gres=gpu:1',
            '--mem=64M', '--time=00:01:00', '--job-name=' + name,
            '--comment=' + marker, '/bin/true']
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 or 'Please submit through the Cockpit training platform' not in result.stderr:
            raise RuntimeError('unexpected result: exit %d, stderr %r' %
                               (result.returncode, result.stderr[-1000:]))
        print('PASS: forged platform marker rejected by ' + command, flush=True)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print('FAIL: ' + command + ': ' + str(exc), file=sys.stderr, flush=True)
        failed = True
    finally:
        try:
            cleanup(name)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            print('FAIL: cleanup: ' + str(exc), file=sys.stderr, flush=True)
            failed = True

raise SystemExit(1 if failed else 0)
