#!/usr/bin/env python3
"""Container-only controller test: synthetic approval and private ledger markers."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys

assert os.geteuid() == 0 and os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
assert Path('/.dockerenv').is_file()
state = Path('/tmp/time-isolated/state')
anchor = Path('/var/lib/time-quota-anchor')
wal, checkpoint, witness = state/'monthly-quota.wal', state/'monthly-quota.checkpoint', anchor/'witness.log'
grant_id = '0123456789abcdef0123456789abcdef'
tools = Path('/validation/recovery')
spec = importlib.util.spec_from_file_location('candidate', tools/'time-grant-repair-candidate.py')
candidate = importlib.util.module_from_spec(spec); spec.loader.exec_module(candidate)


def exchange(text):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(4); conn.connect(str(state/'time-quota.sock'))
        conn.sendall(text.encode('ascii')+b'\n')
        return conn.recv(2048).decode('ascii').strip()


def save(path, data):
    with path.open('r+b') as f:
        f.seek(0); f.write(data); f.truncate(); f.flush(); os.fsync(f.fileno())


def rows():
    fields = wal.read_bytes().splitlines()[-1].split(b'|')
    assert fields[2] == b'GRANT' and fields[7].decode() == grant_id
    uid, month, seconds, actor = int(fields[3]), int(fields[4]), int(fields[5]), fields[8].decode()
    assert uid == 1001 and seconds == 3600 and actor == 'uid-1000'
    outbox = {'application_id':grant_id, 'target_uid':uid, 'month':month,
              'gpu_seconds':seconds, 'actor_uid':1000, 'state':'PENDING'}
    app = {'id':grant_id, 'owner_uid':uid, 'kind':'monthly_gpu_hours',
           'status':'GRANT_PENDING', 'approved_json':json.dumps({'month':month,'gpu_hours':1})}
    return outbox, app


action = sys.argv[1]
if action == 'grant':
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    month = now.year*100+now.month
    assert exchange('GRANT 1001 %d 3600 %s uid-1000' % (month, grant_id)) == 'OK'
    print('PASS: real controller accepted fixture grant')
elif action == 'damage':
    assert not (state/'time-quota.sock').exists()
    data = wal.read_bytes(); lines = data.splitlines(keepends=True)
    assert len(lines) == 2
    records = witness.read_bytes().splitlines(keepends=True)
    assert len(records) == 2
    save(witness, records[0])
    if os.environ['TIME_GRANT_MARKER_CASE'] == 'wal_ahead':
        import hashlib
        digest = hashlib.sha256(lines[0]).hexdigest().encode()
        save(checkpoint, b'U20MQ1|CHECKPOINT|0|'+digest+b'\n')
    else:
        assert os.environ['TIME_GRANT_MARKER_CASE'] == 'witness_behind'
    print('PASS: created simulated marker lag in container-private files')
elif action == 'unavailable':
    assert exchange('SNAP 1001') == 'ERROR unavailable_or_invalid'
    print('PASS: mismatched markers keep quota unavailable')
elif action == 'repair':
    assert not (state/'time-quota.sock').exists()
    outbox, app = rows()
    result = candidate.candidate(wal.read_bytes(),checkpoint.read_bytes(),witness.read_bytes(),outbox,app)
    assert result['repair_authorized'] is False
    # Deliberately limited to this disposable container fixture.
    save(checkpoint, result['checkpoint']); save(witness, result['witness'])
    print('PASS: applied candidate bytes to container-private marker files')
elif action == 'verify':
    response = exchange('SNAP 1001').split()
    assert len(response) == 18 and response[0] == 'OK' and int(response[5]) == 3600, response
    print('PASS: restarted real controller replayed 3600 GPU seconds')
else:
    raise SystemExit('unknown fixture action')
