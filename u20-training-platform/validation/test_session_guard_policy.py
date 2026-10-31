import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('session_guard_policy', root / 'deploy/session-guard.py')
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)


class SessionPolicy(unittest.TestCase):
    def evaluate(self, uid=1002, shell='/bin/bash', groups=(), missing_group=False):
        user = SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_shell=shell)
        def group(name):
            if missing_group: raise KeyError(name)
            return SimpleNamespace(gr_gid={'pro6000d-training':998, 'pro6000d-admin':996}[name])
        with patch.dict(os.environ, {'PAM_USER':'sample','PAM_TYPE':'open_session'}), \
             patch.object(guard.pwd, 'getpwnam', return_value=user), \
             patch.object(guard.os, 'getgrouplist', return_value=list(groups)), \
             patch.object(guard.grp, 'getgrnam', side_effect=group):
            return guard.main()

    def test_unknown_human_is_denied(self):
        self.assertEqual(self.evaluate(), 1)
        self.assertEqual(self.evaluate(shell=''), 1)

    def test_admin_root_and_system_service_are_not_blocked(self):
        self.assertEqual(self.evaluate(groups=(996,)), 0)
        self.assertEqual(self.evaluate(uid=0, missing_group=True), 0)
        self.assertEqual(self.evaluate(uid=64030, shell='/usr/sbin/nologin', missing_group=True), 0)

    def test_unknown_groups_fail_closed(self):
        self.assertEqual(self.evaluate(missing_group=True), 1)

    def test_unknown_user_is_denied(self):
        with patch.dict(os.environ, {'PAM_USER':'missing','PAM_TYPE':'open_session'}), \
             patch.object(guard.pwd, 'getpwnam', side_effect=KeyError('missing')):
            self.assertEqual(guard.main(), 1)


if __name__ == '__main__': unittest.main()
