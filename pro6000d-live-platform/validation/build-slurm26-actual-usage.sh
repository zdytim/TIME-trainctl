#!/bin/sh
# Build only into a private /tmp staging directory; never install or start it.
set -eu
umask 022
[ "$#" -eq 3 ] || { echo 'usage: build-slurm26-target-staged.sh PRIVATE_DIR SOURCE_SHA256 SUBMIT_SOURCE_SHA256' >&2; exit 2; }
base=$1
expected=$2
submit_expected=$3
case "$base" in /tmp/time26-candidate-????????) ;; *) echo 'unexpected build directory' >&2; exit 2;; esac
[ "$(id -u)" -ne 0 ]
[ "$(uname -m)" = x86_64 ]
[ "$(hostname)" = ubuntu ]
[ -d "$base" ] && [ -f "$base/source.tar.gz" ] && [ -f "$base/job_submit_pro6000d.c" ]
[ ! -e "$base/source" ] && [ ! -e "$base/stage" ]
printf '%s\n' 1 > "$base/build.exit"
actual=$(sha256sum "$base/source.tar.gz" | cut -d ' ' -f 1)
[ "$actual" = "$expected" ] || { echo 'source checksum mismatch' >&2; exit 1; }
mkdir -m 700 "$base/source" "$base/mysql-tools"
tar -xzf "$base/source.tar.gz" --strip-components=1 -C "$base/source"
[ "$(sha256sum "$base/job_submit_pro6000d.c" | cut -d ' ' -f 1)" = "$submit_expected" ] || { echo 'submit plugin source checksum mismatch' >&2; exit 1; }
ln -s "$(command -v mariadb_config)" "$base/mysql-tools/mysql_config"
cd "$base/source"
prefix=/opt/slurm/26.05.5-actual-usage-20261118
export CFLAGS='-O2 -DTIME_ENABLE_MONTHLY_ALLOCATION_GUARD=1 -DTIME_ENABLE_MONTHLY_LIFECYCLE_DRAFT=1 -DTIME_ENABLE_SELECTABLE_V2=1 -DTIME_ENABLE_MONTHLY_COMMIT5_DRAFT=1 -DTIME_ENABLE_MONTHLY_TRANSACTION_DRAFT=1 -DTIME_ENABLE_MONTHLY_SLUID_DRAFT=1 -DTIME_ENABLE_MONTHLY_QUERY2_DRAFT=1 -DTIME_ENABLE_MONTHLY_TOMBSTONES_DRAFT=1 -DTIME_ENABLE_MONTHLY_PRIORITY_DRAFT=1 -DTIME_ENABLE_COMPLETION_SNAPSHOT_DRAFT=1 -DTIME_ENABLE_CREATION_BIND2_DRAFT=1 -DTIME_ENABLE_CREATION_BOUND_CAPABILITY_DRAFT=1 -DTIME_ENABLE_EXACT_TOP_GATE_DRAFT=1 -DTIME_ENABLE_ORDERED_APPLY2_DRAFT=1 -DTIME_ENABLE_ORDERED_BASELINE_DRAFT=1 -DTIME_ENABLE_ORDERED_SNAPSHOT1_DRAFT=1 -DTIME_ENABLE_ORDERED_STATUS2_DRAFT=1 -DTIME_ENABLE_ORDERED_TOP_WAL_DRAFT=1 -DTIME_ENABLE_SHARED_TOP_SNAPSHOT_DRAFT=1 -DTIME_ENABLE_SPLIT_TOP_FENCE_DRAFT=1 -DTIME_ENABLE_TOP_BARRIER_DRAFT=1 -DTIME_ENABLE_TOP_GENERATION_DRAFT=1 -DTIME_ENABLE_TOP_HOLD_TRACKING_DRAFT=1 -DTIME_ENABLE_TOP_JOBLIST_SCAN_DRAFT=1 -DTIME_ENABLE_TOP_MARKER_ADD3_LIVE_DRAFT=1 -DTIME_ENABLE_TOP_MARKER_ADD3_REPLAY_DRAFT=1 -DTIME_ENABLE_TOP_MARKER_BIND5_LIVE_DRAFT=1 -DTIME_ENABLE_TOP_MARKER_BIND5_REPLAY_DRAFT=1 -DTIME_ENABLE_TOP_MARKER_CANCEL4_DRAFT=1 -DTIME_ENABLE_TOP_MARKER_V2_DRAFT=1 -DTIME_ENABLE_TOP_RUNNING_RECEIPT_DRAFT=1 -DTIME_ENABLE_UNHELD_TOP_BIND_DRAFT=1'
nice -n 19 ionice -c 3 ./configure --prefix="$prefix" --sysconfdir=/etc/slurm --with-munge --with-mysql_config="$base/mysql-tools" --disable-dependency-tracking > "$base/configure.log" 2>&1
nice -n 19 ionice -c 3 make -j2 > "$base/make.log" 2>&1
nice -n 19 ionice -c 3 make install DESTDIR="$base/stage" > "$base/install.log" 2>&1
install="$base/stage$prefix"
nice -n 19 ionice -c 3 gcc -shared -fPIC -Wall -Wextra -Werror -I"$install/include" "$base/job_submit_pro6000d.c" -o "$install/lib/slurm/job_submit_pro6000d.so"
find "$install" -type d -exec chmod go-w {} +
find "$install" -type f -exec chmod go-w {} +
for item in sbin/slurmctld sbin/slurmd lib/slurm/job_submit_pro6000d.so lib/slurm/cgroup_v2.so lib/slurm/select_cons_tres.so lib/slurm/sched_backfill.so lib/slurm/accounting_storage_mysql.so; do
    [ -f "$install/$item" ] || { echo "missing staged artifact: $item" >&2; exit 1; }
done
sha256sum "$install/sbin/slurmctld" "$install/sbin/slurmd" "$install/lib/slurm/job_submit_pro6000d.so" "$install/lib/slurm/cgroup_v2.so" "$install/lib/slurm/select_cons_tres.so" "$install/lib/slurm/sched_backfill.so"
printf '%s\n' 0 > "$base/build.exit"
