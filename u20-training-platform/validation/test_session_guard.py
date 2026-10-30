#!/usr/bin/env python3
"""Check the staged PAM guard's fail-closed policy without changing accounts."""
import importlib.util
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location('session_guard', Path(sys.argv[1]))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
cg = '8:devices:/user.slice/user-1001.slice/session-3.scope\n'
properties = {'DevicePolicy': 'closed', 'ControlGroup': '/user.slice/user-1001.slice'}
safe = 'c 1:3 rwm\nc 5:0 rw\n'
devices = {(195, 0), (195, 255), (507, 0)}
assert guard.isolated_session(1001, cg, properties, safe, devices)
for key, value in (('DevicePolicy', 'auto'),
                   ('ControlGroup', '/user.slice/user-1002.slice'), ('ControlGroup', '')):
    changed = dict(properties, **{key: value})
    assert not guard.isolated_session(1001, cg, changed, safe, devices), (key, value)
assert not guard.isolated_session(1001, '8:devices:/user.slice/user-1002.slice/session-3.scope\n', properties, safe, devices)
assert not guard.isolated_session(1001, '0::/user.slice/user-1001.slice/session-3.scope\n', properties, safe, devices)
assert not guard.isolated_session(1001, '8:devices:/user.slice/user-1001.slice-evil/session-3.scope\n', properties, safe, devices)
for rule in ('a *:* rwm', 'c 195:* rw', 'c *:255 r', 'c 507:0 w', 'malformed', ''):
    assert not guard.isolated_session(1001, cg, properties, rule, devices), rule
assert not guard.isolated_session(1001, cg, properties, safe, set())
print('PASS: slice policy, devices hierarchy, allow rules and boundary checks')
