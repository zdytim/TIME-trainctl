import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cron_guard', root / 'deploy/cron-account-guard.py')
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)


class CronGuardTest(unittest.TestCase):
    def policy(self, uid=1002, shell='/bin/bash', groups=(), missing_group=False, pam_type='account'):
        user = SimpleNamespace(pw_uid=uid, pw_gid=uid, pw_shell=shell)
        def group(_name):
            if missing_group: raise KeyError('group')
            return SimpleNamespace(gr_gid=996)
        with patch.dict(os.environ, {'PAM_USER':'test','PAM_TYPE':pam_type}), \
             patch.object(guard.pwd, 'getpwnam', return_value=user), \
             patch.object(guard.os, 'getgrouplist', return_value=list(groups)), \
             patch.object(guard.grp, 'getgrnam', side_effect=group):
            return guard.main()

    def test_non_admin_humans_denied_even_if_not_enrolled(self):
        self.assertEqual(self.policy(), 1)
        self.assertEqual(self.policy(groups=(998,)), 1)
        self.assertEqual(self.policy(shell=''), 1)

    def test_admin_root_and_nologin_service_allowed(self):
        self.assertEqual(self.policy(groups=(996,)), 0)
        self.assertEqual(self.policy(uid=0, missing_group=True), 0)
        self.assertEqual(self.policy(uid=64030, shell='/usr/sbin/nologin', missing_group=True), 0)

    def test_unknown_identity_group_or_pam_type_denied(self):
        self.assertEqual(self.policy(missing_group=True), 1)
        self.assertEqual(self.policy(pam_type='open_session'), 1)
        with patch.dict(os.environ, {'PAM_USER':'missing','PAM_TYPE':'account'}), \
             patch.object(guard.pwd, 'getpwnam', side_effect=KeyError('missing')):
            self.assertEqual(guard.main(), 1)

    def test_logging_failure_never_allows_denied_user(self):
        with patch.object(guard.syslog, 'openlog', side_effect=OSError('logging unavailable')):
            self.assertEqual(self.policy(), 1)


if __name__ == '__main__': unittest.main()
