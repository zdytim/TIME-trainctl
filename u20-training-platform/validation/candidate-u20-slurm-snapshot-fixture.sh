#!/bin/bash
# Candidate controller and fresh quota ledger, using a private snapshot copy.
set -euo pipefail
test -f /.dockerenv && test "${TIME_ISOLATED_VALIDATION:-}" = 1
test "$(hostname)" = u20-Z790-S-DDR4
test -f /snapshot/manifest.json && test -f /snapshot/slurm-state/job_state
base=/var/lib/time-restore-fixture
mkdir -p "$base" /run/munge
chmod 700 "$base"
chmod 755 /run/munge
cp -a /snapshot/slurm-state "$base/state"
chown -R slurm:slurm "$base/state"
mkdir "$base/spool" "$base/log"
chown slurm:slurm "$base/spool" "$base/log"
dd if=/dev/urandom of="$base/munge.key" bs=1024 count=1 status=none
chmod 600 "$base/munge.key"
export PATH=/opt/time/slurm19-7a06cfff70a7/bin:/opt/time/slurm19-ae47c8f165bf/sbin:$PATH
export LD_LIBRARY_PATH=/opt/time/slurm19-7a06cfff70a7/lib/slurm
export SLURM_CONF="$base/slurm.conf"
cat > "$SLURM_CONF" <<EOF
ClusterName=u20-training
SlurmctldHost=u20-Z790-S-DDR4
SlurmUser=slurm
AuthType=auth/munge
CredType=cred/munge
SlurmctldPort=16817
SlurmdPort=16818
StateSaveLocation=$base/state
SlurmdSpoolDir=$base/spool
SlurmctldPidFile=$base/state/slurmctld.pid
SlurmdPidFile=$base/slurmd.pid
SlurmctldLogFile=$base/log/slurmctld.log
PluginDir=/opt/time/slurm19-7a06cfff70a7/lib/slurm
SlurmctldParameters=MonthlyGpuQuota
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
ProctrackType=proctrack/linuxproc
TaskPlugin=task/none
AccountingStorageType=accounting_storage/none
MinJobAge=86400
GresTypes=gpu
NodeName=u20-Z790-S-DDR4 CPUs=32 RealMemory=112186 State=UNKNOWN Gres=gpu:1
PartitionName=gpu Nodes=u20-Z790-S-DDR4 Default=YES State=UP
EOF
chmod 755 "$base"
test ! -e "$base/state/monthly-quota.wal"
runuser -u slurm -- /opt/py310/bin/python3 /fixture/ledger-init.py --state-dir "$base/state" --new-empty-ledger
/opt/py310/bin/python3 - "$base/state" <<'PY'
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('anchor_init','/validation/time-quota-anchor-init.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.STATE=pathlib.Path(sys.argv[1]); sys.argv=['anchor-init','--new-empty-witness']; m.main()
PY
munged --force --foreground --key-file="$base/munge.key" --socket=/run/munge/munge.socket.2 \
    --pid-file="$base/munge.pid" --log-file="$base/log/munge.log" --seed-file="$base/seed" &
munge_pid=$!; controller_pid=''
cleanup() { test -z "$controller_pid" || kill "$controller_pid" 2>/dev/null || true; kill "$munge_pid" 2>/dev/null || true; }
trap cleanup EXIT
for _ in $(seq 1 50); do test -S /run/munge/munge.socket.2 && break; sleep .1; done
test -S /run/munge/munge.socket.2
/opt/py310/bin/python3 /validation/time-quota-anchor-exec.py /opt/time/slurm19-7a06cfff70a7/sbin/slurmctld -D -f "$SLURM_CONF" > "$base/log/controller.out" 2>&1 &
controller_pid=$!
ready=0
for _ in $(seq 1 100); do
    if scontrol ping > /dev/null 2>&1; then ready=1; break; fi
    kill -0 "$controller_pid" 2>/dev/null || break
    sleep .1
done
if test "$ready" != 1; then echo 'FAIL: isolated candidate controller did not start with copied state' >&2; exit 1; fi
PYTHONPATH=/broker /opt/py310/bin/python3 - "$base/state/time-quota.sock" <<'PY'
import sys
from controller_bridge import ControllerBridge
s=ControllerBridge(sys.argv[1], 64030).snapshot(1001)
assert s['tier']=='debug' and s['used']['gpu_seconds']==0 and s['capacity']['gpu_seconds']==18000, s
print('PASS: candidate reports fresh 5 GPU-hour debug quota')
PY
for id in 4 5 6 7 8; do
    scontrol show job "$id" -o > "$base/job-$id"
    grep -Eq 'JobState=(COMPLETED|FAILED)' "$base/job-$id"
done
# No node daemon exists: this new synthetic job must stay pending.
job=$(sbatch --parsable --begin=now+10minutes --wrap='true')
case "$job" in ''|*[!0-9]*) echo 'invalid synthetic job ID' >&2; exit 1;; esac
test "$job" -gt 8
scontrol show job "$job" -o > "$base/job-new"
grep -q 'JobState=PENDING' "$base/job-new"
scancel "$job"
echo 'PASS: copied production state started candidate; five old jobs and increasing next job ID verified'
