#!/bin/bash
# Container-only smoke preparation; does not touch real Slurm state or devices.
set -eu
test "${TIME_ISOLATED_VALIDATION:-}" = 1
test -f /.dockerenv
base=/tmp/time-isolated
prefix=${TIME_VALIDATION_PREFIX:-/opt/time/slurm19-2f57b3225733}
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
SlurmdParameters=config_overrides
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
if test "${TIME_EXHAUSTED_TEST:-0}" = 1; then
    now=$(date +%s)
    start=$((now - 544500))
    test "$(date -u -d "@$((now + 28800))" +%Y%m)" = "$(date -u -d "@$((start + 28800))" +%Y%m)"
    printf 'U20MQ1|HEADER|1\nU20MQ1|1|PREPARE|900000|0|1|4|%s|0\nU20MQ1|2|COMMIT|900000|1\nU20MQ1|3|RELEASE|900000|1|%s|0\n' "$start" "$now" > "$base/state/monthly-quota.wal"
    printf 'U20MQ1|CHECKPOINT|3\n' > "$base/state/monthly-quota.checkpoint"
else
    printf 'U20MQ1|HEADER|1\n' > "$base/state/monthly-quota.wal"
    printf 'U20MQ1|CHECKPOINT|0\n' > "$base/state/monthly-quota.checkpoint"
fi
if test "${TIME_EXHAUSTED_TEST:-0}" != 0; then
    touch "$base/fakegpu"
    printf 'NodeName=time-test Name=gpu File=%s/fakegpu\n' "$base" > "$base/gres.conf"
fi
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
        if test "${TIME_EXHAUSTED_TEST:-0}" != 0; then
            if test "$TIME_EXHAUSTED_TEST" = 1; then
                /validation/quota-socket-query root
            else
                /validation/quota-socket-query root-eligible
            fi
            slurmd -D -f "$SLURM_CONF" > "$base/log/slurmd.out" 2>&1 &
            node_pid=$!
            for _ in $(seq 1 100); do
                if scontrol show node time-test > "$base/log/node.out" 2>&1 &&
                   grep -q 'State=IDLE' "$base/log/node.out"; then break; fi
                sleep .1
            done
            if ! grep -q 'State=IDLE' "$base/log/node.out"; then
                cat "$base/log/slurmd.out" "$base/log/node.out"; exit 1
            fi
            job=$(sbatch --parsable --time=00:01:00 --gres=gpu:1 --chdir="$base" --output="$base/slurm-%j.out" --wrap='sleep 30')
            sleep 3
            scontrol show job "$job" > "$base/log/job.out"
            cat "$base/log/job.out"
            if test "$TIME_EXHAUSTED_TEST" = 1; then
                grep -q 'JobState=PENDING' "$base/log/job.out"
                grep -Eq 'NodeList=\(null\)' "$base/log/job.out"
                grep -q 'MonthlyGpuQuota:monthly_capacity_exhausted' "$base/log/job.out"
                echo 'PASS: exhausted synthetic user remains pending without a node allocation'
            else
                if ! grep -q 'JobState=RUNNING' "$base/log/job.out"; then
                    cat "$base/log/controller.out" "$base/log/slurmd.out"
                    exit 1
                fi
                echo 'PASS: eligible synthetic user starts a GPU job'
            fi
            kill "$node_pid" 2>/dev/null || true
        else
            /validation/quota-socket-query
        fi
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
