#!/bin/bash
# Bounded u20-only probe: private node, no modification to /dev or training jobs.
set -eu
test "$(id -u)" = 0
test "${TIME_GPU_CGROUP_PROBE:-}" = 1
test "$(id -u timzhang)" = 1001
test "$(stat -c '%t:%T' /dev/nvidia0)" = c3:0
tmpdir=$(mktemp -d /run/time-gpu-cgroup-probe.XXXXXXXX)
unit="time-gpu-cgroup-probe-${tmpdir##*.}"
cleanup() {
    systemctl stop "$unit.service" >/dev/null 2>&1 || true
    rm -f "$tmpdir/device"
    rmdir "$tmpdir"
}
trap cleanup EXIT
chmod 755 "$tmpdir"
mknod "$tmpdir/device" c 195 0
chmod 666 "$tmpdir/device"
systemd-run --quiet --wait --pipe --unit="$unit" --slice=user-1001.slice \
    -p User=timzhang /usr/bin/python3 -c '
import errno, os, sys
assert os.geteuid() == 1001
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CLOEXEC)
except OSError as exc:
    if exc.errno not in (errno.EACCES, errno.EPERM):
        raise
    print("PASS: world-readable GPU node denied in training user slice (errno %d)" % exc.errno)
else:
    os.close(fd)
    raise SystemExit("FAIL: GPU device was opened from training user slice")
' "$tmpdir/device"
