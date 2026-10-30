#!/usr/bin/env python3
"""Submit one bounded GPU correctness probe as timzhang through u20's broker.

Creates a real platform job and log. Never cancels any job except this probe,
and never cancels it after it starts. This does not exercise monthly gating.
"""
import json
import os
import pwd
import subprocess
import time
import uuid

USER = 'timzhang'
CLIENT = '/usr/local/bin/pro6000d-training-client'


def call(user, method, path, body=None):
    request = {'method': method, 'path': path}
    if body is not None: request['body'] = body
    p = subprocess.run(['/usr/bin/setpriv', '--reuid', str(user.pw_uid), '--regid', str(user.pw_gid),
                        '--init-groups', CLIENT], input=json.dumps(request)+'\n', text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=True)
    reply = json.loads(p.stdout)
    if not reply.get('ok'): raise RuntimeError('broker error: ' + json.dumps(reply.get('error'), ensure_ascii=False))
    return reply['data']


def main():
    if os.geteuid() != 0 or os.uname().nodename != 'u20-Z790-S-DDR4':
        raise SystemExit('root and expected u20 host required')
    queued = subprocess.run(['/usr/bin/squeue', '-h', '-o', '%i'], check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if queued.stdout.strip(): raise SystemExit('scheduler is not empty; probe not submitted')
    user = pwd.getpwnam(USER)
    ident = call(user, 'GET', '/api/v1/state')
    if ident['identity']['uid'] != user.pw_uid or not ident['scheduler_available']:
        raise SystemExit('user identity or scheduler not ready')
    if any(j['state'] not in ('COMPLETED','FAILED','CANCELLED','CANCELED','TIMEOUT','NODE_FAIL',
                             'OUT_OF_MEMORY','PREEMPTED','BOOT_FAIL','DEADLINE','REJECTED','EXPIRED')
           for j in ident['jobs']):
        raise SystemExit('user already has active platform jobs; probe not submitted')
    request_id = 'gpu-probe-' + uuid.uuid4().hex
    code = 'import torch; assert torch.cuda.is_available(); x=torch.ones((16,16),device="cuda"); y=x@x; torch.cuda.synchronize(); assert float(y[0,0])==16.0; print("TIME_GPU_TORCH_OK",torch.cuda.get_device_name(),float(y[0,0]),flush=True)'
    payload = {'client_request_id':request_id, 'name':'time-gpu-probe', 'qos':'debug',
               'gpu':1, 'cpu':1, 'memory_mb':2048, 'time_minutes':2,
               'workdir':user.pw_dir, 'environment':'/home/u20/miniconda3/envs/pytorch_2.4',
               'output_path':user.pw_dir+'/time-gpu-probe-%j.out',
               'command':"python -c '"+code+"'"}
    call(user, 'POST', '/api/v1/jobs/validate', payload)
    response = call(user, 'POST', '/api/v1/jobs', payload)
    job = response['job']; jid = job['id']
    print('PROBE_SUBMITTED id=%s scheduler_id=%s state=%s' % (jid, job.get('scheduler_id'), job['state']), flush=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        rows = call(user, 'GET', '/api/v1/jobs')['jobs']
        job = next((j for j in rows if j['id'] == jid), None)
        if not job: raise RuntimeError('submitted probe no longer visible: ' + jid)
        if job['state'] in ('COMPLETED','FAILED','CANCELLED','CANCELED','TIMEOUT','NODE_FAIL',
                            'OUT_OF_MEMORY','PREEMPTED','BOOT_FAIL','DEADLINE','REJECTED','EXPIRED'):
            result = call(user, 'GET', '/api/v1/jobs/'+jid+'/log')
            print('PROBE_RESULT id=%s scheduler_id=%s state=%s' % (jid, job.get('scheduler_id'), job['state']))
            print(result.get('content', '')[-4096:])
            if job['state'] != 'COMPLETED' or 'TIME_GPU_TORCH_OK' not in result.get('content', ''):
                raise RuntimeError('probe did not complete successfully')
            return
        time.sleep(2)
    if job['state'] in ('WAITING_QUOTA','PENDING'):
        print('PROBE_CANCEL_PENDING id='+jid, flush=True)
        call(user, 'POST', '/api/v1/jobs/'+jid+'/cancel', {})
    raise RuntimeError('probe did not reach a terminal state; inspect id='+jid+' state='+job['state'])


if __name__ == '__main__': main()
