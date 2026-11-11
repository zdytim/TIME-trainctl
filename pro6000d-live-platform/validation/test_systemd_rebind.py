#!/usr/bin/env python3
"""Exercise candidate systemd dependency behavior in the GPU-free Colima VM."""
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=15)


def main():
    if (os.geteuid() != 0 or os.uname().nodename != 'colima' or
            run('systemd-detect-virt').stdout.strip() != 'apple' or
            any(Path('/dev').glob('nvidia*'))):
        raise RuntimeError('requires root in GPU-free Colima')
    deploy = Path(__file__).resolve().parents[1] / 'deploy'
    node_unit = (deploy/'slurmd-monthly.conf').read_text().split('[Service]',1)[0]
    controller_unit = (deploy/'slurmctld-monthly.conf').read_text().split('[Service]',1)[0]
    if not all(x in node_unit for x in ('After=\n', 'Before=slurmctld.service', 'Wants=slurmctld.service')):
        raise RuntimeError('node dependency drop-in differs from tested design')
    if not all(x in controller_unit for x in ('Requires=slurmd.service', 'BindsTo=slurmd.service', 'After=slurmd.service', 'PartOf=slurmd.service')):
        raise RuntimeError('controller dependency drop-in differs from tested design')
    token = uuid.uuid4().hex[:12]
    node, controller = f'time26-node-{token}.service', f'time26-controller-{token}.service'
    unit_dir = Path('/run/systemd/system')
    paths = (unit_dir/node, unit_dir/controller)
    for path in paths:
        if path.exists(): raise RuntimeError('unexpected existing test unit')
    paths[0].write_text(f'''[Unit]\nWants={controller}\nBefore={controller}\n[Service]\nType=simple\nExecStart=/bin/sleep 300\nRestart=on-failure\nRestartSec=1\n''')
    paths[1].write_text(f'''[Unit]\nRequires={node}\nBindsTo={node}\nAfter={node}\nPartOf={node}\n[Service]\nType=simple\nExecStart=/bin/sleep 300\nRestart=on-failure\nRestartSec=1\n''')
    def pid(unit):
        result=run('systemctl','show','-p','MainPID','--value',unit,check=False)
        return int(result.stdout.strip() or '0')
    def wait_both(old_node=0, old_ctl=0):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            n,c=pid(node),pid(controller)
            if n and c and n!=old_node and c!=old_ctl and run('systemctl','is-active','--quiet',node,controller,check=False).returncode == 0:
                return n,c
            time.sleep(.2)
        raise RuntimeError(f'dependency restart failed: node={pid(node)} controller={pid(controller)}')
    try:
        run('systemctl','daemon-reload')
        run('systemctl','start',controller)
        n,c=wait_both()
        run('systemctl','restart',node)
        n,c=wait_both(n,c)
        os.kill(n,signal.SIGKILL)
        wait_both(n,c)
        print('PASS: node service restart and crash restart its bound controller')
    finally:
        run('systemctl','stop',controller,node,check=False)
        for path in paths:
            path.unlink(missing_ok=True)
        run('systemctl','daemon-reload',check=False)

if __name__ == '__main__':
    main()
