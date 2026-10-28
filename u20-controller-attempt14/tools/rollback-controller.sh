#!/bin/sh
set -eu

PREFIX=/opt/slurm/monthly
TARGET=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX=${2-}; shift 2 ;;
        --to) TARGET=${2-}; shift 2 ;;
        *) echo "usage: $0 [--prefix DIR] --to VERSION-DIR" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "root required" >&2; exit 1; }
[ -x "$PREFIX/$TARGET/sbin/slurmctld" ] || { echo "rollback target is not installed" >&2; exit 1; }
CURRENT="$PREFIX/current"
OLD=$(readlink "$CURRENT" 2>/dev/null || true)
ln -sfn "$PREFIX/$TARGET" "$CURRENT"
printf '%s\n' "rolled_back_to=$PREFIX/$TARGET" "previous=$OLD" > "$PREFIX/last-rollback"
echo "Selected $PREFIX/$TARGET. Ledger state was retained; no service was restarted."
