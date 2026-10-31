import hashlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance_install', root / 'deploy/install-u20-maintenance-condition.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class InstallTest(unittest.TestCase):
    def scenario(self, existing=False, marker=False, changed_pid=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); base = path/'service'; old = path/'old.conf'
            dest = path/'new.conf'; mark = path/'marker'; source = path/'source'
            base.write_bytes(b'base'); old.write_bytes(b'old'); source.write_bytes(b'condition')
            if existing: dest.write_bytes(b'preserve')
            if marker: mark.touch()
            sha = lambda data: hashlib.sha256(data).hexdigest()
            expected = {base: sha(b'base'), old: sha(b'old')}
            calls = []
            before = {'ActiveState':'active','MainPID':'123','FragmentPath':str(base),'DropInPaths':str(old)}
            after = {**before, 'MainPID':'456' if changed_pid else '123', 'DropInPaths':str(old)+' '+str(dest)}
            states = iter((before, after))
            def run(args): calls.append(args); return ''
            with patch.multiple(module, BASE=base, OLD_DROPIN=old, DEST=dest, MARKER=mark,
                                EXPECTED=expected, SOURCE_SHA=sha(b'condition')), \
                 patch.object(module, 'trusted', return_value=None), \
                 patch.object(module, 'properties', side_effect=lambda: next(states)), \
                 patch.object(module, 'run', side_effect=run), \
                 patch.object(module.os, 'geteuid', return_value=0), \
                 patch.object(module.os, 'uname', return_value=SimpleNamespace(nodename='u20-Z790-S-DDR4')):
                if existing or marker or changed_pid:
                    with self.assertRaises((ValueError, FileExistsError)): module.main(source)
                    if existing: self.assertEqual(dest.read_bytes(), b'preserve')
                    else: self.assertFalse(dest.exists())
                    if changed_pid: self.assertEqual(len(calls), 2)
                else:
                    module.main(source)
                    self.assertEqual(dest.read_bytes(), b'condition')
                    self.assertEqual(len(calls), 1)

    def test_installs_without_restart(self): self.scenario()
    def test_existing_dropin_is_not_overwritten(self): self.scenario(existing=True)
    def test_existing_marker_refused(self): self.scenario(marker=True)
    def test_pid_change_rolls_back_dropin(self): self.scenario(changed_pid=True)


if __name__ == '__main__': unittest.main()
