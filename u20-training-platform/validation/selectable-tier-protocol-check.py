#!/usr/bin/env python3
"""Validate SNAP2 parsing and rendering without a controller or live data."""
import importlib.util
from pathlib import Path

backend = Path(__file__).resolve().parents[1] / 'backend'
def load(name):
    spec = importlib.util.spec_from_file_location(name, backend / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
bridge, view = load('controller_bridge'), load('controller_view')
client = bridge.ControllerBridge('/unused', 1)
reply = 'OK 2 202611 0 0 0 0 3600 0 0 0 0 0 1793500000 0 5 1 1'
client._exchange = lambda msg: reply if msg == 'SNAP2 1001' else 'bad'
snapshot = client.selectable_snapshot(1001)
assert snapshot['tiers']['important']['used']['gpu_seconds'] == 3600
assert snapshot['active_allocation_count'] == 1 and snapshot['wal_sequence'] == 5
shown = view.selectable_snapshot_view(snapshot, activation_verified=True)
assert shown['selectable_tiers_ready'] is True
assert shown['tiers'][0]['remaining_gpu_seconds'] == 5 * 3600
assert shown['tiers'][1]['used_gpu_seconds'] == 3600
assert view.selectable_snapshot_view(snapshot)['selectable_tiers_ready'] is False
for invalid in ('ERROR unavailable_or_invalid', reply + ' 0', reply.replace('202611', '202613'),
                reply.replace('3600 0', '3600 1000000000')):
    client._exchange = lambda msg, value=invalid: value
    try:
        client.selectable_snapshot(1001)
        raise AssertionError('invalid reply accepted')
    except bridge.ControllerUnavailable:
        pass
print('PASS: selected-tier snapshot protocol, rendering and fail-closed malformed replies')
