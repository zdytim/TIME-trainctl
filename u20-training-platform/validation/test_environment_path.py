#!/usr/bin/env python3
"""The broker must accept absolute environment paths without shell metacharacters."""
import importlib.util
from pathlib import Path
import sys
import unittest

backend = Path(__file__).resolve().parents[1] / 'backend'
sys.path.insert(0, str(backend))
spec = importlib.util.spec_from_file_location('training_backend', backend/'pro6000d-training-api.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class EnvironmentPath(unittest.TestCase):
    def test_names_and_absolute_paths(self):
        for value in ('system', 'pytorch_2.4', '/home/u20/miniconda3/envs/pytorch_2.4'):
            with self.subTest(value=value): self.assertIsNotNone(module.SAFE_ENV.fullmatch(value))

    def test_shell_metacharacters_are_rejected(self):
        for value in ('/tmp/with space', '/tmp/x;id', '$(id)', ''):
            with self.subTest(value=value): self.assertIsNone(module.SAFE_ENV.fullmatch(value))


if __name__ == '__main__': unittest.main()
