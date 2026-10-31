#!/bin/bash
# Replace only the existing cron account helper; leave PAM configuration intact.
set -euo pipefail
test "$(id -u)" = 0
test "$(hostname)" = u20-Z790-S-DDR4
test "$#" = 3
guard=$1; runner=$2; probe=$3
target=/usr/local/libexec/pro6000d-cron-guard
old_sha=e78e145a2b05ff9b7e64ff12cd2555785410e3582975c188042bf7e7f8dc5959
new_sha=23f86b056f5eb78e38977755c04abc8be8c3e0c9fef32b646371cf51bd10c262
runner_sha=93a2709a663d93fb38dbe83ed2c246b6f00ead0f6254e82ef3c46fb90a2d497e
probe_sha=309d75e1db997e42ed8b00d39d5f82dbfdb066f8f5156f5610abb511f94b1416
pam_sha=9d2ecaddb980ce080d549290d56cbc38c1b6f26fb02c65aef852a870105afcfb
check() {
    local file=$1 hash=$2 mode=$3
    test -f "$file" && test ! -L "$file"
    test "$(stat -c '%u:%g:%a:%h' "$file")" = "0:0:$mode:1"
    printf '%s  %s\n' "$hash" "$file" | sha256sum -c --status
}
check "$target" "$old_sha" 755
check "$guard" "$new_sha" 700
check "$runner" "$runner_sha" 700
check "$probe" "$probe_sha" 700
check /etc/pam.d/cron "$pam_sha" 644
for service in cron slurmctld slurmd pro6000d-training; do systemctl is-active --quiet "$service"; done
test -z "$(squeue -h -o %i)"
"$runner" "$guard" "$probe"
umask 077
backup=$(mktemp -d /var/backups/time-cron-audit.XXXXXXXX)
chmod 700 "$backup"
cp -p "$target" "$backup/guard.before"
check "$backup/guard.before" "$old_sha" 755
tmp=$(mktemp /usr/local/libexec/.time-cron-guard.XXXXXXXX)
changed=0
cleanup() {
    rc=$?
    set +e
    if test "$changed" = 1; then
        if check "$target" "$new_sha" 755 && install -o root -g root -m 755 "$backup/guard.before" "$tmp" && mv -f "$tmp" "$target" && check "$target" "$old_sha" 755; then
            echo "Cron helper restored from $backup" >&2
        else echo "Cron helper rollback FAILED; inspect $backup" >&2; rc=1; fi
    fi
    rm -f -- "$tmp"
    exit "$rc"
}
trap cleanup EXIT
install -o root -g root -m 755 "$guard" "$tmp"
check "$target" "$old_sha" 755
mv -f "$tmp" "$target"
changed=1
check "$target" "$new_sha" 755
/usr/bin/python3 -I "$probe" cron u20 allow
/usr/bin/python3 -I "$probe" cron root allow
/usr/bin/python3 -I "$probe" cron timzhang deny
check /etc/pam.d/cron "$pam_sha" 644
for service in cron slurmctld slurmd pro6000d-training; do systemctl is-active --quiet "$service"; done
changed=0
echo "Audited cron helper installed; backup: $backup"
