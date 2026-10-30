#!/bin/bash
# Tests only an ephemeral regular file, never a device node.
set -eu
test "${TIME_ACL_FIXTURE:-}" = 1
test "$(id -u)" = 0
dir=$(mktemp -d /tmp/time-acl-fixture.XXXXXXXX)
trap 'rm -rf -- "$dir"' EXIT
chmod 755 "$dir"
file="$dir/fake-device"
touch "$file"
chmod 666 "$file"
training_gid=$(getent group pro6000d-training | cut -d: -f3)
runtime_gid=$(getent group pro6000d-gpu | cut -d: -f3)
trainer_uid=$(id -u timzhang)
trainer_gid=$(id -g timzhang)
admin_uid=$(id -u u20)
setfacl -m "u:$admin_uid:rw,g:$training_gid:---,g:$runtime_gid:rw,o::---" "$file"
export TIME_ACL_FILE="$file"
check='import os; p=os.environ["TIME_ACL_FILE"]; expected=os.environ["TIME_ACL_EXPECT"]; allowed=False
try:
 fd=os.open(p,os.O_RDWR); os.close(fd); allowed=True
except PermissionError:
 pass
print("allowed="+str(allowed)+" expected="+expected)
assert allowed == (expected == "allow")'
TIME_ACL_EXPECT=deny runuser -u nobody -- python3 -c "$check"
TIME_ACL_EXPECT=deny runuser -u timzhang -- python3 -c "$check"
TIME_ACL_EXPECT=allow runuser -u u20 -- python3 -c "$check"
TIME_ACL_EXPECT=allow setpriv --reuid="$trainer_uid" --regid="$runtime_gid" --groups="$training_gid,$trainer_gid" python3 -c "$check"
echo 'PASS: fixture denies ordinary direct access and permits admin and scheduled runtime group'
