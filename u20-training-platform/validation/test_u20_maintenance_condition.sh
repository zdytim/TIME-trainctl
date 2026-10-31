#!/bin/bash
# Test only isolated transient units and files under a new /run directory.
set -eu
test "$(id -u)" = 0
test "$(hostname)" = u20-Z790-S-DDR4
d=$(mktemp -d /run/time-maintenance-test.XXXXXX)
suffix=${d##*.}
blocked="time-fence-blocked-$suffix.service"
allowed="time-fence-allowed-$suffix.service"
cleanup() {
    systemctl stop "$blocked" "$allowed" >/dev/null 2>&1 || true
    systemctl reset-failed "$blocked" "$allowed" >/dev/null 2>&1 || true
    rm -rf -- "$d"
}
trap cleanup EXIT
touch "$d/maintenance"
# A skipped service may make systemd-run return failure; inspect the actual
# condition result and the effect instead of treating its exit as proof.
systemd-run --wait --unit="$blocked" --property=RemainAfterExit=yes \
    --property="ConditionPathExists=!$d/maintenance" /usr/bin/touch "$d/blocked-ran" >"$d/blocked.log" 2>&1 || true
test "$(systemctl show "$blocked" -p LoadState --value)" = loaded
test "$(systemctl show "$blocked" -p ConditionResult --value)" = no
test ! -e "$d/blocked-ran"
rm "$d/maintenance"
systemd-run --wait --unit="$allowed" --property=RemainAfterExit=yes \
    --property="ConditionPathExists=!$d/maintenance" /usr/bin/touch "$d/allowed-ran" >"$d/allowed.log" 2>&1
test "$(systemctl show "$allowed" -p ConditionResult --value)" = yes
test -f "$d/allowed-ran"
echo 'PASS: maintenance marker blocks a transient unit; absent marker permits it'
