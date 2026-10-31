#!/usr/bin/env python3
"""Create and reject one clearly labelled live test request, without a grant."""
import json
import os
import pwd
import subprocess
import uuid

CLIENT='/usr/local/bin/pro6000d-training-client'
def call(user,method,path,body=None):
    req={'method':method,'path':path}
    if body is not None:req['body']=body
    p=subprocess.run(['/usr/bin/setpriv','--reuid',str(user.pw_uid),'--regid',str(user.pw_gid),
                      '--init-groups',CLIENT],input=json.dumps(req,ensure_ascii=False)+'\n',
                     text=True,capture_output=True,timeout=15,check=True)
    return json.loads(p.stdout)
def ok(reply):
    if not reply.get('ok'):raise RuntimeError('API failure: '+json.dumps(reply.get('error'),ensure_ascii=False))
    return reply['data']
def main():
    if os.geteuid()!=0 or os.uname().nodename!='u20-Z790-S-DDR4':raise ValueError('root on u20 required')
    trainee=pwd.getpwnam('timzhang');admin=pwd.getpwnam('u20')
    initial=ok(call(trainee,'GET','/api/v1/state'))
    monthly=initial['monthly_quota']
    if not monthly.get('enforcement_ready') or not monthly.get('request_available'):
        raise ValueError('monthly requests not ready')
    month=int(monthly['month'].replace('-',''))
    granted=monthly['additional_gpu_seconds']
    request={'kind':'monthly_gpu_hours','reason':'[系统验收] 测试申请，将由管理员拒绝，不追加额度',
             'requested':{'gpu_hours':1,'month':month,'client_request_id':'acceptance-'+uuid.uuid4().hex}}
    application=ok(call(trainee,'POST','/api/v1/applications',request))['application']
    aid=application['id'];print('TEST_APPLICATION_CREATED id='+aid,flush=True)
    if application['status']!='PENDING':raise ValueError('unexpected initial request state')
    decision={'action':'reject','decision_reason':'[系统验收] 测试完成，拒绝测试申请'}
    path='/api/v1/applications/'+aid+'/decision'
    unauthorized=call(trainee,'POST',path,decision)
    if unauthorized.get('ok') or unauthorized.get('error',{}).get('code')!='admin_required':
        raise ValueError('non-admin rejection was not denied; inspect application '+aid)
    rejected=ok(call(admin,'POST',path,decision))['application']
    if rejected['status']!='REJECTED':raise ValueError('test request not rejected: '+aid)
    final=ok(call(trainee,'GET','/api/v1/state'))['monthly_quota']
    if final['additional_gpu_seconds']!=granted:raise ValueError('unexpected change in granted hours: '+aid)
    print('PASS: training user requested, non-admin decision refused, admin rejected, granted hours unchanged; id='+aid,flush=True)

if __name__=='__main__':main()
