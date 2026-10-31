#!/bin/bash
# Bounded u20-only test: private duplicate nodes, never chmod existing devices.
set -eu
test "$(id -u)" = 0
test "$(hostname)" = u20-Z790-S-DDR4
test "${TIME_GPU_CGROUP_PROBE:-}" = 1
test "$(id -u timzhang)" = 1001
case ",$(findmnt -n -o OPTIONS -T /dev)," in *,nodev,*) echo '/dev is nodev; invalid probe' >&2; exit 1;; esac
d=$(mktemp -d /dev/time-gpu-all-probe.XXXXXXXX)
unit="time-gpu-all-probe-${d##*.}"
cleanup() {
    systemctl stop "$unit.service" >/dev/null 2>&1 || true
    rm -f "$d/gpu" "$d/ctl" "$d/uvm"
    rmdir "$d"
}
trap cleanup EXIT
chown root:1001 "$d"; chmod 710 "$d"
for pair in 'nvidia0:gpu' 'nvidiactl:ctl' 'nvidia-uvm:uvm'; do
    original=/dev/${pair%:*}; name=${pair#*:}
    test -c "$original" && test ! -L "$original"
    hex_major=$(stat -c '%t' "$original"); hex_minor=$(stat -c '%T' "$original")
    major=$((16#$hex_major)); minor=$((16#$hex_minor))
    mknod "$d/$name" c "$major" "$minor"
    chmod 666 "$d/$name"
done
# An effective root open of every duplicate is the positive control; otherwise
# a later failure might be due to the driver or mount rather than the cgroup.
/usr/bin/python3 -c 'import os,sys; [(lambda fd:os.close(fd))(os.open(p,os.O_RDONLY|os.O_CLOEXEC)) for p in sys.argv[1:]]' "$d/gpu" "$d/ctl" "$d/uvm"
systemd-run --quiet --wait --pipe --unit="$unit" --slice=user-1001.slice \
    -p User=timzhang /usr/bin/python3 -c '
import errno, os, sys
assert os.geteuid() == 1001
for path in sys.argv[1:]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM): raise
        print("PASS: %s denied to isolated training slice (errno %d)" % (os.path.basename(path), exc.errno))
    else:
        os.close(fd)
        raise SystemExit("FAIL: device opened by training slice: " + path)
' "$d/gpu" "$d/ctl" "$d/uvm"
