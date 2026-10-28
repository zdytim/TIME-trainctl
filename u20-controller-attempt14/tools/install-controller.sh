#!/bin/sh
set -eu
umask 077

PREFIX=/opt/slurm/monthly
ARTIFACT=
STATE_DIR=/var/lib/slurm/monthly-quota
STATE_OWNER=slurm:slurm
ACTIVATE=0
ENABLE=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --artifact) ARTIFACT=${2-}; shift 2 ;;
        --prefix) PREFIX=${2-}; shift 2 ;;
        --state-dir) STATE_DIR=${2-}; shift 2 ;;
        --state-owner) STATE_OWNER=${2-}; shift 2 ;;
        --activate) ACTIVATE=1; shift ;;
        --enable) ENABLE=1; shift ;;
        *) echo "usage: $0 --artifact DIR [--prefix DIR] [--state-dir DIR] [--state-owner user:group] [--activate] [--enable]" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "root required" >&2; exit 1; }
[ -f "$ARTIFACT/sbin/slurmctld" ] || { echo "missing versioned slurmctld artifact" >&2; exit 1; }
test -r "$ARTIFACT/BUILD-MANIFEST"
VERSION=$(sed -n 's/^version=//p' "$ARTIFACT/BUILD-MANIFEST")
[ -n "$VERSION" ] || { echo "artifact manifest has no version" >&2; exit 1; }
DEST="$PREFIX/$VERSION"
test ! -e "$DEST" || { echo "version already installed: $DEST" >&2; exit 1; }
install -d -m 0755 "$DEST/sbin" "$DEST/source-patch"
install -m 0755 "$ARTIFACT/sbin/slurmctld" "$DEST/sbin/slurmctld"
cp -a "$ARTIFACT/source-patch/." "$DEST/source-patch/"
install -m 0644 "$ARTIFACT/BUILD-MANIFEST" "$DEST/BUILD-MANIFEST"
install -m 0644 "$ARTIFACT/SHA256SUMS" "$DEST/SHA256SUMS"
(cd "$DEST" && sha256sum -c SHA256SUMS)

[ ! -L "$STATE_DIR" ] || { echo "state-dir must not be a symlink" >&2; exit 1; }
install -d -m 0700 "$STATE_DIR"
chmod 0700 "$STATE_DIR"
chown "$STATE_OWNER" "$STATE_DIR"
if [ "$ENABLE" -eq 1 ]; then
    [ ! -L "$STATE_DIR/enabled" ] || { echo "enabled marker must not be a symlink" >&2; exit 1; }
    install -m 0600 /dev/null "$STATE_DIR/enabled"
    chown "$STATE_OWNER" "$STATE_DIR/enabled"
fi
if [ "$ACTIVATE" -eq 1 ]; then
    CURRENT="$PREFIX/current"
    OLD=
    if [ -L "$CURRENT" ]; then OLD=$(readlink "$CURRENT"); fi
    printf '%s\n' "previous=${OLD}" "new=$DEST" "state_dir=$STATE_DIR" > "$PREFIX/last-activation"
    ln -sfn "$DEST" "$CURRENT"
fi
echo "Staged $DEST. No service was restarted; a separately reviewed maintenance window must activate the controller."
