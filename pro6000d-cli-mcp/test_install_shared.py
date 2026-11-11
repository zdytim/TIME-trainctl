"""Exercise installation and rollback in a disposable directory."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

path=Path(__file__).with_name('install-shared.py')
spec=importlib.util.spec_from_file_location('install_shared',path)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class InstallerTests(unittest.TestCase):
    def setup_paths(self, tmp):
        root=Path(tmp); source=root/'source'; source.mkdir(); bindir=root/'bin'; bindir.mkdir()
        for name in module.FILES: (source/name).write_bytes(('example '+name).encode())
        (source/'manifest.json').write_text(json.dumps({n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in module.FILES}))
        lib=root/'lib'/'pro6000d-client'; lib.parent.mkdir()
        version=lib/'version'; wrappers={name:('#!/bin/sh\n'+name+'\n').encode() for name in ('timectl','time-mcp','time-mcp-confirm')}
        return source,bindir,lib,version,wrappers

    def test_success_installs_only_expected_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,bindir,lib,version,wrappers=self.setup_paths(tmp)
            with patch.object(module,'trusted'), patch.object(module.os,'geteuid',return_value=0), \
                 patch.object(module.os,'uname',return_value=types.SimpleNamespace(nodename='ubuntu')), \
                 patch.object(module,'BIN',bindir),patch.object(module,'LIB',lib),patch.object(module,'VERSION',version),patch.object(module,'WRAPPERS',wrappers):
                module.main(source)
            self.assertEqual(set(p.name for p in bindir.iterdir()),set(wrappers))
            self.assertEqual((version/'time_core.py').read_bytes(),(source/'time_core.py').read_bytes())

    def test_failed_publication_removes_only_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,bindir,lib,version,wrappers=self.setup_paths(tmp)
            sentinel=bindir/'unrelated'; sentinel.write_text('preserve')
            real_link=module.os.link; calls=[]
            def fail_second(*args,**kwargs):
                calls.append(1)
                if len(calls)==2: raise OSError('injected failure')
                return real_link(*args,**kwargs)
            with patch.object(module,'trusted'), patch.object(module.os,'geteuid',return_value=0), \
                 patch.object(module.os,'uname',return_value=types.SimpleNamespace(nodename='ubuntu')), \
                 patch.object(module,'BIN',bindir),patch.object(module,'LIB',lib),patch.object(module,'VERSION',version), \
                 patch.object(module,'WRAPPERS',wrappers),patch.object(module.os,'link',side_effect=fail_second):
                with self.assertRaises(OSError): module.main(source)
            self.assertEqual([p.name for p in bindir.iterdir()],['unrelated'])
            self.assertEqual(sentinel.read_text(),'preserve')
            self.assertFalse(lib.exists())

if __name__=='__main__': unittest.main()
