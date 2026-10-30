#!/bin/bash
# Container-only smoke preparation; does not touch real Slurm state or devices.
set -eu
test "${TIME_ISOLATED_VALIDATION:-}" = 1
test -f /.dockerenv
base=/tmp/time-isolated
prefix=${TIME_VALIDATION_PREFIX:-/opt/time/slurm19-2f57b3225733}
gpu_count=${TIME_FAKE_GPU_COUNT:-1}
case "$gpu_count" in 1|4) ;; *) echo 'Only 1 or 4 synthetic GPUs supported' >&2; exit 2;; esac
request_gpus=${TIME_REQUEST_GPUS:-$gpu_count}
case "$request_gpus" in 1|4) ;; *) exit 2;; esac
test "$request_gpus" -le "$gpu_count"
export TIME_EXPECT_GPUS="$request_gpus"
gpu_spec="gpu:$gpu_count"
request_spec="gpu:$request_gpus"
if test "${TIME_EXHAUSTED_TEST:-0}" = 14; then gpu_spec="gpu:fixture:$gpu_count"; request_spec="gpu:fixture:$request_gpus"; fi
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
NodeName=time-test CPUs=2 RealMemory=1024 State=UNKNOWN Gres=$gpu_spec
PartitionName=normal Nodes=time-test Default=YES State=UP
EOF
if test "${TIME_EXHAUSTED_TEST:-0}" = 13; then
    now=$(date +%s)
    normal_start=$((now - 94500))
    important_start=$((now - 4500))
    test "$(date -u -d "@$((now + 28800))" +%Y%m)" = "$(date -u -d "@$((normal_start + 28800))" +%Y%m)"
    printf 'U20MQ1|HEADER|1\nU20MQ1|1|PREPARE|900000|1003|1|4|%s|0\nU20MQ1|2|COMMIT|900000|1\nU20MQ1|3|RELEASE|900000|1|%s|0\nU20MQ1|4|PREPARE|900001|1002|2|4|%s|0\nU20MQ1|5|COMMIT|900001|2\nU20MQ1|6|RELEASE|900001|2|%s|0\n' "$normal_start" "$now" "$important_start" "$now" > "$base/state/monthly-quota.wal"
    printf 'U20MQ1|CHECKPOINT|6\n' > "$base/state/monthly-quota.checkpoint"
    chmod 755 "$base"
    mkdir "$base/out"
    chmod 777 "$base/out"
    touch "$base/out/order"
    chmod 666 "$base/out/order"
elif test "${TIME_EXHAUSTED_TEST:-0}" = 1 || test "${TIME_EXHAUSTED_TEST:-0}" = 3 || test "${TIME_EXHAUSTED_TEST:-0}" = 4 || test "${TIME_EXHAUSTED_TEST:-0}" = 5 || test "${TIME_EXHAUSTED_TEST:-0}" = 6; then
    now=$(date +%s)
    seeded=${TIME_SEED_GPU_SECONDS:-2178000}
    test "$seeded" -gt 0 && test $((seeded % 4)) -eq 0
    start=$((now - seeded / 4))
    test "$(date -u -d "@$((now + 28800))" +%Y%m)" = "$(date -u -d "@$((start + 28800))" +%Y%m)"
    printf 'U20MQ1|HEADER|1\nU20MQ1|1|PREPARE|900000|0|1|4|%s|0\nU20MQ1|2|COMMIT|900000|1\nU20MQ1|3|RELEASE|900000|1|%s|0\n' "$start" "$now" > "$base/state/monthly-quota.wal"
    printf 'U20MQ1|CHECKPOINT|3\n' > "$base/state/monthly-quota.checkpoint"
else
    printf 'U20MQ1|HEADER|1\n' > "$base/state/monthly-quota.wal"
    printf 'U20MQ1|CHECKPOINT|0\n' > "$base/state/monthly-quota.checkpoint"
