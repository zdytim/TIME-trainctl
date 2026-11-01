#!/usr/bin/env python3
"""Exercise selectable snapshot view with fabricated data; no live service."""
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'backend/controller_view.py'
spec = importlib.util.spec_from_file_location('tier_view', path)
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)

def amount(seconds):
    return {'gpu_seconds': seconds, 'nanoseconds': 0}

def snapshot():
    return {'schema_version': 2, 'month': 202611,
            'granted': amount(0), 'overage': amount(10),
            'tiers': {'debug': {'used': amount(0)},
                      'important': {'used': amount(100 * 3600)},
                      'normal': {'used': amount(500 * 3600)}}}

candidate = snapshot()
result = view.selectable_snapshot_view(candidate, activation_verified=True)
assert result['selectable_tiers_ready'] is True
assert result['remaining_gpu_seconds'] == 5 * 3600
assert result['overage_gpu_seconds'] == 10
assert result['tiers'][0]['used_gpu_seconds'] == 0
assert result['current_tier'] is None
assert view.selectable_snapshot_view(candidate)['selectable_tiers_ready'] is False
bad = snapshot()
bad['tiers']['normal']['used'] = amount(1)
try:
    view.selectable_snapshot_view(bad, activation_verified=True)
    raise AssertionError('overage before normal exhaustion accepted')
except ValueError:
    pass
bad = snapshot()
del bad['tiers']['important']
try:
    view.selectable_snapshot_view(bad, activation_verified=True)
    raise AssertionError('missing tier accepted')
except ValueError:
    pass
try:
    view.selectable_snapshot_view({'month': 202611}, activation_verified=True)
    raise AssertionError('legacy total accepted as selectable snapshot')
except ValueError:
    pass
print('PASS: chosen tiers, staged readiness, malformed balances and legacy snapshot')
