#!/usr/bin/env python3
"""Bounded local SSH/PAM device-slice check using an isolated SSH daemon.

Creates only temporary keys, a loopback-only sshd and a private device inode.
Never modifies user keys, the production SSH daemon, or existing GPU nodes.
"""
import errno
import grp
import os
from pathlib import Path
import pwd
import shlex
import signal
import socket
import stat
import subprocess
import tempfile
import time

def run(args,**kwargs):
    return subprocess.run(args,check=True,text=True,capture_output=True,timeout=15,**kwargs)

def main():
    if os.geteuid()!=0 or os.uname().nodename!='u20-Z790-S-DDR4':raise ValueError('root on u20 required')
    user=pwd.getpwnam('timzhang')
    if user.pw_uid!=1001:raise ValueError('unexpected training UID')
    members=set(grp.getgrgid(user.pw_gid).gr_mem)
    members.update(p.pw_name for p in pwd.getpwall() if p.pw_gid==user.pw_gid)
    if members!={'timzhang'}:raise ValueError('test user primary group is shared')
    device=Path('/dev/nvidia0');gpu=device.stat()
    if not stat.S_ISCHR(gpu.st_mode):raise ValueError('GPU node not character device')
    policy=run(['/usr/bin/systemctl','show','user-1001.slice','-p','DevicePolicy','--value']).stdout.strip()
    if policy!='closed':raise ValueError('training slice policy is not closed')
    with tempfile.TemporaryDirectory(prefix='time-ssh-device-',dir='/run') as d, \
            tempfile.TemporaryDirectory(prefix='.time-ssh-device-',dir='/dev') as devdir:
        root=Path(d);root.chmod(0o755);os.chown(devdir,0,user.pw_gid);Path(devdir).chmod(0o710)
        node=Path(devdir)/'gpu-probe';os.mknod(node,stat.S_IFCHR|0o666,gpu.st_rdev);os.chmod(node,0o666)
        fd=os.open(node,os.O_RDWR);os.close(fd)  # positive control in the root cgroup
        for name in ('host','client'):
            run(['/usr/bin/ssh-keygen','-q','-t','ed25519','-N','','-f',str(root/name)])
        (root/'authorized_keys').write_bytes((root/'client.pub').read_bytes());(root/'authorized_keys').chmod(0o644)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        hostkey=(root/'host.pub').read_text().split()
        (root/'known_hosts').write_text('[127.0.0.1]:%d %s %s\n'%(port,hostkey[0],hostkey[1]))
        config=root/'sshd_config'
        config.write_text('\n'.join([
            'Port %d'%port,'ListenAddress 127.0.0.1','HostKey '+str(root/'host'),
            'PidFile '+str(root/'sshd.pid'),'AuthorizedKeysFile '+str(root/'authorized_keys'),
            'UsePAM yes','AllowUsers timzhang','PasswordAuthentication no',
            'KbdInteractiveAuthentication no','PermitRootLogin no','PermitTTY no',
            'UseDNS no','X11Forwarding no','AllowTcpForwarding no','PrintMotd no',
            'LogLevel ERROR','Subsystem sftp internal-sftp','']) )
        run(['/usr/sbin/sshd','-t','-f',str(config)])
        server=subprocess.Popen(['/usr/sbin/sshd','-D','-e','-f',str(config)],
                                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,start_new_session=True)
        try:
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if server.poll() is not None:raise RuntimeError('temporary sshd exited')
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.2):break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('temporary sshd did not listen')
            code=("import errno,os; p="+repr(str(node))+"; "
                  "c=open('/proc/self/cgroup').read().splitlines(); "
                  "assert any(':devices:' in x and (x.split(':',2)[-1].endswith('/user-1001.slice') or '/user-1001.slice/' in x) for x in c),c; "
                  "exec(\"try:\\n os.open(p,os.O_RDWR)\\nexcept OSError as e:\\n assert e.errno==errno.EPERM,e\\n print('SSH_GPU_DEVICE_DENIED_BY_CGROUP')\\nelse:\\n raise AssertionError('device opened')\")")
            command='/usr/bin/python3 -c '+shlex.quote(code)
            try:
                result=run(['/usr/bin/ssh','-F','/dev/null','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
                            '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(root/'known_hosts'),
                            '-o','ConnectTimeout=5','-i',str(root/'client'),'-p',str(port),
                            'timzhang@127.0.0.1',command])
            except subprocess.CalledProcessError as exc:
                print('SSH device check failed; remote stderr:',exc.stderr[-2000:],flush=True)
                raise
            if 'SSH_GPU_DEVICE_DENIED_BY_CGROUP' not in result.stdout:
                raise ValueError('SSH login probe did not report expected denial')
            print('PASS: real SSH/PAM login placed timzhang in closed devices slice and denied GPU node',flush=True)
        finally:
            try:os.killpg(server.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid,signal.SIGKILL);server.wait(timeout=5)

if __name__=='__main__':main()
