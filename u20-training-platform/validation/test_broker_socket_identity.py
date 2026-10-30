#!/usr/bin/env python3
"""Real broker socket identity and approval flow in an isolated container."""
import importlib.util
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import time

assert os.environ.get('TIME_ISOLATED_VALIDATION') == '1'
assert pathlib.Path('/.dockerenv').is_file()
root = pathlib.Path(os.environ['TIME_VALIDATION_BROKER_DIR']).resolve(strict=True)
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('time_validation_broker', root / 'pro6000d-training-api.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

if len(sys.argv) > 1 and sys.argv[1] == 'server':
    service = module.Service(sys.argv[2])
    # Limit the fixture to API/identity logic, not live scheduler polling.
    service.tick = lambda: None
    original_run = module.run
    def fixture_run(argv, *args, **kwargs):
        if argv[0] == '/usr/bin/setfacl':
            return 0, '', ''  # Container lacks setfacl; group socket mode is used.
        return original_run(argv, *args, **kwargs)
    module.run = fixture_run
    module.serve(service)
    raise SystemExit('server unexpectedly returned')

if len(sys.argv) > 1 and sys.argv[1] == 'client':
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5)
        conn.connect(sys.argv[2])
        conn.sendall(sys.argv[3].encode() + b'\n')
        data = bytearray()
        while b'\n' not in data:
            part = conn.recv(65536)
            if not part:
                raise RuntimeError('unexpected EOF')
            data.extend(part)
        print(data.decode().strip())
    sys.exit(0)

assert os.geteuid() == 0
def become_user():
    os.setgroups([])
    os.setgid(1001)
    os.setuid(1001)

with tempfile.TemporaryDirectory(prefix='time-socket-', dir='/tmp') as tmp:
    # The CLI durably syncs the request ledger's parent directory; allow the
    # fixture user to open this parent while keeping the database private.
    os.chmod(tmp, 0o755)
    private = pathlib.Path(tmp) / 'private'
    private.mkdir(mode=0o700)
    # The production client deliberately accepts only this fixed path. /run is
    # container-private tmpfs in the enclosing fixture.
    endpoint = '/run/pro6000d-training/backend.sock'
    config = private / 'config.json'
    config.write_text(json.dumps({
        'state_db': str(private / 'state.sqlite3'), 'socket_path': endpoint,
        'socket_group': 'timefixture', 'training_group': 'timefixture', 'admin_group': 'root',
        'monthly_controller': {'grant_enabled': True, 'read_enabled': True, 'activation_verified': True, 'controller_user': 'slurm',
                               'socket_path': '/tmp/time-isolated/state/time-quota.sock'},
    }))
    server = subprocess.Popen([sys.executable, __file__, 'server', str(config)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            if pathlib.Path(endpoint).is_socket():
                break
            if server.poll() is not None:
                raise RuntimeError('broker exited: ' + server.stderr.read())
            time.sleep(.05)
        else:
            raise RuntimeError('broker socket not ready')

        def request(uid, payload):
            proc = subprocess.run([sys.executable, __file__, 'client', endpoint, json.dumps(payload)],
                                  preexec_fn=become_user if uid == 1001 else None,
                                  capture_output=True, text=True, timeout=10)
            assert proc.returncode == 0, (proc.stdout, proc.stderr)
            return json.loads(proc.stdout)

        me = request(1001, {'method': 'GET', 'path': '/api/v1/me', 'uid': 0})
        assert me['ok'] and me['data']['identity']['uid'] == 1001 and not me['data']['identity']['is_admin'], me
        bridge = module.ControllerBridge('/tmp/time-isolated/state/time-quota.sock', 64030)
        month = bridge.snapshot(1001)['month']
        create = {'method': 'POST', 'path': '/api/v1/applications', 'body': {
            'kind': 'monthly_gpu_hours', 'reason': 'Need more GPU training time',
            'requested': {'gpu_hours': 10, 'month': month, 'client_request_id': 'socket-approval-1'}}}
        application = request(1001, create)
        assert application['ok'], application
        aid = application['data']['application']['id']
        decision = {'method': 'POST', 'path': '/api/v1/applications/%s/decision' % aid,
                    'body': {'action': 'approve_adjusted', 'approved': {'gpu_hours': 7},
                             'decision_reason': 'Approved seven hours', 'uid': 0}}
        refused = request(1001, decision)
        assert not refused['ok'] and refused['error']['code'] == 'admin_required', refused
        accepted = request(0, decision)
        assert accepted['ok'] and accepted['data']['application']['status'] == 'APPROVED', accepted
        assert bridge.snapshot(1001)['granted']['gpu_seconds'] == 7 * 3600
        assert request(1001, create)['data']['application']['id'] == aid
        print('PASS: real socket peer identity enforces approval authorization and grant')

        user_state = pathlib.Path(tmp) / 'user-state'
        user_state.mkdir(mode=0o700)
        os.chown(user_state, 1001, 1001)
        admin_state = private / 'admin-state'
        admin_state.mkdir(mode=0o700)
        def cli(uid, state, *args):
            proc = subprocess.run([sys.executable, '/client/timectl.py', '--json', '--state-dir', str(state), *args],
                                  preexec_fn=become_user if uid == 1001 else None,
                                  capture_output=True, text=True, timeout=15)
            try:
                output = json.loads(proc.stdout)
            except ValueError:
                raise AssertionError((proc.returncode, proc.stdout, proc.stderr))
            assert proc.returncode == 0 and output.get('ok'), (proc.returncode, output, proc.stderr)
            return output
        capabilities = cli(1001, user_state, 'capabilities')
        assert capabilities['data']['monthly_quota']['server_state'] == 'enforcing'
        assert capabilities['data']['monthly_quota']['enforcement_ready'] is True
        message = {'jsonrpc':'2.0', 'id':1, 'method':'tools/call',
                   'params':{'name':'time_capabilities', 'arguments':{}}}
        mcp = subprocess.run([sys.executable, '/client/time_mcp.py'], input=json.dumps(message) + '\n',
                             preexec_fn=become_user, capture_output=True, text=True, timeout=15)
        assert mcp.returncode == 0, (mcp.stdout, mcp.stderr)
        mcp_result = json.loads(mcp.stdout)['result']['structuredContent']
        assert mcp_result['ok'] and mcp_result['data']['monthly_quota']['enforcement_ready'] is True
        print('PASS: real MCP capability discovery agrees with broker')
        created = cli(1001, user_state, 'request', 'create', '--kind', 'monthly_gpu_hours',
                      '--reason', 'Need another GPU training allocation', '--gpu-hours', '3',
                      '--month', str(month), '--client-request-id', 'socket-cli-request-1', '--yes')
        cli_aid = created['data']['application']['id']
        approved = cli(0, admin_state, 'admin', 'request', 'approve', cli_aid, '--yes')
        assert approved['data']['application']['status'] == 'APPROVED', approved
        assert bridge.snapshot(1001)['granted']['gpu_seconds'] == 10 * 3600
        print('PASS: actual CLI application and approval reach the real controller once')
    finally:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