fi
if test "${TIME_EXHAUSTED_TEST:-0}" = 7 || test "${TIME_EXHAUSTED_TEST:-0}" = 8; then
    case "${TIME_CORRUPT_KIND:-missing}" in
        missing) rm "$base/state/monthly-quota.checkpoint" ;;
        truncated) printf 'U20MQ1|1|PREPARE' >> "$base/state/monthly-quota.wal" ;;
        none) ;;
        *) exit 2 ;;
    esac
    corrupt_wal_hash=$(sha256sum "$base/state/monthly-quota.wal" | cut -d' ' -f1)
fi
if test "${TIME_EXHAUSTED_TEST:-0}" = 11; then
    printf 'preserve-me\n' > "$base/state/time-quota.sock"
    chmod 600 "$base/state/time-quota.sock"
fi
listener_pid=''
if test "${TIME_EXHAUSTED_TEST:-0}" = 12; then
    /validation/quota-socket-query listen &
    listener_pid=$!
    for _ in $(seq 1 30); do test -S "$base/state/time-quota.sock" && break; sleep .1; done
    test -S "$base/state/time-quota.sock"
    original_socket=$(stat -c '%d:%i' "$base/state/time-quota.sock")
fi
if test "${TIME_EXHAUSTED_TEST:-0}" = 1 || test "${TIME_EXHAUSTED_TEST:-0}" = 2 || test "${TIME_EXHAUSTED_TEST:-0}" = 4 || test "${TIME_EXHAUSTED_TEST:-0}" = 7 || test "${TIME_EXHAUSTED_TEST:-0}" = 8 || test "${TIME_EXHAUSTED_TEST:-0}" = 9 || test "${TIME_EXHAUSTED_TEST:-0}" = 10 || test "${TIME_EXHAUSTED_TEST:-0}" = 13 || test "${TIME_EXHAUSTED_TEST:-0}" = 14; then
    if test "$gpu_count" = 4; then
        touch "$base/fakegpu0" "$base/fakegpu1" "$base/fakegpu2" "$base/fakegpu3"
        printf 'NodeName=time-test Name=gpu File=%s/fakegpu[0-3]\n' "$base" > "$base/gres.conf"
    else
        touch "$base/fakegpu"
        if test "${TIME_EXHAUSTED_TEST:-0}" = 14; then
            printf 'NodeName=time-test Name=gpu Type=fixture File=%s/fakegpu\n' "$base" > "$base/gres.conf"
        else
            printf 'NodeName=time-test Name=gpu File=%s/fakegpu\n' "$base" > "$base/gres.conf"
        fi
    fi
fi
chmod 600 "$base/state/monthly-quota.wal"
if test -f "$base/state/monthly-quota.checkpoint"; then chmod 600 "$base/state/monthly-quota.checkpoint"; fi
munged --force --foreground --key-file="$base/munge.key" --socket=/run/munge/munge.socket.2 \
    --pid-file="$base/munge.pid" --log-file="$base/log/munge.log" --seed-file="$base/seed" &
