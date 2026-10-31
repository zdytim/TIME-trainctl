#!/bin/bash
# Temporary PAM service only; no actual cron jobs, sessions or credentials.
set -euo pipefail
test "$(id -u)" = 0
test "$(hostname)" = u20-Z790-S-DDR4
test "$#" = 2
guard=$1; probe=$2
for f in "$guard" "$probe"; do
    test -f "$f" && test ! -L "$f"
    test "$(stat -c '%u:%g:%a' "$f")" = 0:0:700
done
test "$(sha256sum "$guard" | cut -d' ' -f1)" = 23f86b056f5eb78e38977755c04abc8be8c3e0c9fef32b646371cf51bd10c262
test "$(sha256sum "$probe" | cut -d' ' -f1)" = 309d75e1db997e42ed8b00d39d5f82dbfdb066f8f5156f5610abb511f94b1416
suffix=$(/usr/bin/python3 -I -c 'import uuid;print(uuid.uuid4().hex)')
name="time-cron-account-$suffix"
path="/etc/pam.d/$name"
created=0
cleanup() { if test "$created" = 1; then rm -f -- "$path"; fi; }
trap cleanup EXIT
umask 077
(set -o noclobber; printf 'account requisite pam_exec.so quiet type=account %s\naccount required pam_permit.so\n' "$guard" > "$path")
created=1
/usr/bin/python3 -I "$probe" "$name" u20 allow
/usr/bin/python3 -I "$probe" "$name" root allow
/usr/bin/python3 -I "$probe" "$name" timzhang deny
/usr/bin/python3 -I "$probe" "$name" time-nonexistent-cron-account deny
echo 'PASS: temporary PAM account stack admits admins and refuses ordinary users'
