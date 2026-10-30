#!/usr/bin/env python3
"""GPU monitoring must never invoke NVIDIA tooling as root."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

backend = Path(__file__).resolve().parents[1] / 'backend'
sys.path.insert(0, str(backend))
spec = importlib.util.spec_from_file_location('training_backend', backend / 'pro6000d-training-api.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GpuProbeIdentity(unittest.TestCase):
    def setUp(self):
        self.service = module.Service.__new__(module.Service)
        self.service.config = {}

    @patch.object(module.os, 'geteuid', return_value=0)
    def test_root_requires_explicit_nonroot_probe_user(self, _euid):
        with self.assertRaises(ValueError):
            self.service.gpu_probe_argv('/usr/bin/nvidia-smi')
        self.service.config['gpu_probe_user'] = 'root'
        with self.assertRaises(ValueError):
            self.service.gpu_probe_argv('/usr/bin/nvidia-smi')

    @patch.object(module.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1001, pw_gid=1001))
    @patch.object(module.os, 'geteuid', return_value=0)
    def test_drops_uid_gid_and_supplementary_groups(self, _euid, _pwd):
        self.service.config['gpu_probe_user'] = 'nobody'
        self.assertEqual(self.service.gpu_probe_argv('/usr/bin/nvidia-smi'),
                         ['/usr/bin/setpriv', '--reuid', '1001', '--regid', '1001',
                          '--clear-groups', '/usr/bin/nvidia-smi', '-q', '-x'])

    @patch.object(module.os, 'geteuid', return_value=1001)
    def test_nonroot_service_does_not_escalate(self, _euid):
        self.assertEqual(self.service.gpu_probe_argv('/usr/bin/nvidia-smi'),
                         ['/usr/bin/nvidia-smi', '-q', '-x'])


if __name__ == '__main__':
    unittest.main()
