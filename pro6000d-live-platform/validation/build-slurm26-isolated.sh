#!/bin/sh
# Compile only in the dedicated GPU-free Colima VM. Artifacts are NOT suitable
# for deployment: this VM is arm64 and the target production host is amd64.
set -eu
[ "$(hostname)" = colima ]
[ "$(systemd-detect-virt)" = apple ]
[ ! -e /dev/nvidiactl ]
[ "$#" -eq 2 ] || { echo 'usage: build-slurm26-isolated.sh SOURCE NEW_OUTPUT' >&2; exit 2; }
source_dir=$1
base=$2
case "$base" in /tmp/time-slurm26-*) ;; *) echo 'output must be under /tmp/time-slurm26-*' >&2; exit 2;; esac
[ -f "$source_dir/configure" ]
[ ! -e "$base" ]
mkdir -m 700 "$base"
cp -R "$source_dir" "$base/source"
mkdir "$base/mysql-tools"
ln -s /usr/bin/mariadb_config "$base/mysql-tools/mysql_config"
cd "$base/source"
export CFLAGS='-O1 -g -DTIME_ENABLE_MONTHLY_ALLOCATION_GUARD=1 -DTIME_ENABLE_MONTHLY_LIFECYCLE_DRAFT=1 -DTIME_ENABLE_SELECTABLE_V2=1 -DTIME_ENABLE_MONTHLY_COMMIT5_DRAFT=1 -DTIME_ENABLE_MONTHLY_TRANSACTION_DRAFT=1 -DTIME_ENABLE_MONTHLY_SLUID_DRAFT=1 -DTIME_ENABLE_MONTHLY_QUERY2_DRAFT=1 -DTIME_ENABLE_MONTHLY_TOMBSTONES_DRAFT=1 -DTIME_ENABLE_MONTHLY_PRIORITY_DRAFT=1 -DTIME_ENABLE_MONTHLY_TEST_FIXTURE=1'
./configure --prefix="$base/install" --sysconfdir="$base/etc" --with-munge --with-mysql_config="$base/mysql-tools" --disable-dependency-tracking > "$base/configure.log" 2>&1 || { tail -60 "$base/configure.log"; exit 1; }
make -j2 > "$base/make.log" 2>&1 || { tail -80 "$base/make.log"; exit 1; }
make install > "$base/install.log" 2>&1 || { tail -60 "$base/install.log"; exit 1; }
test -f "$base/install/lib/slurm/accounting_storage_mysql.so"
sha256sum "$base/install/sbin/slurmctld"
