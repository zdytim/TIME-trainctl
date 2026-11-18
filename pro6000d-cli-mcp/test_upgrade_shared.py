import importlib.util
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('upgrader', Path(__file__).with_name('upgrade-shared.py'))
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)

def local_check(path, file=False):
    path = Path(path)
    if path.is_symlink() or (file and not path.is_file()) or (not file and not path.is_dir()): raise ValueError('unsafe test path')

class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.lib = self.base/'lib'; self.lib.mkdir()
        self.bin = self.base/'bin'; self.bin.mkdir()
        self.old = self.base/'old'; self.old.mkdir()
        self.new = self.base/'new'; self.new.mkdir()
        self.installed = self.lib/u.OLD_NAME; self.installed.mkdir()
        for folder, content in ((self.old, b'old'), (self.new, b'new')):
            for name in u.FILES:
                (folder/name).write_bytes(content + name.encode())
            (folder/'manifest.json').write_text(json.dumps({name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in u.FILES}))
        for name in u.FILES:
            (self.installed/name).write_bytes((self.old/name).read_bytes())
            (self.installed/name).chmod(0o644)
        for name, data in u.wrappers(self.lib, u.OLD_NAME).items():
            (self.bin/name).write_bytes(data); (self.bin/name).chmod(0o755)
    def tearDown(self): self.temp.cleanup()
    def upgrade(self, **kw): u.upgrade(self.new, self.old, self.lib, self.bin, check=local_check, **kw)
    def test_dry_run(self):
        self.upgrade()
        self.assertFalse((self.lib/u.NEW_NAME).exists())
    def test_upgrade_and_idempotent(self):
        self.upgrade(execute=True); self.upgrade(execute=True)
        for name, data in u.wrappers(self.lib, u.NEW_NAME).items(): self.assertEqual((self.bin/name).read_bytes(), data)
        self.assertEqual(set(os.listdir(self.lib/(u.NEW_NAME+'-backups'))), set(u.wrappers(self.lib, u.OLD_NAME)))
    def test_rollback_after_replace(self):
        def hook(step, name):
            if step == 'after': raise RuntimeError('failure')
        with self.assertRaises(RuntimeError): self.upgrade(execute=True, hook=hook)
        for name, data in u.wrappers(self.lib, u.OLD_NAME).items(): self.assertEqual((self.bin/name).read_bytes(), data)
    def test_reject_mixed(self):
        (self.bin/'timectl').write_bytes(u.wrappers(self.lib, u.NEW_NAME)['timectl'])
        with self.assertRaisesRegex(ValueError, 'mixed'): self.upgrade(execute=True)
        self.assertFalse((self.lib/u.NEW_NAME).exists())
    def test_reject_changed_old_and_mode(self):
        (self.installed/'timectl.py').write_text('changed')
        with self.assertRaises(ValueError): self.upgrade(execute=True)
        (self.installed/'timectl.py').write_bytes((self.old/'timectl.py').read_bytes())
        (self.bin/'timectl').chmod(0o644)
        with self.assertRaises(ValueError): self.upgrade(execute=True)
    def test_reject_incomplete_backup(self):
        b = self.lib/(u.NEW_NAME+'-backups'); b.mkdir(mode=0o700)
        with self.assertRaises(ValueError): self.upgrade(execute=True)

    def test_recheck_does_not_overwrite_changed_wrapper(self):
        def hook(step, name):
            if step == 'before' and name == 'timectl':
                (self.bin/name).write_bytes(b'external change')
        with self.assertRaisesRegex(RuntimeError, 'rollback incomplete'):
            self.upgrade(execute=True, hook=hook)
        self.assertEqual((self.bin/'timectl').read_bytes(), b'external change')

    def test_restrictive_umask(self):
        previous = os.umask(0o077)
        try:
            self.upgrade(execute=True)
            self.upgrade(execute=True)
        finally:
            os.umask(previous)
        self.assertEqual((self.lib/u.NEW_NAME).stat().st_mode & 0o777, 0o755)

if __name__ == '__main__': unittest.main()
