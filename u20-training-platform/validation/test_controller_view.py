#!/usr/bin/env python3
"""Check controller-to-console accounting and unavailable-state behavior."""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location('controller_view', sys.argv[1])
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)

def amount(ns):
    return {'gpu_seconds': ns // view.NANO, 'nanoseconds': ns % view.NANO}

def snapshot(seconds, tier, grant=0, fraction=0):
    used = seconds * view.NANO + fraction
    total = (605 * 3600 + grant) * view.NANO
    return {'month': 202610, 'tier': tier, 'used': amount(used), 'granted': amount(grant * view.NANO),
            'capacity': amount(total), 'remaining': amount(max(0, total-used)),
            'overage': amount(max(0, used-total))}

staged = view.snapshot_view(snapshot(0, 'debug'))
assert staged['month'] == '2026-10' and not staged['enforcement_ready'] and staged['mode'] == 'staged'
current = view.snapshot_view(snapshot(6 * 3600, 'important'), True)
assert current['enforcement_ready'] and current['current_tier'] == 'important'
assert current['tiers'][0]['remaining_gpu_seconds'] == 0
assert current['tiers'][1]['used_gpu_seconds'] == 3600
over = view.snapshot_view(snapshot(607 * 3600, 'exhausted'), True)
assert over['remaining_gpu_seconds'] == 0 and over['overage_gpu_seconds'] == 7200
granted = view.snapshot_view(snapshot(607 * 3600, 'normal', grant=3 * 3600, fraction=1), True)
assert granted['additional_gpu_seconds'] == 10800 and 3599 < granted['remaining_gpu_seconds'] < 3600
assert not view.unavailable(True)['enforcement_ready'] and not view.unavailable(True)['request_available']
for bad in (dict(snapshot(0, 'debug'), tier='normal'), dict(snapshot(0, 'debug'), month=202613),
            dict(snapshot(0, 'debug'), capacity=amount(1))):
    try:
        view.snapshot_view(bad, True)
        raise AssertionError('inconsistent snapshot was accepted')
    except ValueError:
        pass
print('PASS: staged state, tier accounting, overage, grants and invalid snapshots')
