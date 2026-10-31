#!/usr/bin/env python3
"""Exercise private SQLite approval, real controller and candidate marker recovery."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time

assert os.geteuid() == 0 and os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
assert Path('/.dockerenv').is_file()
state = Path('/tmp/time-isolated/state'); anchor = Path('/var/lib/time-quota-anchor')
wal, checkpoint, witness = state/'monthly-quota.wal', state/'monthly-quota.checkpoint', anchor/'witness.log'
config, db_path = Path('/tmp/time-isolated/broker-config.json'), Path('/tmp/time-isolated/broker.sqlite3')
tools = Path('/validation/recovery'); sys.path.insert(0, '/broker')
def load(name, path):
    spec=importlib.util.spec_from_file_location(name, path)
    obj=importlib.util.module_from_spec(spec); spec.loader.exec_module(obj); return obj
candidate = load('candidate', tools/'time-grant-repair-candidate.py')
broker = load('broker', '/broker/pro6000d-training-api.py')


def exchange(text):
    deadline=time.monotonic()+3
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(4); conn.connect(str(state/'time-quota.sock'))
                conn.sendall(text.encode('ascii')+b'\n'); return conn.recv(2048).decode('ascii').strip()
        except (ConnectionRefusedError, FileNotFoundError):
            if time.monotonic() >= deadline: raise
            time.sleep(.1)


def save(path, data):
    with path.open('r+b') as f:
        f.seek(0); f.write(data); f.truncate(); f.flush(); os.fsync(f.fileno())


def rows():
    fields = wal.read_bytes().splitlines()[-1].split(b'|')
    assert fields[2] == b'GRANT'
    grant_id=fields[7].decode('ascii')
    with sqlite3.connect('file:'+str(db_path)+'?mode=ro', uri=True) as conn:
        conn.row_factory=sqlite3.Row
        outbox=conn.execute('SELECT * FROM monthly_grant_outbox WHERE application_id=?',(grant_id,)).fetchone()
        app=conn.execute('SELECT * FROM applications WHERE id=?',(grant_id,)).fetchone()
        assert outbox is not None and app is not None
        return dict(outbox),dict(app)


action=sys.argv[1]
if action == 'grant':
    config.write_text(json.dumps({'state_db':str(db_path),'training_group':'timefixture','admin_group':'root',
        'monthly_controller':{'read_enabled':True,'grant_enabled':True,'activation_verified':True,
                              'controller_user':'slurm','socket_path':str(state/'time-quota.sock')}}))
    service=broker.Service(str(config))
    user,admin=service.identity(1001),service.identity(0)
    now=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))); month=now.year*100+now.month
    app=service.create_application(user,{'kind':'monthly_gpu_hours','reason':'fixture recovery scenario',
        'requested':{'gpu_hours':1,'month':month,'client_request_id':'fixture-grant-001'}})
    real=broker.ControllerBridge.grant
    def lost_reply(self,*args):
        real(self,*args); raise broker.ControllerOutcomeUnknown('simulated loss after commit')
    broker.ControllerBridge.grant=lost_reply
    try:
        service.decide_application(admin,app['id'],{'action':'approve','decision_reason':'fixture approval'})
        raise AssertionError('lost reply should keep approval pending')
    except broker.ApiError as exc:
        assert exc.code == 'grant_unconfirmed', exc
    finally:
        broker.ControllerBridge.grant=real; service.store.db.close()
    row,app=rows()
    assert row['state']=='PENDING' and app['status']=='GRANT_PENDING'
    print('PASS: real broker SQLite has durable pending approval after lost response')
elif action == 'damage':
    if os.environ.get('TIME_GRANT_RUNNING_JOB') != '1':
        assert not (state/'time-quota.sock').exists()
    lines=wal.read_bytes().splitlines(keepends=True); records=witness.read_bytes().splitlines(keepends=True)
    assert len(lines)>=2 and len(records)==len(lines) and lines[-1].split(b'|')[2]==b'GRANT'
    save(witness,b''.join(records[:-1]))
    if os.environ['TIME_GRANT_MARKER_CASE']=='wal_ahead':
        import hashlib
        prior=b''.join(lines[:-1]); seq=str(len(lines)-2).encode()
        save(checkpoint,b'U20MQ1|CHECKPOINT|'+seq+b'|'+hashlib.sha256(prior).hexdigest().encode()+b'\n')
    else: assert os.environ['TIME_GRANT_MARKER_CASE']=='witness_behind'
    print('PASS: created simulated marker lag in container-private files')
elif action == 'unavailable':
    try:
        assert exchange('SNAP 1001')=='ERROR unavailable_or_invalid'
    except (ConnectionRefusedError, FileNotFoundError):
        # After SIGKILL the controller intentionally preserves a stale socket
        # when the ledger is unhealthy. Both outcomes are fail-closed.
        assert os.environ.get('TIME_GRANT_RUNNING_JOB') == '1'
        print('OBSERVED: unhealthy restart preserved an unavailable stale socket')
    print('PASS: inconsistent markers keep quota unavailable')
elif action == 'repair':
    if os.environ.get('TIME_GRANT_RUNNING_JOB') != '1':
        assert not (state/'time-quota.sock').exists()
    outbox,app=rows()
    result=candidate.candidate(wal.read_bytes(),checkpoint.read_bytes(),witness.read_bytes(),outbox,app)
    assert not result['repair_authorized']
    save(checkpoint,result['checkpoint']); save(witness,result['witness'])
    print('PASS: candidate uses real private SQLite rows; wrote container-only files')
elif action == 'verify':
    outbox,app=rows(); assert outbox['state']=='PENDING' and app['status']=='GRANT_PENDING'
    service=broker.Service(str(config)); admin=service.identity(0)
    assert service.finish_monthly_grant(admin,app['id'])['status']=='APPROVED'
    service.store.db.close(); outbox,app=rows()
    assert outbox['state']=='APPLIED' and app['status']=='APPROVED'
    response=exchange('SNAP 1001').split()
    assert len(response)==18 and response[0]=='OK' and int(response[5])==3600,response
    if os.environ.get('TIME_GRANT_RUNNING_JOB') == '1':
        assert int(response[13])==1 and int(response[14])==1, response
        assert int(response[3]) > 0, response
        print('PASS: recovered running allocation continues to accrue GPU usage')
    print('PASS: broker reconciled pending approval without double grant')
else: raise SystemExit('unknown fixture action')
