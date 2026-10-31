#!/bin/bash
# Install a pinned cron-only PAM account guard; restore prior PAM file on failure.
set -euo pipefail
test "$(id -u)" = 0
test "$(hostname)" = u20-Z790-S-DDR4
test "$#" = 3
guard=$1; runner=$2; probe=$3
pam=/etc/pam.d/cron; target=/usr/local/libexec/pro6000d-cron-guard
old_sha=48657378ba743ca0bb465d856c122f9ac255fe99ad1b9a37c96a552b9524c816
guard_sha=e78e145a2b05ff9b7e64ff12cd2555785410e3582975c188042bf7e7f8dc5959
runner_sha=d8a2cd84e5674d6f37266319f6a83f1f4eb267d39b062a40c81e0dc33fee9849
probe_sha=309d75e1db997e42ed8b00d39d5f82dbfdb066f8f5156f5610abb511f94b1416
check() {
    local file=$1 hash=$2 mode=$3
    test -f "$file" && test ! -L "$file"
    test "$(stat -c '%u:%g:%a:%h' "$file")" = "0:0:$mode:1"
    printf '%s  %s\n' "$hash" "$file" | sha256sum -c --status
}
check "$pam" "$old_sha" 644
check "$guard" "$guard_sha" 700
check "$runner" "$runner_sha" 700
check "$probe" "$probe_sha" 700
test ! -e "$target" && test ! -L "$target"
test "$(stat -c '%u:%g:%a' /etc/pam.d /usr/local/libexec | sort -u)" = 0:0:755
for service in cron slurmctld slurmd pro6000d-training; do systemctl is-active --quiet "$service"; done
test -z "$(squeue -h -o %i)" || { echo 'Slurm jobs exist; refusing' >&2; exit 1; }
/usr/bin/python3 -I - <<'PY'
import os, pwd
excluded = {'nologin', 'false', 'sync', 'shutdown', 'halt'}
human = {p.pw_name for p in pwd.getpwall() if p.pw_uid >= 1000 and os.path.basename(p.pw_shell) not in excluded}
if human != {'u20', 'timzhang'}:
    raise SystemExit('Human account inventory changed; refusing cron PAM update')
PY
"$runner" "$guard" "$probe"
umask 077
backup=$(mktemp -d /var/backups/time-cron-guard.XXXXXXXX)
chmod 700 "$backup"
cp -p "$pam" "$backup/cron.before"
check "$backup/cron.before" "$old_sha" 644
tmp=$(mktemp /etc/pam.d/.time-cron.XXXXXXXX)
target_created=0; pam_changed=0
cleanup() {
    rc=$?
    set +e
    safe_remove=1
    if test "$pam_changed" = 1; then
        if check "$pam" "$new_sha" 644 && install -o root -g root -m 644 "$backup/cron.before" "$tmp" && mv -f "$tmp" "$pam" && check "$pam" "$old_sha" 644; then
            echo "Cron PAM restored from $backup" >&2
        else echo "Cron PAM rollback FAILED; inspect $backup" >&2; rc=1; safe_remove=0; fi
    fi
    if test "$target_created" = 1 && test "$safe_remove" = 1; then
        if check "$target" "$guard_sha" 755; then rm -f -- "$target"; else echo 'Guard changed; not removing it' >&2; rc=1; fi
    fi
    rm -f -- "$tmp"
    exit "$rc"
}
trap cleanup EXIT
install -o root -g root -m 755 "$guard" "$target"
target_created=1
check "$target" "$guard_sha" 755
/usr/bin/python3 -I - "$pam" "$tmp" <<'PY'
import os
from pathlib import Path
import sys
src, dst = map(Path, sys.argv[1:])
data = src.read_bytes()
line = b'account requisite pam_succeed_if.so quiet user notingroup pro6000d-training\n'
added = b'account requisite pam_exec.so quiet type=account /usr/local/libexec/pro6000d-cron-guard\n'
if data.count(line) != 1 or added in data:
    raise SystemExit('Unexpected cron account policy; refusing to modify')
with dst.open('wb') as out:
    out.write(data.replace(line, line + added, 1))
    out.flush(); os.fsync(out.fileno())
PY
chmod 644 "$tmp"
new_sha=$(sha256sum "$tmp" | cut -d' ' -f1)
check "$pam" "$old_sha" 644
mv -f "$tmp" "$pam"
pam_changed=1
/usr/bin/python3 -I -c 'import os; fd=os.open("/etc/pam.d",os.O_RDONLY|os.O_DIRECTORY); os.fsync(fd); os.close(fd)'
grep -Fxq 'account requisite pam_exec.so quiet type=account /usr/local/libexec/pro6000d-cron-guard' "$pam"
/usr/bin/python3 -I "$probe" cron u20 allow
/usr/bin/python3 -I "$probe" cron root allow
/usr/bin/python3 -I "$probe" cron timzhang deny
/usr/bin/python3 -I "$probe" cron time-nonexistent-cron-account deny
for service in cron slurmctld slurmd pro6000d-training; do systemctl is-active --quiet "$service"; done
target_created=0; pam_changed=0
echo "Cron account guard installed and PAM checks passed; backup: $backup"
