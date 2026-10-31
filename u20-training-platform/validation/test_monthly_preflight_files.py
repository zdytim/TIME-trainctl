import importlib.util
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('monthly_preflight', root / 'deploy/preflight-u20-monthly-cutover.py')
preflight = importlib.util.module_from_spec(spec); spec.loader.exec_module(preflight)


class FileChecks(unittest.TestCase):
    def check(self, mode, allow_group):
        path = Path('/safe/state.sqlite3')
        def info(p):
            if p == path:
                return SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=0, st_gid=998, st_nlink=1)
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0, st_nlink=1)
        with patch.object(Path, 'lstat', info), patch.object(Path, 'read_bytes', return_value=b'data'):
            return preflight.checked_file(path, require_root_group=not allow_group, private=True)

    def test_private_root_owned_database_with_service_group(self):
        self.assertEqual(self.check(0o600, True), b'data')

    def test_reject_group_readable_database(self):
        with self.assertRaises(ValueError): self.check(0o640, True)

    def test_reject_service_group_for_root_group_files(self):
        with self.assertRaises(ValueError): self.check(0o600, False)


if __name__ == '__main__': unittest.main()
