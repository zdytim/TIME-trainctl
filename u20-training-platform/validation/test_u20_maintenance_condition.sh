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
# Skipped transient units may be garbage-collected immediately on this host.
# Check that systemd accepted the unit and that its command did not run; the
# positive control below verifies the same command can run without the marker.
systemd-run --wait --unit="$blocked" --property=RemainAfterExit=yes \
    --property="ConditionPathExists=!$d/maintenance" /usr/bin/touch "$d/blocked-ran" >"$d/blocked.log" 2>&1
grep -q 'Running as unit:' "$d/blocked.log"
grep -q 'Finished with result: success' "$d/blocked.log"
test ! -e "$d/blocked-ran"
rm "$d/maintenance"
systemd-run --wait --unit="$allowed" --property=RemainAfterExit=yes \
    --property="ConditionPathExists=!$d/maintenance" /usr/bin/touch "$d/allowed-ran" >"$d/allowed.log" 2>&1
test "$(systemctl show "$allowed" -p ConditionResult --value)" = yes
test -f "$d/allowed-ran"
echo 'PASS: maintenance marker blocks a transient unit; absent marker permits it'
