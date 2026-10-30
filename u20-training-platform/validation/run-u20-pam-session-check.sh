#!/bin/bash
# Isolated temporary PAM service names; existing PAM files are never replaced.
set -euo pipefail
test "$(id -u)" = 0
test "$#" = 2
guard=$1
probe=$2
for file in "$guard" "$probe"; do
    test -f "$file" && test ! -L "$file"
    test "$(stat -c '%u:%a' "$file")" = 0:700
done
test "$(sha256sum "$guard" | cut -d' ' -f1)" = 274a70e98183d0055d063c19f4eb24a0569d883788fe2f98bcf2545417a0ecab
work=$(mktemp -d /run/time-pam-check-XXXXXXXX)
# libpam canonicalizes service names to lowercase when loading /etc/pam.d.
# Mixed-case mktemp suffixes would silently select the fallback service.
suffix=$(printf '%s' "${work##*-}" | tr '[:upper:]' '[:lower:]')
positive="time-pam-${suffix}-allow"
negative="time-pam-${suffix}-deny"
created=()
cleanup() {
    for file in "${created[@]}"; do rm -f -- "$file"; done
    rmdir "$work" 2>/dev/null || true
}
trap cleanup EXIT
umask 077
for name in "$positive" "$negative"; do
    # Never overwrite a preexisting PAM service.
    (set -o noclobber; printf '%s\n' 'auth requisite pam_deny.so' 'account requisite pam_deny.so' 'password requisite pam_deny.so' 'session required pam_systemd.so' > "/etc/pam.d/$name")
    created+=("/etc/pam.d/$name")
done
printf 'session required pam_exec.so type=open_session quiet %s\n' "$guard" >> "/etc/pam.d/$positive"
printf '%s\n' 'session required pam_exec.so type=open_session quiet /bin/false' >> "/etc/pam.d/$negative"
# Give the close-session stack a module that returns PAM_SUCCESS even when the
# open-session-only guard is skipped; it cannot override a required failure.
printf '%s\n' 'session required pam_permit.so' >> "/etc/pam.d/$positive"
printf '%s\n' 'session required pam_permit.so' >> "/etc/pam.d/$negative"
before=$(loginctl list-sessions --no-legend | awk '$2 == 1001 { print $1 }' | sort)
# Dedicated transient units have bounded lifetimes and are collected afterwards.
systemd-run --quiet --wait --collect --pipe --unit="$positive" --property=RuntimeMaxSec=15 \
    /usr/bin/python3 "$probe" "$positive" timzhang allow
systemd-run --quiet --wait --collect --pipe --unit="$negative" --property=RuntimeMaxSec=15 \
    /usr/bin/python3 "$probe" "$negative" timzhang deny
for _ in $(seq 1 25); do
    after=$(loginctl list-sessions --no-legend | awk '$2 == 1001 { print $1 }' | sort)
    test "$before" = "$after" && break
    sleep .2
done
if test "$before" != "$after"; then
    echo 'Session set changed; inspect without terminating any user session' >&2
    exit 1
fi
echo 'PASS: temporary PAM session stack allowed the staged guard and rejected a failing guard'
