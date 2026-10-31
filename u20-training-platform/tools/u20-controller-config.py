#!/usr/bin/env python3
"""Build a controller-only candidate config in memory; never writes or activates it."""
import hashlib
import json
import re
import sys

PLUGIN_DIR = '/opt/time/slurm19-7a06cfff70a7/lib/slurm'
EXPECTED = {
    'clustername': 'u20-training',
    'slurmuser': 'slurm',
    'slurmduser': 'root',
    'authtype': 'auth/munge',
    'credtype': 'cred/munge',
    'statesavelocation': '/var/lib/slurm/ctld',
    'accountingstoragetype': 'accounting_storage/none',
    'selecttype': 'select/cons_tres',
    'schedulertype': 'sched/backfill',
    'jobsubmitplugins': 'lua',
    'taskplugin': 'task/cgroup',
    'proctracktype': 'proctrack/cgroup',
    'grestypes': 'gpu',
}


def candidate_config(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > 1024 * 1024:
        raise ValueError('configuration must be between 1 byte and 1 MiB')
    text = raw.decode('utf-8')
    if '\x00' in text or '\r' in text:
        raise ValueError('unsupported control character or line ending')
    seen = {}
    for line in text.splitlines():
        content = line.split('#', 1)[0].strip()
        if not content:
            continue
        if '\\' in content:
            raise ValueError('continued or escaped configuration is unsupported')
        match = re.match(r'^([A-Za-z][A-Za-z0-9_]*)\s*(?:=|\s)', content)
        if not match:
            raise ValueError('unrecognized configuration line')
        key = match.group(1).lower()
        if key == 'include':
            raise ValueError('included configuration cannot be safely copied')
        if key in ('plugindir', 'slurmctldparameters'):
            raise ValueError('controller-specific setting already exists: ' + key)
        if key in EXPECTED:
            value = re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*\s*=\s*(\S+)\s*', content)
            if not value or key in seen:
                raise ValueError('ambiguous baseline setting: ' + key)
            seen[key] = value.group(1)
    for key, expected in EXPECTED.items():
        if seen.get(key) != expected:
            raise ValueError('unexpected or missing baseline setting: ' + key)
    separator = '' if text.endswith('\n') else '\n'
    return (text + separator + 'PluginDir=' + PLUGIN_DIR + '\n'
            + 'SlurmctldParameters=MonthlyGpuQuota\n').encode('utf-8')


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        result = candidate_config(raw)
        print(json.dumps({'baseline_sha256': hashlib.sha256(raw).hexdigest(),
                          'candidate_sha256': hashlib.sha256(result).hexdigest(),
                          'candidate_bytes': len(result),
                          'installed': False, 'enabled': False}))
    except (UnicodeError, ValueError) as exc:
        print('Candidate config refused: ' + str(exc), file=sys.stderr)
        sys.exit(1)
