import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'tools/u20-controller-config.py'
spec = importlib.util.spec_from_file_location('u20_controller_config', path)
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ControllerConfigTests(unittest.TestCase):
    def setUp(self):
        self.base = b''.join((key + '=' + value + '\n').encode()
                             for key, value in config.EXPECTED.items())

    def test_preserves_baseline_and_adds_only_controller_options(self):
        baseline = self.base + b'NodeName=u20 Gres=gpu:1\n'
        result = config.candidate_config(baseline)
        self.assertEqual(result, baseline +
                         ('PluginDir=' + config.PLUGIN_DIR + '\n').encode() +
                         b'SlurmctldParameters=MonthlyGpuQuota\n')

    def test_rejects_existing_options_and_includes(self):
        for extra in (b'PluginDir=/tmp/unsafe\n', b'slurmctldparameters=x\n',
                      b'Include /tmp/other.conf\n', b'Include=/tmp/other.conf\n'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                config.candidate_config(self.base + extra)

    def test_rejects_unexpected_duplicate_or_missing_baseline(self):
        for raw in (self.base + b'SlurmUser=root\n',
                    self.base.replace(b'slurmuser=slurm\n', b'slurmuser=root\n'),
                    self.base.replace(b'slurmuser=slurm\n', b'')):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.candidate_config(raw)

    def test_rejects_multiline_and_invalid_input(self):
        for raw in (b'', self.base + b'Other=foo\\\nbar\n', self.base + b'\x00', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                config.candidate_config(raw)


if __name__ == '__main__':
    unittest.main()
