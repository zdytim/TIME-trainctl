#!/usr/bin/env python3
"""Prove matching marker hashes cannot hide invalid WAL event semantics."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3

assert os.geteuid()==0 and os.environ.get('TIME_ISOLATED_VALIDATION')=='1' and Path('/.dockerenv').is_file()
root=Path('/tmp/time-semantic-rejection'); root.mkdir(mode=0o700)
state,anchor,dbdir,backups=[root/name for name in ('state','anchor','db','backups')]
for path in (state,anchor,dbdir,backups): path.mkdir(mode=0o700)
os.chown(state,64030,64030)
grant='0123456789abcdef0123456789abcdef'
header=b'U20MQ1|HEADER|1\n'; invalid=b'U20MQ1|1|ABORT|999|1\n'
event=('U20MQ1|2|GRANT|1001|202610|3600|0|'+grant+'|uid-1000\n').encode()
prior=header+invalid; wal=prior+event
def sha(data): return hashlib.sha256(data).hexdigest().encode()
checkpoint=b'U20MQ1|CHECKPOINT|1|'+sha(prior)+b'\n'
witness=b'U20MQA1|0|'+sha(header)+b'\nU20MQA1|1|'+sha(prior)+b'\n'
for p,data in ((state/'monthly-quota.wal',wal),(state/'monthly-quota.checkpoint',checkpoint),(anchor/'witness.log',witness)):
    p.write_bytes(data); p.chmod(0o600)
    if p.parent==state: os.chown(p,64030,64030)
db=dbdir/'state.sqlite3'
with sqlite3.connect(str(db)) as c:
    c.execute('CREATE TABLE monthly_grant_outbox (application_id TEXT, target_uid INTEGER, month INTEGER, gpu_seconds INTEGER, actor_uid INTEGER, state TEXT)')
    c.execute('CREATE TABLE applications (id TEXT, owner_uid INTEGER, kind TEXT, status TEXT, approved_json TEXT)')
    c.execute('INSERT INTO monthly_grant_outbox VALUES (?,?,?,?,?,?)',(grant,1001,202610,3600,1000,'PENDING'))
    c.execute('INSERT INTO applications VALUES (?,?,?,?,?)',(grant,1001,'monthly_gpu_hours','GRANT_PENDING',json.dumps({'month':202610,'gpu_hours':1})))
db.chmod(0o600)
path=Path('/validation/recovery/time-grant-recovery-snapshot.py')
spec=importlib.util.spec_from_file_location('snapshot',path); snapshot=importlib.util.module_from_spec(spec); spec.loader.exec_module(snapshot)
assert snapshot.inspector.inspect(wal,checkpoint,witness)['observation']=='wal_one_event_ahead'
try:
    snapshot.collect(state,anchor,db,backups,64030,0,lambda:None,
                     lambda p:snapshot.semantic_replay('/validation/recovery/quota-replay',p))
    raise AssertionError('invalid prior ABORT must fail controller-source replay')
except ValueError as exc:
    assert 'replay failed' in str(exc), exc
assert (state/'monthly-quota.wal').read_bytes()==wal
assert (state/'monthly-quota.checkpoint').read_bytes()==checkpoint
assert (anchor/'witness.log').read_bytes()==witness
assert all(not (p/'preview.json').exists() for p in backups.iterdir())
print('PASS: matching markers and approval cannot bypass invalid WAL semantics')
