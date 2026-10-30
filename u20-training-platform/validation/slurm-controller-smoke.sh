#!/bin/bash
# Container-only smoke preparation; does not touch real Slurm state or devices.
set -eu
test "${TIME_ISOLATED_VALIDATION:-}" = 1
test -f /.dockerenv
base=/tmp/time-isolated
prefix=/opt/time/slurm19-2f57b3225733
mkdir -p "$base/state" "$base/spool" "$base/log" /run/munge
chmod 700 "$base" "$base/state" "$base/spool" /run/munge
dd if=/dev/urandom of="$base/munge.key" bs=1024 count=1 status=none
chmod 600 "$base/munge.key"
export PATH="$prefix/sbin:$PATH"
export LD_LIBRARY_PATH="$prefix/lib/slurm"
export SLURM_CONF="$base/slurm.conf"
cat > "$SLURM_CONF" <<EOF
ClusterName=time-isolated
SlurmctldHost=time-test
SlurmUser=root
AuthType=auth/munge
CredType=cred/munge
SlurmctldPort=16817
SlurmdPort=16818
StateSaveLocation=$base/state
SlurmdSpoolDir=$base/spool
SlurmctldPidFile=$base/slurmctld.pid
SlurmdPidFile=$base/slurmd.pid
SlurmctldLogFile=$base/log/slurmctld.log
SlurmdLogFile=$base/log/slurmd.log
PluginDir=$prefix/lib/slurm
SlurmctldParameters=MonthlyGpuQuota
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory
ProctrackType=proctrack/linuxproc
TaskPlugin=task/none
AccountingStorageType=accounting_storage/none
GresTypes=gpu
NodeName=time-test CPUs=2 RealMemory=256 State=UNKNOWN Gres=gpu:1
PartitionName=normal Nodes=time-test Default=YES State=UP
EOF
printf 'U20MQ1|HEADER|1\n' > "$base/state/monthly-quota.wal"
printf 'U20MQ1|CHECKPOINT|0\n' > "$base/state/monthly-quota.checkpoint"
chmod 600 "$base/state/monthly-quota.wal" "$base/state/monthly-quota.checkpoint"
munged --force --foreground --key-file="$base/munge.key" --socket=/run/munge/munge.socket.2 \
    --pid-file="$base/munge.pid" --log-file="$base/log/munge.log" --seed-file="$base/seed" &
munge_pid=$!
trap 'kill "$munge_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do test -S /run/munge/munge.socket.2 && break; sleep .1; done
if ! test -S /run/munge/munge.socket.2; then cat "$base/log/munge.log"; exit 1; fi
timeout 8 slurmctld -D -v -f "$SLURM_CONF" > "$base/log/controller.out" 2>&1 &
controller_pid=$!
for _ in $(seq 1 50); do
    if scontrol ping > "$base/log/ping.out" 2>&1 &&
       grep -Eq '^Slurmctld\(primary\) at .* is UP' "$base/log/ping.out" &&
       kill -0 "$controller_pid" 2>/dev/null; then
        cat "$base/log/ping.out"
        echo 'PASS: isolated controller responds with a fresh ledger'
        kill "$controller_pid" 2>/dev/null || true
        wait "$controller_pid" 2>/dev/null || true
        exit 0
    fi
    sleep .1
done
cat "$base/log/controller.out"
cat "$base/log/ping.out"
exit 1
