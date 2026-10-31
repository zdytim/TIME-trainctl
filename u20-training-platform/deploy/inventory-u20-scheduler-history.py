#!/usr/bin/env python3
"""Read-only inventory of retained Slurm and platform history before cutover.

This is a point-in-time observation, not a submission fence or a backup.
It intentionally reports counts only, never job scripts, paths, or environment.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys

TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY',
            'NODE_FAIL', 'BOOT_FAIL', 'DEADLINE', 'PREEMPTED', 'EXPIRED'}


def parse_jobs(output):
    jobs = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        match = re.match(r'^JobId=(\d+)\s', line)
        state = re.search(r'(?:^|\s)JobState=([A-Z_]+)(?:\s|$)', line)
        if not match or not state or match.group(1) in jobs:
            raise ValueError('unrecognised or duplicate Slurm job record')
        jobs[match.group(1)] = state.group(1)
    return jobs


def main():
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise ValueError('root on u20 is required')
    proc = subprocess.run(['/usr/bin/scontrol', 'show', 'jobs', '-o'],
                          capture_output=True, text=True, timeout=15, check=True)
    jobs = parse_jobs(proc.stdout)
    if any(state not in TERMINAL for state in jobs.values()):
        raise ValueError('Slurm has nonterminal or unrecognised jobs')
    queue = subprocess.run(['/usr/bin/squeue', '-h', '-o', '%i'],
                           capture_output=True, text=True, timeout=15, check=True)
    if queue.stdout.strip():
        raise ValueError('Slurm queue is not empty')
    with sqlite3.connect('file:/var/lib/pro6000d-training/state.sqlite3?mode=ro',
                         uri=True, timeout=5) as db:
        rows = db.execute('SELECT scheduler_id, state FROM jobs').fetchall()
    if any(state not in TERMINAL for _, state in rows):
        raise ValueError('platform has nonterminal or unrecognised jobs')
    ids = [str(sid) for sid, _ in rows if sid is not None]
    if any(not re.fullmatch(r'\d+', sid) for sid in ids) or len(ids) != len(set(ids)):
        raise ValueError('platform scheduler identifiers need reconciliation')
    print(json.dumps({'slurm_retained_terminal_jobs': len(jobs),
                      'platform_terminal_jobs': len(rows),
                      'platform_jobs_without_scheduler_id': len(rows) - len(ids),
                      'matching_scheduler_ids': len(set(ids) & set(jobs)),
                      'slurm_only_scheduler_ids': len(set(jobs) - set(ids)),
                      'platform_only_scheduler_ids': len(set(ids) - set(jobs)),
                      'fenced': False, 'history_deleted': False}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print('Inventory refused: ' + str(exc), file=sys.stderr)
        sys.exit(1)
