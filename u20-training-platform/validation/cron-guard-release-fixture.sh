#!/bin/bash
# Networkless container only; all PAM files and accounts are synthetic.
set -euo pipefail
test -f /.dockerenv
test "${TIME_ISOLATED_VALIDATION:-}" = 1
test "$(hostname)" = u20-Z790-S-DDR4
test "${TIME_RELEASE_CASE:-}" = success -o "${TIME_RELEASE_CASE:-}" = rollback
cat > /etc/passwd <<'EOF'
root:x:0:0:root:/root:/bin/bash
u20:x:1000:1000:fixture:/tmp:/bin/bash
timzhang:x:1001:1001:fixture:/tmp:/bin/bash
EOF
cat > /etc/group <<'EOF'
root:x:0:
u20:x:1000:
timzhang:x:1001:
pro6000d-admin:x:996:u20
pro6000d-training:x:998:u20,timzhang
EOF
cat > /etc/shadow <<'EOF'
root:!:20000:0:99999:7:::
u20:!:20000:0:99999:7:::
timzhang:!:20000:0:99999:7:::
EOF
chmod 600 /etc/shadow
mkdir -p /fixture-bin /var/backups /usr/local/libexec
chmod 755 /usr/local/libexec /etc/pam.d
ln -s /opt/py310/bin/python3 /usr/bin/python3
cat > /fixture-bin/systemctl <<'EOF'
#!/bin/sh
set -eu
file=/tmp/time-systemctl-count
n=0; test ! -f "$file" || n=$(cat "$file")
n=$((n+1)); printf '%s\n' "$n" > "$file"
if test "${TIME_RELEASE_CASE:-}" = rollback && test "$n" -eq 5; then exit 1; fi
exit 0
EOF
printf '#!/bin/sh\nexit 0\n' > /fixture-bin/squeue
chmod 755 /fixture-bin/systemctl /fixture-bin/squeue
export PATH="/fixture-bin:$PATH"
chmod 700 /fixture/guard.py /fixture/runner.sh /fixture/probe.py
if test "$TIME_RELEASE_CASE" = success; then
    /bin/bash /fixture/release.sh /fixture/guard.py /fixture/runner.sh /fixture/probe.py
    test -f /usr/local/libexec/pro6000d-cron-guard
    grep -Fxq 'account requisite pam_exec.so quiet type=account /usr/local/libexec/pro6000d-cron-guard' /etc/pam.d/cron
    echo 'PASS: isolated cron guard install and actual PAM account checks'
else
    if /bin/bash /fixture/release.sh /fixture/guard.py /fixture/runner.sh /fixture/probe.py; then
        echo 'FAIL: injected post-install failure unexpectedly passed' >&2; exit 1
    fi
    test ! -e /usr/local/libexec/pro6000d-cron-guard
    printf '%s  %s\n' 48657378ba743ca0bb465d856c122f9ac255fe99ad1b9a37c96a552b9524c816 /etc/pam.d/cron | sha256sum -c --status
    echo 'PASS: isolated failed install restored original cron PAM and removed helper'
fi
