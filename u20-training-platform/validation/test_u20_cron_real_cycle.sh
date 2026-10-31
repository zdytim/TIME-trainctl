#!/bin/bash
# Bounded real cron cycle using only a locked temporary account and marker files.
set -euo pipefail
test "$(id -u)" = 0
test "$(hostname)" = u20-Z790-S-DDR4
test "$#" = 1
probe=$1
test -f "$probe" && test ! -L "$probe"
test "$(stat -c '%u:%g:%a:%h' "$probe")" = 0:0:700:1
printf '%s  %s\n' 309d75e1db997e42ed8b00d39d5f82dbfdb066f8f5156f5610abb511f94b1416 "$probe" | sha256sum -c --status
printf '%s  %s\n' 9d2ecaddb980ce080d549290d56cbc38c1b6f26fb02c65aef852a870105afcfb /etc/pam.d/cron | sha256sum -c --status
printf '%s  %s\n' 48657378ba743ca0bb465d856c122f9ac255fe99ad1b9a37c96a552b9524c816 /var/backups/time-cron-guard.GYV2SZsa/cron.before | sha256sum -c --status
for service in cron slurmctld slurmd pro6000d-training; do systemctl is-active --quiet "$service"; done
test -z "$(squeue -h -o %i)"
suffix=$(/usr/bin/python3 -I -c 'import uuid;print(uuid.uuid4().hex[:12])')
user="timecron$suffix"
cronfile="/etc/cron.d/time-cron-check-$suffix"
oldpam="/etc/pam.d/time-cron-old-$suffix"
testdir=$(mktemp -d /tmp/time-cron-check.XXXXXXXX)
created_user=0; created_cron=0; created_pam=0; created_uid=''
cleanup() {
    rc=$?
    set +e
    if test "$created_cron" = 1; then rm -f -- "$cronfile"; fi
    if test "$created_pam" = 1; then rm -f -- "$oldpam"; fi
    if test "$created_user" = 1; then
        current=$(id -u "$user" 2>/dev/null)
        if test "$current" = "$created_uid" && /usr/sbin/userdel "$user"; then
            echo 'Temporary locked account removed'
        else
            echo "CLEANUP FAILED: inspect temporary account $user" >&2; rc=1
        fi
    fi
    rm -rf -- "$testdir"
    exit "$rc"
}
trap cleanup EXIT
test ! -e "$cronfile" && test ! -L "$cronfile"
test ! -e "$oldpam" && test ! -L "$oldpam"
if getent passwd "$user" >/dev/null; then echo 'temporary username already exists' >&2; exit 1; fi
/usr/sbin/useradd --no-create-home --no-user-group --gid nogroup --shell /bin/bash --password '!' "$user"
created_user=1; created_uid=$(id -u "$user")
test "$created_uid" -ge 1000 && test "$created_uid" -lt 60000
chmod 1777 "$testdir"
# Establish that the prior account stack admitted the same un-enrolled account,
# while the installed stack rejects it. Both PAM files are exact private copies.
(set -o noclobber; cat /var/backups/time-cron-guard.GYV2SZsa/cron.before > "$oldpam")
created_pam=1; chmod 644 "$oldpam"
/usr/bin/python3 -I "$probe" "${oldpam##*/}" "$user" allow
/usr/bin/python3 -I "$probe" cron "$user" deny
started=$(date '+%Y-%m-%d %H:%M:%S')
(set -o noclobber; printf '* * * * * u20 /usr/bin/touch %s/admin.ok\n* * * * * %s /usr/bin/touch %s/unmanaged.bad\n' "$testdir" "$user" "$testdir" > "$cronfile")
created_cron=1; chmod 644 "$cronfile"
for _ in $(seq 1 110); do
    test ! -e "$testdir/unmanaged.bad" || { echo 'FAIL: unmanaged cron command executed' >&2; exit 1; }
    if test -f "$testdir/admin.ok" && journalctl -u cron.service --since "$started" --no-pager -o cat 2>/dev/null | grep -Fq "$user"; then
        rm -f -- "$cronfile"; created_cron=0
        sleep 2
        test ! -e "$testdir/unmanaged.bad" || { echo 'FAIL: unmanaged cron command executed' >&2; exit 1; }
        echo 'PASS: old PAM admitted the un-enrolled account; installed PAM refused it'
        echo 'PASS: real cron ran administrator marker and logged the denied un-enrolled account'
        exit 0
    fi
    sleep 1
done
echo 'INCONCLUSIVE: expected cron markers/logs not observed within the bound' >&2
exit 1
