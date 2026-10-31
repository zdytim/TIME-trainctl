import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance_rehearsal', root / 'deploy/rehearse-u20-maintenance-fence.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'marker'
        self.parent = os.open(self.temp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.patcher = patch.object(module, 'MARKER', self.path); self.patcher.start()

    def tearDown(self):
        self.patcher.stop(); os.close(self.parent); self.temp.cleanup()

    def test_create_and_remove_exact_marker(self):
        identity = module.create_marker(self.parent, b'rehearsal\n')
        self.assertEqual(self.path.read_bytes(), b'rehearsal\n')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        module.remove_marker(self.parent, identity, b'rehearsal\n')
        self.assertFalse(self.path.exists())

    def test_refuses_changed_marker(self):
        identity = module.create_marker(self.parent, b'original')
        self.path.write_bytes(b'changed')
        with self.assertRaises(ValueError): module.remove_marker(self.parent, identity, b'original')
        self.assertEqual(self.path.read_bytes(), b'changed')

    def test_never_overwrites_existing_marker(self):
        self.path.write_bytes(b'existing')
        with self.assertRaises(FileExistsError): module.create_marker(self.parent, b'new')
        self.assertEqual(self.path.read_bytes(), b'existing')

    def test_partial_marker_is_removed_after_write_failure(self):
        actual = os.fsync; attempts = [0]
        def fsync(fd):
            attempts[0] += 1
            if attempts[0] == 1: raise OSError('injected fsync failure')
            return actual(fd)
        with patch.object(module.os, 'fsync', side_effect=fsync):
            with self.assertRaises(OSError): module.create_marker(self.parent, b'partial')
        self.assertFalse(self.path.exists())


if __name__ == '__main__': unittest.main()
