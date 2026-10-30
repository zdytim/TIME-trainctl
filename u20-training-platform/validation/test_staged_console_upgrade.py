#!/usr/bin/env python3
"""Exercise staged upgrade and rollback using disposable container-local files."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

assert os.geteuid() == 0 and os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
assert Path('/.dockerenv').is_file()
spec = importlib.util.spec_from_file_location('staged_upgrade', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def sha(data): return hashlib.sha256(data).hexdigest()


def scenario(base, fail_health=False, race=False):
    lib, ui, config_dir, db_dir, backups, code = [base / x for x in ('lib', 'ui', 'config', 'db', 'backups', 'code')]
    for directory in (lib, ui, config_dir, db_dir, backups, code): directory.mkdir(parents=True)
    module.LIB, module.UI, module.BACKUPS = lib, ui, backups
    module.CONFIG, module.DATABASE, module.CLIENT = config_dir / 'config.json', db_dir / 'db.sqlite3', base / 'client'
    module.CONFIG.write_text(json.dumps({'state_db':str(module.DATABASE)}))
    module.CLIENT.write_text('fixture')
    with sqlite3.connect(str(module.DATABASE)) as db:
        db.execute('CREATE TABLE jobs (id TEXT, state TEXT)')
        db.execute('INSERT INTO jobs VALUES (?,?)', ('old-job', 'COMPLETED'))
    old = {'broker.py': (lib / 'api.py', b'old broker'), 'app.js': (ui / 'app.js', b'old js'),
           'index.html': (ui / 'index.html', b'old html'), 'styles.css': (ui / 'styles.css', b'old css')}
    new = {'broker.py': (lib / 'api.py', b'new broker'), 'controller_bridge.py': (lib / 'controller_bridge.py', b'bridge'),
           'controller_view.py': (lib / 'controller_view.py', b'view'), 'app.js': (ui / 'app.js', b'new js'),
           'index.html': (ui / 'index.html', b'new html'), 'styles.css': (ui / 'styles.css', b'new css')}
    for _name, (path, data) in old.items(): path.write_bytes(data)
    for name, (_path, data) in new.items(): (code / name).write_bytes(data)
    module.OLD = {name:(path,sha(data)) for name,(path,data) in old.items()}
    module.NEW = {name:(path,sha(data)) for name,(path,data) in new.items()}
    checks = {'n':0, 'running':True}
    def no_jobs():
        checks['n'] += 1
        return not (race and checks['n'] == 2)
    def run(args, **_kw):
        if args[:2] == ['/usr/bin/systemctl','stop']: checks['running'] = False
        if args[:2] == ['/usr/bin/systemctl','start']: checks['running'] = True
        return SimpleNamespace(stdout='', stderr='')
    def health(staged):
        assert checks['running']
        if staged and fail_health: raise RuntimeError('injected health failure')
        if staged: assert (lib/'api.py').read_bytes() == b'new broker'
        else: assert (lib/'api.py').read_bytes() == b'old broker'
    module.no_scheduler_jobs = no_jobs
    module.run, module.health = run, health
    try:
        module.main(code)
        assert not (fail_health or race)
        assert all(path.read_bytes() == data for path, data in new.values())
        record = next(backups.iterdir())
        assert json.loads((record/'result.json').read_text())['status'] == 'staged-upgrade-healthy'
    except (RuntimeError, ValueError):
        assert fail_health or race
        assert checks['running'] and all(path.read_bytes() == data for path, data in old.values())
        assert not (lib/'controller_bridge.py').exists() and not (lib/'controller_view.py').exists()
        record = next(backups.iterdir())
        assert 'old service healthy' in json.loads((record/'result.json').read_text())['rollback']
    with sqlite3.connect(str(module.DATABASE)) as db:
        assert db.execute('SELECT * FROM jobs').fetchall() == [('old-job','COMPLETED')]


with tempfile.TemporaryDirectory(prefix='time-upgrade-test-', dir='/run') as temp:
    root = Path(temp)
    scenario(root/'success')
    scenario(root/'rollback', fail_health=True)
    scenario(root/'race', race=True)
print('PASS: staged upgrade, failure rollback and queue race; live data untouched')
