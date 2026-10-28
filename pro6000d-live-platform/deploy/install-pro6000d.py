#!/usr/bin/python3
"""Install the platform alongside live Slurm. Never stop/restart Slurm daemons."""
import ast
import datetime
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import tempfile
import time

ROOT=Path(__file__).resolve().parent.parent
def run(argv, check=True):
    r=subprocess.run(argv,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=40)
    if check and r.returncode: raise RuntimeError('%s: %s'%(argv[0],r.stderr[-3000:]))
    return r

def main():
    if os.geteuid()!=0: raise SystemExit('root required')
    conf=Path('/etc/slurm/slurm.conf')
    original=conf.read_text()
    if 'ClusterName=pro6000d' not in original or 'Gres=gpu:4' not in original:
        raise SystemExit('Expected existing pro6000d four-GPU cluster')
    version=run(['/opt/slurm/current/bin/scontrol','version']).stdout
    if '26.05.5' not in version: raise SystemExit('Review native plugin ABI for this Slurm version first')
    for p in [ROOT/'backend/pro6000d-training-api.py',ROOT/'backend/pro6000d-training-client.py']:
        ast.parse(p.read_text())
    gpu=run(['/usr/bin/nvidia-smi','--query-gpu=index','--format=csv,noheader']).stdout.splitlines()
    if len(gpu)!=4: raise SystemExit('Run outside the restricted user.slice; expected four working GPUs')
    running=run(['/opt/slurm/current/bin/squeue','-h','-o','%i|%u|%T']).stdout
    stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup=Path('/var/backups/pro6000d-training')/('deployment-'+stamp)
    backup.mkdir(parents=True,mode=0o700);os.chmod(backup,0o700)
    manifest={'files':[],'new_groups':[],'added_memberships':[],'queue_before':running,'complete':False}
    def save(): (backup/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    def preserve(path):
        path=Path(path)
        if any(x['path']==str(path) for x in manifest['files']): return
        record={'path':str(path),'existed':path.exists()}
        if path.exists():
            target=backup/'files'/str(path).lstrip('/')
            target.parent.mkdir(parents=True,exist_ok=True)
            if path.is_dir(): shutil.copytree(path,target)
            else: shutil.copy2(path,target)
        manifest['files'].append(record);save()
    def write(path,text,mode=0o644):
        path=Path(path);preserve(path);path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.platform-')
        with os.fdopen(fd,'w') as f: f.write(text)
        os.chmod(tmp,mode);os.replace(tmp,path)
    def copy(src,dest,mode=0o644): write(dest,Path(src).read_text(),mode)
    print('Backup: '+str(backup),flush=True)
    # The plugin is built against the installed public headers, not a new Slurm.
    plugin=Path('/opt/slurm/current/lib/slurm/job_submit_pro6000d.so').resolve()
    compiled=backup/'job_submit_pro6000d.so'
    run(['gcc','-shared','-fPIC','-O2','-Wall','-Wextra','-I/opt/slurm/current/include',
         str(ROOT/'deploy/job_submit_pro6000d.c'),'-o',str(compiled)])
    preserve(plugin)
    fd,staged=tempfile.mkstemp(dir=plugin.parent,prefix='.platform-plugin-')
    with os.fdopen(fd,'wb') as stream: stream.write(compiled.read_bytes())
    os.chmod(staged,0o755);os.replace(staged,plugin)
    for name in ['pro6000d-training','pro6000d-admin','pro6000d-gpu']:
        try: grp.getgrnam(name)
        except KeyError: run(['groupadd','--system',name]);manifest['new_groups'].append(name);save()
    names=['ubuntu','timzhang']+['time%d'%i for i in range(41,56)]
    for name in names:
        user=pwd.getpwnam(name)
        for group in ['pro6000d-training']+(['pro6000d-admin'] if name=='ubuntu' else []):
            if grp.getgrnam(group).gr_gid not in os.getgrouplist(name,user.pw_gid):
                run(['usermod','-a','-G',group,name]);manifest['added_memberships'].append([name,group]);save()
    for p in ['/var/lib/pro6000d-training','/var/lib/pro6000d-training/jobs']:
        Path(p).mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(p,0o700)
    cfg=json.loads((ROOT/'backend/config.example.json').read_text())
    cfg.update(display_name='pro6000d',slurm_conf='/etc/slurm/slurm.conf',slurm_account='training',
               import_external_jobs=True,admission_plugin_name='pro6000d',
               admission_plugin_binary=str(plugin),admission_plugin_sha256=hashlib.sha256(plugin.read_bytes()).hexdigest(),
               capacity={'gpu':4,'cpu':128,'memory_mb':229376},max_requested_gpu=4,
               default_quota={'gpu_limit':4,'concurrent_gpu_limit':4,'concurrent_job_limit':4,'active_job_limit':32},
               user_roots=['/home/{username}','/srv/pro6000d/users/{username}','/mnt/sdata/{username}'],
               jobcomp_log='/var/log/slurm/jobcomp-platform.log')
    configpath=Path('/etc/pro6000d-training/config.json')
    if configpath.exists():
        previous=json.loads(configpath.read_text())
        for k in ['default_quota','qos']:
            if k in previous: cfg[k]=previous[k]
    write(configpath,json.dumps(cfg,indent=2,ensure_ascii=False)+'\n',0o640)
    copy(ROOT/'backend/pro6000d-training-api.py','/usr/local/lib/pro6000d-training/pro6000d-training-api.py')
    copy(ROOT/'backend/pro6000d-training-client.py','/usr/local/bin/pro6000d-training-client',0o755)
    copy(ROOT/'deploy/backend.service','/etc/systemd/system/pro6000d-training.service')
    write('/etc/systemd/system/pro6000d-training.service.d/slurm.conf','[Service]\nEnvironment=SLURM_CONF=/etc/slurm/slurm.conf\n')
    pluginroot=Path('/usr/share/cockpit/pro6000d-training');preserve(pluginroot)
    if pluginroot.exists(): shutil.rmtree(pluginroot)
    shutil.copytree(ROOT/'cockpit-plugin/pro6000d-training',pluginroot)
    for p in [pluginroot]+list(pluginroot.rglob('*')): os.chmod(p,0o755 if p.is_dir() else 0o644)
    # Existing global user.slice already excludes GPUs. Preserve it and live jobs.
    if 'DevicePolicy=closed' not in Path('/etc/systemd/system/user.slice.d/50-pro6000d-gpu.conf').read_text():
        raise RuntimeError('Expected existing login GPU isolation; review before enrollment')
    copy(ROOT/'deploy/session-guard.py','/usr/local/libexec/pro6000d-session-guard',0o755)
    marker='# BEGIN pro6000d training policy'
    pam=Path('/etc/pam.d/common-session')
    if marker not in pam.read_text():
        write(pam,pam.read_text()+'\n'+marker+'\nsession required pam_exec.so quiet /usr/local/libexec/pro6000d-session-guard\n# END pro6000d training policy\n')
    for name in ['cron','atd']:
        pam=Path('/etc/pam.d')/name
        if pam.exists() and marker not in pam.read_text():
            write(pam,marker+'\naccount [success=1 default=ignore] pam_succeed_if.so quiet user ingroup pro6000d-admin\naccount requisite pam_succeed_if.so quiet user notingroup pro6000d-training\n# END pro6000d training policy\n'+pam.read_text())
    # Do not change live GPU ACLs: legacy Slurm steps retain their original GID.
    # Existing global user.slice device isolation and PAM boundaries remain in force.
    slurm=pwd.getpwnam('slurm');jobcomp=Path(cfg['jobcomp_log'])
    jobcomp.touch(exist_ok=True);os.chown(jobcomp,slurm.pw_uid,slurm.pw_gid);os.chmod(jobcomp,0o640)
    updated=original
    settings={'JobSubmitPlugins':'pro6000d','JobCompType':'jobcomp/filetxt','JobCompLoc':str(jobcomp),'MinJobAge':'86400'}
    for key,value in settings.items():
        updated=re.sub(r'^'+key+r'\s*=.*\n?','',updated,flags=re.M)
        updated+='\n'+key+'='+value+'\n'
    # Bring up and read the broker before committing the admission gate. A broker
    # startup problem must never strand existing users behind a new submit policy.
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','pro6000d-training']);run(['systemctl','restart','pro6000d-training'])
    for _ in range(30):
        if Path('/run/pro6000d-training/backend.sock').exists(): break
        time.sleep(.2)
    else: raise RuntimeError('Backend socket did not appear; original admission remains active')
    run(['systemctl','is-active','--quiet','pro6000d-training'])
    probe=subprocess.run(['setpriv','--reuid=ubuntu','--regid=ubuntu','--init-groups',
                          '/usr/local/bin/pro6000d-training-client'],
                         input='{"method":"GET","path":"/api/v1/state"}\n',
                         text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=40)
    response=json.loads(probe.stdout)
    if not response.get('ok') or not response['data'].get('scheduler_available') or len(response['data'].get('gpus',{}).get('devices',[]))!=4:
        raise RuntimeError('Broker state health failed; original admission remains active: '+probe.stdout[:1000])
    write(conf,updated)
    try:
        run(['/opt/slurm/current/bin/scontrol','reconfigure'])
        actual=run(['/opt/slurm/current/bin/scontrol','show','config']).stdout
        if not re.search(r'JobSubmitPlugins\s*=\s*pro6000d\s',actual):
            raise RuntimeError('Native admission did not activate')
        run(['systemctl','is-active','--quiet','pro6000d-training'])
        manifest['queue_after']=run(['/opt/slurm/current/bin/squeue','-h','-o','%i|%u|%T']).stdout
    except Exception:
        conf.write_text(original)
        run(['/opt/slurm/current/bin/scontrol','reconfigure'],False)
        raise
    manifest['complete']=True;save()
    print('Platform installed at existing Cockpit port9091. Existing Slurm was reconfigured, not restarted.')
    print('Queue after:\n'+manifest['queue_after'])

if __name__=='__main__': main()
