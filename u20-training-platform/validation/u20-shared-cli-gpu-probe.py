#!/usr/bin/env python3
"""Submit one bounded PyTorch probe through shared timectl as timzhang."""
import json
import os
import pwd
import subprocess
import time
import uuid

def run_as(user,args,request=None,timeout=20):
    env=os.environ.copy();env['HOME']=user.pw_dir;env.pop('XDG_STATE_HOME',None);env.pop('TIME_STATE_DIR',None)
    return subprocess.run(['/usr/bin/setpriv','--reuid',str(user.pw_uid),'--regid',str(user.pw_gid),
                           '--init-groups',*args],input=request,text=True,capture_output=True,
                          cwd=user.pw_dir,env=env,timeout=timeout)
def api(user,path):
    p=run_as(user,['/usr/local/bin/pro6000d-training-client'],json.dumps({'method':'GET','path':path})+'\n')
    if p.returncode:raise RuntimeError('API client failed')
    result=json.loads(p.stdout)
    if not result.get('ok'):raise RuntimeError('API rejected request')
    return result['data']
def main():
    if os.geteuid()!=0 or os.uname().nodename!='u20-Z790-S-DDR4':raise ValueError('root on u20 required')
    if subprocess.run(['/usr/bin/squeue','-h','-o','%i'],capture_output=True,text=True,check=True,timeout=10).stdout.strip():
        raise ValueError('queue not empty')
    user=pwd.getpwnam('timzhang')
    admin=pwd.getpwnam('u20')
    terminal={'COMPLETED','FAILED','CANCELLED','CANCELED','TIMEOUT','NODE_FAIL','OUT_OF_MEMORY','PREEMPTED','BOOT_FAIL','DEADLINE','REJECTED','EXPIRED'}
    if any(j.get('state') not in terminal for j in api(admin,'/api/v1/jobs')['jobs']):
        raise ValueError('platform has active jobs')
    request_id='shared-cli-probe-'+uuid.uuid4().hex
    print('PROBE_REQUEST_ID '+request_id,flush=True)
    code='import torch; x=torch.ones((16,16),device="cuda"); y=x@x; torch.cuda.synchronize(); assert float(y[0,0])==16.0; print("TIME_SHARED_CLI_GPU_OK",torch.cuda.get_device_name(),flush=True)'
    args=['/usr/local/bin/timectl','run','--yes','--client-request-id',request_id,'--name','time-shared-cli-probe',
          '--gpus','1','--cpus','1','--mem','2GiB','--time','2m','--qos','debug','--workdir',user.pw_dir,
          '--environment','/home/u20/miniconda3/envs/pytorch_2.4','--output',user.pw_dir+'/time-shared-cli-%j.out',
          '--','python','-c',code]
    p=run_as(user,args,timeout=50)
    try:result=json.loads(p.stdout)
    except ValueError:raise RuntimeError('CLI outcome unknown; inspect request ID '+request_id)
    if p.returncode or not result.get('ok'):
        raise RuntimeError('CLI rejected or outcome unknown; inspect request ID '+request_id+': '+json.dumps(result.get('error'),ensure_ascii=False))
    job=result['data']['job']; jid=job['id'];print('PROBE_JOB_ID '+jid,flush=True)
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        jobs=api(user,'/api/v1/jobs')['jobs'];row=next((j for j in jobs if j['id']==jid),None)
        if row and row['state'] in ('COMPLETED','FAILED','CANCELLED','TIMEOUT','NODE_FAIL','OUT_OF_MEMORY'):
            text=api(user,'/api/v1/jobs/'+jid+'/log').get('content','')
            if row['state']!='COMPLETED' or 'TIME_SHARED_CLI_GPU_OK' not in text:
                raise RuntimeError('probe failed; inspect job '+jid+' state '+row['state'])
            print('PASS: shared CLI submitted a successful RTX 4090 PyTorch job '+jid,flush=True);return
        time.sleep(2)
    raise RuntimeError('probe still pending or running; inspect job '+jid+'; no cancellation sent')
if __name__=='__main__':main()
