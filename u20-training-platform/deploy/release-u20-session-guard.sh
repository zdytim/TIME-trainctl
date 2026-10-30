#!/bin/bash
# Replace only the PAM guard after checking the existing version; rollback on failure.
set -euo pipefail
test "$(id -u)" = 0
test "$#" = 3
candidate=$1
runner=$2
probe=$3
target=/usr/local/libexec/pro6000d-session-guard
old_sha=d383a6b0dfcfceabd1f3d1ca0a276e2b852bbb54edcb5ed0da81f6d1c7fa87b7
new_sha=274a70e98183d0055d063c19f4eb24a0569d883788fe2f98bcf2545417a0ecab
runner_sha=c4ce035cbfff37fbbcc263aaca20b607a1228e3df9cebceebbce3c4e4652b640
probe_sha=1a9572ff65b095024cb6d03bdf99ca7d942ec4f3c18927d1c3759ca21a55d613
check_file() {
    local path=$1 expected=$2 mode=$3
    test -f "$path" && test ! -L "$path"
    test "$(stat -c '%u:%g:%a' "$path")" = "0:0:$mode"
    printf '%s  %s\n' "$expected" "$path" | sha256sum -c --status
}
check_file "$target" "$old_sha" 755
check_file "$candidate" "$new_sha" 700
check_file "$runner" "$runner_sha" 700
check_file "$probe" "$probe_sha" 700
test "$(stat -c '%u:%g:%a' /usr/local/libexec)" = 0:0:755
grep -Fxq 'session required pam_exec.so quiet /usr/local/libexec/pro6000d-session-guard' /etc/pam.d/common-session
systemctl is-active --quiet slurmctld
systemctl is-active --quiet slurmd
systemctl is-active --quiet pro6000d-training
test -z "$(squeue -h)" || { echo 'Refusing change while Slurm jobs are present' >&2; exit 1; }
umask 077
backup=$(mktemp -d /var/backups/time-session-guard-90fa24a.XXXXXXXX)
chmod 700 "$backup"
cp -p "$target" "$backup/session-guard.before"
check_file "$backup/session-guard.before" "$old_sha" 755
temp=$(mktemp /usr/local/libexec/.time-session-guard.XXXXXXXX)
changed=0
cleanup() {
    rc=$?
    set +e
    if test "$changed" = 1; then
        install -o root -g root -m 755 "$backup/session-guard.before" "$temp" && mv -f "$temp" "$target"
        if check_file "$target" "$old_sha" 755; then
            echo "Guard change failed; prior guard restored from $backup" >&2
        else
            echo "Guard rollback FAILED; inspect $backup immediately" >&2
            rc=1
        fi
    fi
    rm -f -- "$temp"
    exit "$rc"
}
trap cleanup EXIT
install -o root -g root -m 755 "$candidate" "$temp"
mv -f "$temp" "$target"
changed=1
check_file "$target" "$new_sha" 755
# The temporary service includes the host's actual common-session, so the
# newly installed guard is exercised before the separate candidate check.
TIME_PAM_COMMON_SESSION=1 "$runner" "$candidate" "$probe"
PAM_USER=u20 PAM_TYPE=open_session /usr/bin/python3 "$target"
systemctl is-active --quiet slurmctld
systemctl is-active --quiet slurmd
systemctl is-active --quiet pro6000d-training
changed=0
echo "Guard installed and common-session verified; backup: $backup"
