#!/usr/bin/env python3
"""Check broker upgrade configuration and rollback with private fixture files."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'deploy/upgrade-u20-gpu-probe.py'
spec = importlib.util.spec_from_file_location('probe_upgrade', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Upgrade(unittest.TestCase):
    def test_configuration_preserves_other_fields_and_rejects_root(self):
        original = b'{"state_db":"/x", "monthly_controller":{"read_enabled":false}}\n'
        updated = json.loads(module.updated_config(original, 'u20', 1000))
        self.assertEqual(updated['gpu_probe_user'], 'u20')
        self.assertEqual(updated['state_db'], '/x')
        self.assertEqual(updated['monthly_controller'], {'read_enabled': False})
        with self.assertRaises(ValueError):
            module.updated_config(original, 'root', 0)
        with self.assertRaises(ValueError):
            module.updated_config(b'{"gpu_probe_user":"other"}', 'u20', 1000)

    def test_rollback_refuses_unexpected_content(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'file'
            p.write_bytes(b'unexpected concurrent update')
            with self.assertRaises(ValueError):
                module.restore_known(p, b'old', b'new', 0o600)
            self.assertEqual(p.read_bytes(), b'unexpected concurrent update')
            p.write_bytes(b'new')
            module.restore_known(p, b'old', b'new', 0o600)
            self.assertEqual(p.read_bytes(), b'old')

    def scenario(self, fail_health=False, race=False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            broker, config, helper = root/'broker', root/'config', root/'helper'
            candidate, backups = root/'candidate', root/'backups'
            backups.mkdir()
            broker.write_bytes(b'old code'); candidate.write_bytes(b'new code')
            config.write_bytes(b'{"state_db":"/fixture"}\n'); helper.write_bytes(b'helper')
            calls = {'checks': 0, 'running': True}
            def no_jobs():
                calls['checks'] += 1
                return not (race and calls['checks'] == 2)
            def run(argv):
                if argv[1:2] == ['stop']: calls['running'] = False
                if argv[1:2] == ['start']: calls['running'] = True
                return SimpleNamespace(stdout='other::---\n')
            def health(staged):
                self.assertTrue(calls['running'])
                if staged and fail_health: raise RuntimeError('injected health failure')
            base = SimpleNamespace(BACKUPS=backups, DATABASE=Path('/fixture'), SERVICE='test',
                trusted_directory=lambda _p: None, trusted_file=lambda p, *args: p.read_bytes(),
                no_scheduler_jobs=no_jobs, no_active_platform_jobs=lambda: True,
                backup_database=lambda p: p.write_bytes(b'database backup'), run=run, health=health,
                api=lambda _uid,_path: {'gpus': {'available':True, 'devices':[{}]}})
            users = lambda name: SimpleNamespace(pw_name=name, pw_uid=1000 if name == 'u20' else 1001)
            with patch.multiple(module, BROKER=broker, CONFIG=config, HELPER=helper), \
                 patch.object(module.os, 'geteuid', return_value=0), \
                 patch.object(module.os, 'uname', return_value=SimpleNamespace(nodename='u20-Z790-S-DDR4')), \
                 patch.object(module.pwd, 'getpwnam', side_effect=users):
                if fail_health or race:
                    with self.assertRaises((RuntimeError, ValueError)): module.main(base, candidate)
                    self.assertEqual(broker.read_bytes(), b'old code')
                    self.assertEqual(config.read_bytes(), b'{"state_db":"/fixture"}\n')
                else:
                    module.main(base, candidate)
                    self.assertEqual(broker.read_bytes(), b'new code')
                    self.assertEqual(json.loads(config.read_bytes())['gpu_probe_user'], 'u20')
                self.assertTrue(calls['running'])

    def test_switch(self): self.scenario()
    def test_failed_health_rolls_back(self): self.scenario(fail_health=True)
    def test_queue_race_does_not_change_code(self): self.scenario(race=True)


if __name__ == '__main__':
    unittest.main()
