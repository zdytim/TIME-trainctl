#!/bin/bash
# Pinned ACL-only u20 release. No Slurm service or running job is restarted.
set -eu
test "$(id -u)" = 0
test "$#" = 1
candidate=$1
old=eae5ed2e355ae8bef43da88578f8e4ea54bea9ebfca00d2ffc9d40647289dccb
new=f21d51f036ae052b675327cc5fc81b26742d7dceb87f1a34a7574555ef504fe9
target=/usr/local/libexec/pro6000d-gpu-acl
test "$(hostname)" = u20-Z790-S-DDR4
test "$(sha256sum "$target" | cut -d' ' -f1)" = "$old"
test "$(sha256sum "$candidate" | cut -d' ' -f1)" = "$new"
test -z "$(squeue -h)"
while read -r pid; do
    owner=$(ps -o user= -p "$pid" | tr -d ' ')
    case "$owner" in root|u20|gdm|'') ;; *) echo "Unexpected GPU process owner: $owner" >&2; exit 1;; esac
done < <(nvidia-smi pmon -c 1 | awk 'NR>2 && $2 ~ /^[0-9]+$/ {print $2}')
shopt -s nullglob
targets=(/dev/nvidia[0-9]* /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools)
for path in "${targets[@]}"; do test -c "$path" && test ! -L "$path"; done
backup=$(mktemp -d /var/backups/time-gpu-acl-83043ee.XXXXXXXX)
chmod 700 "$backup"
cp -p "$target" "$backup/gpu-acl.before"
getfacl -p -- "${targets[@]}" > "$backup/devices.before.acl"
temp=$(mktemp /usr/local/libexec/.time-gpu-acl.XXXXXXXX)
applied=0
finish() {
    result=$?
    if test "$result" -ne 0 && test "$applied" = 1; then
        echo "Rolling back ACL and helper from $backup" >&2
        setfacl --restore="$backup/devices.before.acl" || echo 'ACL rollback failed' >&2
        install -o root -g root -m 755 "$backup/gpu-acl.before" "$target" || echo 'helper rollback failed' >&2
    fi
    rm -f -- "$temp"
    exit "$result"
}
trap finish EXIT
install -o root -g root -m 755 "$candidate" "$temp"
test "$(sha256sum "$temp" | cut -d' ' -f1)" = "$new"
applied=1
mv -f -- "$temp" "$target"
"$target"
check='import os; allowed=False
try:
 f=os.open("/dev/nvidia0",os.O_RDWR); os.close(f); allowed=True
except PermissionError:
 pass
expected=os.environ["TIME_EXPECT"] == "allow"
print("allowed="+str(allowed)+" expected="+str(expected))
assert allowed == expected'
TIME_EXPECT=deny runuser -u nobody -- python3 -c "$check"
TIME_EXPECT=deny runuser -u timzhang -- python3 -c "$check"
TIME_EXPECT=allow runuser -u u20 -- python3 -c "$check"
if id gdm >/dev/null 2>&1; then TIME_EXPECT=allow runuser -u gdm -- python3 -c "$check"; fi
runtime_gid=$(getent group pro6000d-gpu | cut -d: -f3)
training_gid=$(getent group pro6000d-training | cut -d: -f3)
trainer_uid=$(id -u timzhang)
trainer_gid=$(id -g timzhang)
TIME_EXPECT=allow setpriv --reuid="$trainer_uid" --regid="$runtime_gid" --groups="$training_gid,$trainer_gid" python3 -c "$check"
for service in slurmctld slurmd pro6000d-training; do systemctl is-active --quiet "$service"; done
echo "ACL_RELEASE_OK backup=$backup commit=83043ee06abf2cc357ce86ac7ff58ea628655d5b"
