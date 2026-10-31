#!/bin/bash
# Observe NVIDIA userspace only on newly created container-private device nodes.
set -eu
test "${TIME_ISOLATED_VALIDATION:-}" = 1
test -f /.dockerenv
test "$(id -u)" = 0
case "${TIME_UVM_MAJOR:-}" in ''|*[!0-9]*) echo 'numeric UVM major required' >&2; exit 2;; esac
for p in /dev/nvidia0 /dev/nvidiactl /dev/nvidia-uvm; do
    test ! -e "$p" && test ! -L "$p"
done
mknod /dev/nvidia0 c 195 0
mknod /dev/nvidiactl c 195 255
mknod /dev/nvidia-uvm c "$TIME_UVM_MAJOR" 0
chmod 600 /dev/nvidia0 /dev/nvidiactl /dev/nvidia-uvm
echo 'PRIVATE_DEVICE_BEFORE'
stat -c '%d:%i %t:%T %a %n' /dev/nvidia0 /dev/nvidiactl /dev/nvidia-uvm
export LD_LIBRARY_PATH=/trace-libs
if ! /trace-libs/strace -f -o /tmp/nvidia-permissions.trace \
    -e trace=chmod,fchmod,fchmodat,mknod,mknodat,openat \
    /trace-libs/nvidia-smi --query-gpu=name --format=csv,noheader > /tmp/nvidia-query.out 2>/tmp/nvidia-trace.err; then
    cat /tmp/nvidia-trace.err >&2
    cat /tmp/nvidia-query.out >&2
    exit 1
fi
cat /tmp/nvidia-query.out
echo 'PRIVATE_DEVICE_AFTER'
stat -c '%d:%i %t:%T %a %n' /dev/nvidia0 /dev/nvidiactl /dev/nvidia-uvm
echo 'PERMISSION_SYSCALLS'
grep -E 'chmod|mknod' /tmp/nvidia-permissions.trace || true
