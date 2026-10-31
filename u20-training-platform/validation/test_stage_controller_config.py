import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stage_controller_config', root / 'deploy/stage-u20-controller-config.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class StageTests(unittest.TestCase):
    def scenario(self, existing=False, drift=False):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); source = directory / 'inputs'; source.mkdir()
            baseline = directory / 'slurm.conf'; baseline.write_bytes(b'baseline\n')
            cfg, launcher = directory / 'candidate.conf', directory / 'launcher'
            gen = b'def candidate_config(data):\n    return data + b"candidate\\n"\n'
            code = b'#!/bin/sh\nexit 0\n'
            (source / 'config.py').write_bytes(gen); (source / 'launcher.py').write_bytes(code)
            hashes = {'config.py': hashlib.sha256(gen).hexdigest(), 'launcher.py': hashlib.sha256(code).hexdigest()}
            if existing: launcher.write_text('preserve')
            expected = hashlib.sha256(b'wrong' if drift else baseline.read_bytes()).hexdigest()
            with patch.multiple(module, BASE=baseline, CONFIG=cfg, LAUNCHER=launcher, INPUTS=hashes, BASE_SHA=expected), \
                 patch.object(module, 'trusted', return_value=None), \
                 patch.object(module, 'verify_release', return_value=None), \
                 patch.object(module.os, 'geteuid', return_value=0), \
                 patch.object(module.os, 'uname', return_value=SimpleNamespace(nodename='u20-Z790-S-DDR4')):
                if existing or drift:
                    with self.assertRaises((ValueError, FileExistsError)): module.main(source)
                    self.assertFalse(cfg.exists())
                    if existing: self.assertEqual(launcher.read_text(), 'preserve')
                else:
                    module.main(source)
                    self.assertEqual(cfg.read_bytes(), b'baseline\ncandidate\n')
                    self.assertEqual(launcher.read_bytes(), code)
                    self.assertEqual(os.stat(cfg).st_mode & 0o777, 0o644)
                    self.assertEqual(os.stat(launcher).st_mode & 0o777, 0o755)

    def test_stage_new_files(self): self.scenario()
    def test_refuse_existing_destination(self): self.scenario(existing=True)
    def test_refuse_baseline_drift(self): self.scenario(drift=True)


if __name__ == '__main__': unittest.main()