munge_pid=$!
trap 'kill "$munge_pid" 2>/dev/null || true; if test -n "$listener_pid"; then kill "$listener_pid" 2>/dev/null || true; fi' EXIT
for _ in $(seq 1 30); do test -S /run/munge/munge.socket.2 && break; sleep .1; done
if ! test -S /run/munge/munge.socket.2; then cat "$base/log/munge.log"; exit 1; fi
timeout 45 slurmctld -D -v -f "$SLURM_CONF" > "$base/log/controller.out" 2>&1 &
controller_pid=$!
for _ in $(seq 1 50); do
    if scontrol ping > "$base/log/ping.out" 2>&1 &&
       grep -Eq '^Slurmctld\(primary\) at .* is UP' "$base/log/ping.out" &&
       kill -0 "$controller_pid" 2>/dev/null; then
        cat "$base/log/ping.out"
        if test "${TIME_EXHAUSTED_TEST:-0}" = 12; then
            test -S "$base/state/time-quota.sock"
            test "$(stat -c '%d:%i' "$base/state/time-quota.sock")" = "$original_socket"
            kill -0 "$listener_pid"
            grep -q 'monthly quota control socket unavailable' "$base/log/controller.out"
            echo 'PASS: existing active listener was preserved'
        elif test "${TIME_EXHAUSTED_TEST:-0}" = 11; then
            test -f "$base/state/time-quota.sock"
            test "$(cat "$base/state/time-quota.sock")" = preserve-me
            grep -q 'monthly quota control socket unavailable' "$base/log/controller.out"
            echo 'PASS: unrelated regular file was preserved'
        elif test "${TIME_EXHAUSTED_TEST:-0}" = 5 || test "${TIME_EXHAUSTED_TEST:-0}" = 6; then
            /validation/quota-socket-query grant 20 grant-one
            /validation/quota-socket-query grant 20 grant-one
            /validation/quota-socket-query grant-conflict 24 grant-one
            /validation/quota-socket-query root-overage
            /validation/quota-socket-query grant 40 grant-two
            /validation/quota-socket-query root-granted
            echo 'PASS: grants offset overage and retries remain idempotent'
            if test "$TIME_EXHAUSTED_TEST" = 6; then
                kill "$controller_pid"
                wait "$controller_pid" 2>/dev/null || true
                test ! -e "$base/state/time-quota.sock"
                timeout 45 slurmctld -D -v -f "$SLURM_CONF" > "$base/log/controller-restart.out" 2>&1 &
                controller_pid=$!
                ready=0
                for _ in $(seq 1 50); do
                    if scontrol ping > "$base/log/ping-restart.out" 2>&1 &&
                       grep -Eq '^Slurmctld\(primary\) at .* is UP' "$base/log/ping-restart.out"; then
                        ready=1; break
                    fi
                    sleep .1
                done
                if test "$ready" != 1; then cat "$base/log/controller-restart.out"; exit 1; fi
                /validation/quota-socket-query root-granted
                echo 'PASS: ledger and grants survived isolated controller restart'
            fi
        elif test "${TIME_EXHAUSTED_TEST:-0}" = 3; then
            /validation/quota-socket-query root-tier
            echo 'PASS: expected monthly tier at seeded usage'
        elif test "${TIME_EXHAUSTED_TEST:-0}" != 0; then
            if test "$TIME_EXHAUSTED_TEST" = 1; then
                /validation/quota-socket-query root
            elif test "$TIME_EXHAUSTED_TEST" = 4; then
                TIME_EXPECT_TIER=2 /validation/quota-socket-query root-tier
            elif test "$TIME_EXHAUSTED_TEST" = 7 || test "$TIME_EXHAUSTED_TEST" = 8; then
                if test "${TIME_CORRUPT_KIND:-missing}" = none; then
                    /validation/quota-socket-query root-eligible
                else
                    /validation/quota-socket-query root-unavailable
                fi
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
            if test "$TIME_EXHAUSTED_TEST" = 8; then
                job=$(sbatch --parsable --time=00:01:00 --mem=64M --chdir="$base" --output="$base/slurm-%j.out" --wrap='sleep 30')
            else
                job=$(sbatch --parsable --time=00:01:00 --mem=64M --gres="$request_spec" --chdir="$base" --output="$base/slurm-%j.out" --wrap='sleep 30')
            fi
            if test "$TIME_EXHAUSTED_TEST" = 7 || test "$TIME_EXHAUSTED_TEST" = 8; then sleep 10; else sleep 3; fi
            scontrol show job "$job" > "$base/log/job.out"
            cat "$base/log/job.out"
            if test "$TIME_EXHAUSTED_TEST" = 14; then
                grep -q 'JobState=PENDING' "$base/log/job.out"
                grep -Eq 'NodeList=\(null\)' "$base/log/job.out"
                grep -q 'MonthlyGpuQuota:typed' "$base/log/job.out"
                echo 'PASS: unsupported typed GPU request remains unallocated'
            elif test "$TIME_EXHAUSTED_TEST" = 8; then
                grep -q 'JobState=RUNNING' "$base/log/job.out"
                echo "PASS: CPU-only job runs alone with ledger fixture ${TIME_CORRUPT_KIND:-missing}"
            elif test "$TIME_EXHAUSTED_TEST" = 7; then
                grep -q 'JobState=PENDING' "$base/log/job.out"
                grep -Eq 'NodeList=\(null\)' "$base/log/job.out"
                echo 'OBSERVED: GPU job is pending and unallocated; reason may be stale'
                cpu=$(sbatch --parsable --time=00:01:00 --mem=64M --chdir="$base" --output="$base/cpu-%j.out" --wrap='sleep 30')
                sleep 5
                scontrol show job "$cpu" > "$base/log/cpu.out"
                cat "$base/log/cpu.out"
                grep -q 'JobState=PENDING' "$base/log/cpu.out"
                grep -Eq 'NodeList=\(null\)' "$base/log/cpu.out"
                test "$(sha256sum "$base/state/monthly-quota.wal" | cut -d' ' -f1)" = "$corrupt_wal_hash"
                echo 'OBSERVED: mixed queue is conservatively paused; damaged WAL remains unchanged'
            elif test "$TIME_EXHAUSTED_TEST" = 1; then
                grep -q 'JobState=PENDING' "$base/log/job.out"
                grep -Eq 'NodeList=\(null\)' "$base/log/job.out"
                grep -q 'MonthlyGpuQuota:monthly_capacity_exhausted' "$base/log/job.out"
                echo 'PASS: exhausted synthetic user remains pending without a node allocation'
            else
                if ! grep -q 'JobState=RUNNING' "$base/log/job.out"; then
                    cat "$base/log/controller.out" "$base/log/slurmd.out"
                    if test -f "$base/slurm-$job.out"; then cat "$base/slurm-$job.out"; fi
                    cat "$base/state/monthly-quota.wal"
                    exit 1
                fi
                if test "$TIME_EXHAUSTED_TEST" = 13; then
                    for pair in 1003:normal 1002:important 1001:debug; do
                        uid=${pair%:*}; label=${pair#*:}
                        sbatch --parsable --uid="$uid" --gid="$uid" --time=00:01:00 --mem=64M --gres=gpu:1 --chdir="$base/out" --output="$base/out/$label-%j.out" --wrap="echo $label >> $base/out/order; sleep 2"
                    done
                    scancel "$job"
                    for _ in $(seq 1 35); do
                        test "$(wc -l < "$base/out/order")" -eq 3 && break
                        sleep 1
                    done
                    cat "$base/out/order"
                    test "$(cat "$base/out/order")" = "$(printf 'debug\nimportant\nnormal')"
                    echo 'PASS: debug, important and normal queued jobs start in tier order'
                elif test "$TIME_EXHAUSTED_TEST" = 10; then
                    sleep 2
                    /validation/quota-socket-query root-active
                    child=$(ps -o pid= --ppid "$controller_pid" | tr -d ' ')
                    test -n "$child" && test "$(ps -o comm= -p "$child")" = slurmctld
                    kill -KILL "$child"
                    wait "$controller_pid" 2>/dev/null || true
                    test -S "$base/state/time-quota.sock"
                    timeout 45 slurmctld -D -v -f "$SLURM_CONF" > "$base/log/controller-crash-restart.out" 2>&1 &
                    controller_pid=$!
                    ready=0
                    for _ in $(seq 1 80); do
                        if scontrol ping > "$base/log/ping-restart.out" 2>&1 &&
                           grep -Eq '^Slurmctld\(primary\) at .* is UP' "$base/log/ping-restart.out"; then ready=1; break; fi
                        sleep .1
                    done
                    if test "$ready" != 1; then cat "$base/log/controller-crash-restart.out"; exit 1; fi
                    grep -q 'removed verified stale control socket' "$base/log/controller-crash-restart.out"
                    /validation/quota-socket-query root-active
                    scontrol show job "$job" | grep -q 'JobState=RUNNING'
                    echo 'PASS: isolated job, accounting, and quota socket recover after abrupt restart'
                elif test "$TIME_EXHAUSTED_TEST" = 9; then
                    sleep 2
                    /validation/quota-socket-query root-active
                    scontrol shutdown
                    wait "$controller_pid" 2>/dev/null || true
                    test ! -e "$base/state/time-quota.sock"
                    timeout 45 slurmctld -D -v -f "$SLURM_CONF" > "$base/log/controller-restart.out" 2>&1 &
                    controller_pid=$!
                    ready=0
                    for _ in $(seq 1 80); do
                        if scontrol ping > "$base/log/ping-restart.out" 2>&1 &&
                           grep -Eq '^Slurmctld\(primary\) at .* is UP' "$base/log/ping-restart.out"; then ready=1; break; fi
                        sleep .1
                    done
                    if test "$ready" != 1; then cat "$base/log/controller-restart.out"; exit 1; fi
                    /validation/quota-socket-query root-active
                    scontrol show job "$job" | grep -q 'JobState=RUNNING'
                    echo 'PASS: running job and accounting survive clean controller restart'
                elif test "$TIME_EXHAUSTED_TEST" = 4; then
                    sleep 7
                    /validation/quota-socket-query root-active-exhausted
                    scontrol show job "$job" | grep -q 'JobState=RUNNING'
                    next=$(sbatch --parsable --time=00:01:00 --mem=64M --gres=gpu:1 --chdir="$base" --output="$base/slurm-%j.out" --wrap='sleep 1')
                    sleep 3
                    scontrol show job "$next" > "$base/log/next.out"
                    cat "$base/log/next.out"
                    grep -q 'JobState=PENDING' "$base/log/next.out"
                    grep -Eq 'NodeList=\(null\)' "$base/log/next.out"
                    grep -q 'MonthlyGpuQuota:monthly_capacity_exhausted' "$base/log/next.out"
                    echo 'PASS: running job continues into overage and new job stays unallocated'
                    finished=0
                    for _ in $(seq 1 25); do
                        scontrol show job "$job" > "$base/log/finished.out"
                        if grep -q 'JobState=COMPLETED' "$base/log/finished.out" &&
                           grep -q 'ExitCode=0:0' "$base/log/finished.out"; then finished=1; break; fi
                        sleep 1
                    done
                    if test "$finished" != 1; then cat "$base/log/finished.out"; exit 1; fi
                    /validation/quota-socket-query root-finished-overage
                    scontrol show job "$next" > "$base/log/next-after.out"
                    grep -q 'JobState=PENDING' "$base/log/next-after.out"
                    grep -Eq 'NodeList=\(null\)' "$base/log/next-after.out"
                    echo 'PASS: overage job finishes and waiting job remains unallocated'
                else
                    sleep 2
                    first=$(/validation/quota-socket-query root-active)
                    sleep 2
                    second=$(/validation/quota-socket-query root-active)
                    printf '%s\n%s\n' "$first" "$second"
                    printf '%s\n%s\n' "$first" "$second" | awk -v g="$request_gpus" '
                        NR == 1 { u=$4; un=$5; t=$16; tn=$17; next }
                        NR == 2 { du=($4-u)*1000000000+($5-un); dt=($16-t)*1000000000+($17-tn);
                                  if (dt <= 0 || du != g*dt) exit 1; print "PASS: GPU-time multiplier " g }'
                fi
                echo 'PASS: eligible synthetic user starts a GPU job'
            fi
            kill "$node_pid" 2>/dev/null || true
        else
            /validation/quota-socket-query
        fi
        echo 'DONE: isolated controller fixture completed'
        kill "$controller_pid" 2>/dev/null || true
        wait "$controller_pid" 2>/dev/null || true
        exit 0
    fi
    sleep .1
done
cat "$base/log/controller.out"
cat "$base/log/ping.out"
exit 1
